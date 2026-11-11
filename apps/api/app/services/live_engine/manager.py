"""Viewer/stream orchestration and one cancellation/cleanup owner."""
from __future__ import annotations
import logging
import subprocess
import threading
import time
import uuid
from pathlib import Path
from app.core.config import settings
from app.core.sanitization import redact_text
from app.db.session import SessionLocal
from app.models.camera import Camera
from app.services.hardware import hardware_capabilities_summary
from .viewers import ViewerRegistry, ViewerSession
from .stream_instance import StreamInstance, _camera_source, classify_live_failure
from .mse import MseRegistry

logger = logging.getLogger(__name__)
StreamKey = tuple[int, str]


class StreamManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.streams: dict[StreamKey, StreamInstance] = {}
        # Separate from orchestration lock: callbacks run under an instance lock
        # and must never acquire a lock which waits for a different instance.
        self.capacity_lock = threading.Lock()
        self.transcode_reservations: dict[StreamKey, StreamInstance] = {}
        self.viewer_registry = ViewerRegistry()
        self.worker_thread: threading.Thread | None = None
        self.worker_stop_event = threading.Event()
        self.mse = MseRegistry()

    @property
    def viewers(self):
        return self.viewer_registry.sessions

    def _key(self, camera_id: int, stream: str) -> StreamKey:
        return int(camera_id), str(stream or "main").lower()

    def _get_stream_locked(self, camera_id: int, stream: str) -> StreamInstance:
        key = self._key(camera_id, stream)
        instance = self.streams.get(key)
        if not instance:
            instance = StreamInstance(camera_id=key[0], stream=key[1])
            self.streams[key] = instance
            instance.acquire_transcode = lambda: self._reserve_transcode(key, instance)
            instance.release_transcode = lambda: self._release_transcode(key, instance)
        return instance

    def _reserve_transcode(self, key: StreamKey, owner: StreamInstance) -> bool:
        with self.capacity_lock:
            if self.streams.get(key) is not owner:
                return False
            reserved = self.transcode_reservations.get(key)
            if reserved is owner:
                return True
            if reserved is not None:
                return False
            limit = int(settings.live_max_concurrent_transcodes or 0)
            if limit > 0 and len(self.transcode_reservations) >= limit:
                return False
            self.transcode_reservations[key] = owner
            return True

    def _release_transcode(self, key: StreamKey, owner: StreamInstance) -> None:
        with self.capacity_lock:
            if self.transcode_reservations.get(key) is owner:
                self.transcode_reservations.pop(key, None)

    def _viewer_count_locked(self, camera_id: int, stream: str) -> int:
        return self.viewer_registry.count(camera_id, stream)

    def _viewer_details_locked(self, camera_id: int, stream: str) -> list[dict]:
        return self.viewer_registry.details(camera_id, stream, time.time())

    def _running_transcodes_locked(self, exclude_key: StreamKey | None = None) -> int:
        with self.capacity_lock:
            return sum(key != exclude_key for key in self.transcode_reservations)

    def _cleanup_stale_viewers_locked(self, now: float | None = None):
        now = now or time.time()
        ttl = max(int(settings.live_viewer_ttl_seconds), 15)
        for viewer in self.viewer_registry.expire(now, ttl):
            logger.warning(
                "Live Engine viewer expired viewer_id=%s camera_id=%s stream=%s idle_seconds=%s",
                viewer.id, viewer.camera_id, viewer.stream, round(now - viewer.last_seen, 2),
            )

    def start_or_reuse_stream(self, camera: Camera, stream: str) -> dict:
        with self.lock:
            instance = self._get_stream_locked(camera.id, stream)
            viewers = self._viewer_count_locked(camera.id, stream)

        result = instance.start(camera)
        if not result.get("ok"):
            return result

        snapshot = instance.snapshot(viewers=viewers)
        return {
            "ok": True,
            **snapshot,
            "stream_url": f"/api/live/{camera.id}/{stream}/index.m3u8",
        }

    def open_viewer(self, camera: Camera, stream: str) -> dict:
        now = time.time()
        viewer_id = uuid.uuid4().hex

        with self.lock:
            instance = self._get_stream_locked(camera.id, stream)
            instance.viewer_managed = True
            instance.start_cancelled.clear()
            instance.touch()
            self.viewer_registry.add(ViewerSession(
                id=viewer_id,
                camera_id=camera.id,
                stream=stream,
                created_at=now,
                last_seen=now,
            ))
            viewers = self._viewer_count_locked(camera.id, stream)

        # Return the viewer id before any RTSP/hardware probe. The browser can
        # cancel a tile immediately, including while that probe is in flight.
        self._launch_viewer_start(instance, _camera_source(camera))
        snapshot = instance.snapshot(viewers=viewers)
        return {
            "ok": True,
            "viewer_id": viewer_id,
            "stream_url": f"/api/live/{camera.id}/{stream}/index.m3u8",
            **snapshot,
        }

    def _launch_viewer_start(self, instance: StreamInstance, camera) -> None:
        key = self._key(instance.camera_id, instance.stream)
        with self.lock:
            if self.streams.get(key) is not instance or not self._viewer_count_locked(*key):
                return
            if instance.is_running() or (instance.start_thread and instance.start_thread.is_alive()):
                return
            if instance.next_retry_at and time.time() < instance.next_retry_at:
                return
            instance.start_cancelled.clear()
            instance.camera_source = camera
            instance._set_state_locked("starting", "start_queued")

            def run():
                try:
                    with self.lock:
                        if self.streams.get(key) is not instance or not self._viewer_count_locked(*key):
                            return
                    # Admission may have read the row just before concurrent
                    # deletion. Revalidate every launch, including the first one;
                    # healthy media/heartbeat traffic still performs no DB polling.
                    with SessionLocal() as db:
                        current = db.get(Camera, instance.camera_id)
                        source = _camera_source(current) if current and not current.deleted_at else None
                    if source is None:
                        self.stop_stream(*key, reason="camera_not_found")
                        return
                    instance.start(source)
                except Exception as exc:
                    logger.error("Live Engine background start failed camera_id=%s stream=%s error=%s", *key, redact_text(str(exc)))
                    with instance.lock:
                        instance._schedule_retry_locked("live_start_failed")
                finally:
                    if instance.start_cancelled.is_set():
                        instance.stop(reason="client_stop", cleanup_files=True)

            instance.start_thread = threading.Thread(target=run, name=f"live-start-{key[0]}-{key[1]}", daemon=True)
            instance.start_thread.start()

    def close_viewer(self, viewer_id: str) -> bool:
        with self.lock:
            viewer = self.viewer_registry.pop(viewer_id, None)
            if not viewer:
                return False
            instance = self.streams.get(self._key(viewer.camera_id, viewer.stream))
            if instance:
                instance.touch()
                if self._viewer_count_locked(viewer.camera_id, viewer.stream) == 0:
                    instance.start_cancelled.set()
            return True

    def touch_viewer(self, viewer_id: str) -> bool:
        with self.lock:
            viewer = self.viewer_registry.get(viewer_id)
            if not viewer:
                return False
            viewer.last_seen = time.time()
            instance = self.streams.get(self._key(viewer.camera_id, viewer.stream))
            if instance:
                instance.touch()
            return True

    def get_playlist_file(self, camera_id: int, stream: str) -> Path:
        with self.lock:
            instance = self._get_stream_locked(camera_id, stream)
        return instance.playlist_path

    def get_segment_file(self, camera_id: int, stream: str, filename: str) -> Path:
        with self.lock:
            instance = self._get_stream_locked(camera_id, stream)
        return instance.stream_dir / filename

    def stop_stream(self, camera_id: int, stream: str, reason: str = "client_stop") -> bool:
        mse_stopped = self.mse.stop_sync(camera_id, stream)
        with self.lock:
            instance = self.streams.get(self._key(camera_id, stream))
            if not instance:
                return bool(mse_stopped)
            instance.start_cancelled.set()
            self.viewer_registry.remove_stream(camera_id, stream)
        instance.stop(reason=reason, cleanup_files=True)
        return True

    def stop_all_streams(self) -> int:
        mse_stopped = self.mse.stop_sync()
        with self.lock:
            instances = list(self.streams.values())
            self.viewer_registry.clear()
            for instance in instances:
                instance.start_cancelled.set()
        for instance in instances:
            instance.stop(reason="stop_all", cleanup_files=True)
        return len(instances) + mse_stopped

    def stop_camera(self, camera_id: int, reason: str = "camera_deleted") -> int:
        """Cancel both roles and viewers after deletion is durably committed."""
        mse_stopped = self.mse.stop_sync(int(camera_id))
        with self.lock:
            keys = [key for key in self.streams if key[0] == int(camera_id)]
            for key in keys:
                self.streams[key].start_cancelled.set()
                self.viewer_registry.remove_stream(*key)
        for key in keys:
            self.stop_stream(*key, reason=reason)
        return len(keys) + mse_stopped

    def cleanup(self):
        ttl = max(int(settings.live_idle_ttl_seconds), 5)
        now = time.time()
        with self.lock:
            self._cleanup_stale_viewers_locked(now)
            instances = list(self.streams.items())

        for key, instance in instances:
            with self.lock:
                viewers = self._viewer_count_locked(instance.camera_id, instance.stream)
            if not instance.lock.acquire(blocking=False):
                continue
            launch_retry = False
            try:
                if instance.viewer_managed and not viewers:
                    instance.start_cancelled.set()
                    instance.stop(reason="client_stop", cleanup_files=True)
                    with self.lock:
                        if self._viewer_count_locked(*key):
                            # A new viewer may arrive while terminate/wait is in
                            # progress. Do not leave that viewer attached to a
                            # stopped, permanently cancelled stream.
                            instance.start_cancelled.clear()
                            instance._schedule_retry_locked("stream_stalled" if instance.ever_ready else "process_exit")
                        elif not (instance.start_thread and instance.start_thread.is_alive()):
                            self.streams.pop(key, None)
                    continue
                instance.maintain_startup()
                if viewers > 0 and not instance.is_running():
                    starting = bool(instance.start_thread and instance.start_thread.is_alive())
                    if not starting and instance.next_retry_at is None:
                        instance.start_cancelled.clear()
                        instance._schedule_retry_locked(instance.failure_reason or ("stream_stalled" if instance.ever_ready else "process_exit"))
                    launch_retry = bool(instance.next_retry_at and now >= instance.next_retry_at and instance.camera_source)
            finally:
                instance.lock.release()
            if launch_retry:
                self._launch_viewer_start(instance, instance.camera_source)
            if viewers > 0:
                continue
            if instance.is_running() and now - instance.last_access > ttl:
                instance.stop(reason=f"idle_ttl:{int(now - instance.last_access)}s", cleanup_files=True)
            elif not instance.is_running() and now - instance.last_access > ttl:
                with self.lock:
                    self.streams.pop(key, None)

    def start_cleanup_worker(self):
        with self.lock:
            if self.worker_thread and self.worker_thread.is_alive():
                return
            self.worker_stop_event.clear()
            self.worker_thread = threading.Thread(
                target=self._cleanup_worker,
                name="live-engine-v2-cleanup",
                daemon=True,
            )
            self.worker_thread.start()

    def stop_cleanup_worker(self):
        self.worker_stop_event.set()
        thread = self.worker_thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self.worker_thread = None

    def _cleanup_worker(self):
        interval = max(int(settings.live_cleanup_interval_seconds), 5)
        logger.info("Live Engine cleanup worker started interval=%ss ttl=%ss", interval, settings.live_idle_ttl_seconds)
        while not self.worker_stop_event.wait(interval):
            try:
                self.cleanup()
            except Exception:
                logger.exception("Live Engine cleanup worker failed")
        logger.info("Live Engine cleanup worker stopped")

    def status(self, camera_id: int | None = None, stream: str | None = None) -> list[dict]:
        self.cleanup()
        with self.lock:
            self._cleanup_stale_viewers_locked()
            instances = list(self.streams.values())

        result = []
        for instance in instances:
            if camera_id is not None and instance.camera_id != camera_id:
                continue
            if stream is not None and instance.stream != stream:
                continue
            with self.lock:
                viewers = self._viewer_count_locked(instance.camera_id, instance.stream)
                viewer_details = self._viewer_details_locked(instance.camera_id, instance.stream)
            item = instance.snapshot(viewers=viewers)
            item["viewer_ids"] = [viewer["id"] for viewer in viewer_details]
            item["viewer_sessions"] = viewer_details
            item["last_heartbeat"] = max(
                [viewer["last_heartbeat"] for viewer in viewer_details],
                default=None,
            )
            result.append(item)
        return result + self.mse.snapshots(camera_id, stream)

    def debug(self, camera_id: int | None = None, stream: str | None = None) -> dict:
        items = self.status(camera_id=camera_id, stream=stream)
        with self.lock:
            self._cleanup_stale_viewers_locked()
            viewers = [
                {
                    "id": viewer.id,
                    "camera_id": viewer.camera_id,
                    "stream": viewer.stream,
                    "age_seconds": round(time.time() - viewer.created_at, 2),
                    "idle_seconds": round(time.time() - viewer.last_seen, 2),
                }
                for viewer in self.viewers.values()
            ]
        return {
            "items": items,
            "count": len(items),
            "viewers": viewers,
            "viewers_count": len(viewers),
            "hardware_capabilities": hardware_capabilities_summary(),
        }


manager = StreamManager()


def start_cleanup_worker():
    manager.start_cleanup_worker()


def stop_cleanup_worker():
    manager.stop_cleanup_worker()


def stop_all_streams() -> int:
    return manager.stop_all_streams()
