from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import settings
from app.models.camera import Camera
from app.services.live_engine_v2.ffmpeg import (
    build_hls_command,
    choose_input_url,
    command_text,
    inspect_input_url,
    mask_url_password,
    probe_video_codec,
)

logger = logging.getLogger(__name__)

StreamKey = tuple[int, str]


@dataclass
class ViewerSession:
    id: str
    camera_id: int
    stream: str
    created_at: float
    last_seen: float


class StreamInstance:
    def __init__(self, camera_id: int, stream: str):
        self.camera_id = camera_id
        self.stream = stream
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.stderr_file = None
        self.stderr_path: Path | None = None
        self.cmd_text = ""
        self.mode = "fallback_transcode"
        self.requested_mode = "auto"
        self.status = "stopped"
        self.started_at: float | None = None
        self.last_access = time.time()
        self.last_exit_code: int | None = None
        self.last_error: str | None = None
        self.last_fallback_reason: str | None = None
        self.failure_reason: str | None = None
        self.restart_count = 0

    @property
    def sid(self) -> str:
        return f"{self.camera_id}_{self.stream}"

    @property
    def stream_dir(self) -> Path:
        path = Path(settings.storage_previews) / "live_v2" / str(self.camera_id) / self.stream
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def playlist_path(self) -> Path:
        return self.stream_dir / "index.m3u8"

    @property
    def default_stderr_path(self) -> Path:
        return self.stream_dir / "ffmpeg.log"

    def is_running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def is_ready(self) -> bool:
        try:
            segments = list(self.stream_dir.glob("seg_*.ts"))
            return self.playlist_path.exists() and self.playlist_path.stat().st_size > 0 and bool(segments)
        except Exception:
            return False

    def touch(self):
        self.last_access = time.time()

    def _cleanup_dir(self):
        if self.stream_dir.exists():
            shutil.rmtree(self.stream_dir, ignore_errors=True)
        self.stream_dir.mkdir(parents=True, exist_ok=True)

    def _read_log_tail(self, max_chars: int = 4000) -> str:
        path = self.stderr_path or self.default_stderr_path
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-max_chars:]
        except Exception:
            return ""

    def _mark_process_exit_locked(self, reason: str = "process_exit", cleanup_files: bool = True):
        proc = self.proc
        if not proc:
            return

        if self.stderr_file:
            try:
                self.stderr_file.flush()
                self.stderr_file.close()
            except Exception:
                pass

        self.last_exit_code = proc.poll()
        self.failure_reason = reason
        self.last_error = self._read_log_tail()
        logger.warning(
            "Live Engine ffmpeg exited camera_id=%s stream=%s pid=%s reason=%s exit_code=%s mode=%s command=%s stderr_tail=%s",
            self.camera_id,
            self.stream,
            proc.pid,
            reason,
            self.last_exit_code,
            self.mode,
            self.cmd_text,
            self.last_error,
        )
        self.proc = None
        self.stderr_file = None
        self.status = "failed"
        self.started_at = None

        if cleanup_files:
            shutil.rmtree(self.stream_dir, ignore_errors=True)

    def _choose_mode(self, camera: Camera, input_url: str, force_mode: str | None = None) -> str:
        if force_mode == "fallback_transcode":
            self.last_fallback_reason = self.last_fallback_reason or "forced_fallback"
            return "fallback_transcode"

        if force_mode == "copy":
            return "copy"

        if settings.live_transcode:
            self.last_fallback_reason = "settings_live_transcode_enabled"
            return "fallback_transcode"

        requested_codec = (settings.live_video_codec or "copy").lower()
        if requested_codec != "copy":
            self.last_fallback_reason = f"settings_codec:{requested_codec}"
            return "fallback_transcode"

        probe = probe_video_codec(input_url, (camera.rtsp_transport or "tcp").lower())
        self.last_error = probe.error
        if probe.safe_for_copy:
            self.last_fallback_reason = None
            return "copy"

        self.last_fallback_reason = f"codec_not_safe_for_copy:{probe.codec or 'unknown'}"
        logger.warning(
            "Live Engine fallback selected camera_id=%s stream=%s codec=%s probe_error=%s",
            camera.id,
            self.stream,
            probe.codec,
            probe.error,
        )
        return "fallback_transcode"

    def start(self, camera: Camera, force_mode: str | None = None) -> dict:
        with self.lock:
            self.touch()
            if self.is_running():
                return self.snapshot(viewers=0)

            if self.proc:
                if self.proc.poll() is not None:
                    self._mark_process_exit_locked(reason="restart_dead_process", cleanup_files=True)
                else:
                    self.stop(reason="restart_running_process", cleanup_files=True)

            input_url = choose_input_url(camera, self.stream)
            if not input_url:
                self.status = "failed"
                self.last_error = "No RTSP URL for selected stream"
                return {"ok": False, "error": "Не найден RTSP URL для выбранного потока"}

            inspect_input_url(camera, self.stream, input_url)
            self._cleanup_dir()
            self.status = "starting"
            self.failure_reason = None
            self.mode = self._choose_mode(camera, input_url, force_mode=force_mode)
            self.requested_mode = force_mode or "auto"
            cmd = build_hls_command(
                camera=camera,
                stream=self.stream,
                input_url=input_url,
                out_dir=self.stream_dir,
                mode="copy" if self.mode == "copy" else "fallback_transcode",
            )
            self.cmd_text = command_text(cmd, input_url)
            self.stderr_path = self.default_stderr_path
            self.stderr_file = open(self.stderr_path, "a", encoding="utf-8")

            logger.info(
                "Live Engine starting ffmpeg camera_id=%s stream=%s mode=%s command=%s",
                camera.id,
                self.stream,
                self.mode,
                self.cmd_text,
            )

            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=self.stderr_file,
                    text=True,
                )
            except Exception as exc:
                try:
                    self.stderr_file.write(f"Failed to start ffmpeg: {exc}\n")
                    self.stderr_file.flush()
                    self.stderr_file.close()
                except Exception:
                    pass
                self.proc = None
                self.stderr_file = None
                self.status = "failed"
                self.failure_reason = "ffmpeg_start_failed"
                self.last_error = str(exc)
                logger.exception(
                    "Live Engine failed to start ffmpeg camera_id=%s stream=%s mode=%s command=%s",
                    camera.id,
                    self.stream,
                    self.mode,
                    self.cmd_text,
                )
                return {"ok": False, "error": str(exc), **self.snapshot(viewers=0)}

            self.status = "running"
            self.started_at = time.time()
            self.last_exit_code = None
            self.last_error = None

            logger.info(
                "Live Engine started stream camera_id=%s stream=%s pid=%s mode=%s fallback_reason=%s",
                camera.id,
                self.stream,
                self.proc.pid,
                self.mode,
                self.last_fallback_reason,
            )

            return {"ok": True, **self.snapshot(viewers=0)}

    def stop(self, reason: str, cleanup_files: bool = True):
        with self.lock:
            self.status = "stopping"
            proc = self.proc
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=5)
                    except Exception:
                        pass

            if self.stderr_file:
                try:
                    self.stderr_file.flush()
                    self.stderr_file.close()
                except Exception:
                    pass

            exit_code = proc.poll() if proc else None
            self.last_exit_code = exit_code
            stderr_tail = self._read_log_tail()
            logger.warning(
                "Live Engine ffmpeg stopped camera_id=%s stream=%s pid=%s reason=%s exit_code=%s mode=%s command=%s stderr_tail=%s",
                self.camera_id,
                self.stream,
                proc.pid if proc else None,
                reason,
                exit_code,
                self.mode,
                self.cmd_text,
                stderr_tail,
            )

            self.proc = None
            self.stderr_file = None
            self.status = "stopped" if reason.startswith("idle") or reason in {"stop_all", "client_stop"} else "failed"
            self.failure_reason = None if self.status == "stopped" else reason
            self.started_at = None

            if cleanup_files:
                shutil.rmtree(self.stream_dir, ignore_errors=True)

    def note_process_exit_if_needed(self) -> bool:
        with self.lock:
            if self.proc and self.proc.poll() is not None:
                self._mark_process_exit_locked(reason="process_exit", cleanup_files=True)
                return True
            return False

    def snapshot(self, viewers: int) -> dict:
        now = time.time()
        running = self.is_running()
        ready = self.is_ready()
        if running and ready:
            status = "ready"
        elif running:
            status = self.status if self.status in {"starting", "restarting"} else "starting"
        else:
            status = self.status
        return {
            "stream_key": self.sid,
            "camera_id": self.camera_id,
            "stream": self.stream,
            "stream_type": self.stream,
            "pid": self.proc.pid if running and self.proc else None,
            "running": running,
            "status": status,
            "mode": "copy" if self.mode == "copy" else "transcode",
            "requested_mode": self.requested_mode,
            "viewers": viewers,
            "started_at": self.started_at,
            "last_access": self.last_access,
            "uptime_seconds": round(now - self.started_at, 2) if self.started_at else 0,
            "idle_seconds": round(now - self.last_access, 2),
            "ready": ready,
            "playlist_path": str(self.playlist_path),
            "playlist_exists": self.playlist_path.exists(),
            "playlist_size": self.playlist_path.stat().st_size if self.playlist_path.exists() else 0,
            "segment_count": len(list(self.stream_dir.glob("seg_*.ts"))) if self.stream_dir.exists() else 0,
            "segments_count": len(list(self.stream_dir.glob("seg_*.ts"))) if self.stream_dir.exists() else 0,
            "exit_code": self.last_exit_code,
            "fallback_reason": self.last_fallback_reason,
            "failure_reason": self.failure_reason,
            "last_error": self.last_error,
            "restart_count": self.restart_count,
            "stderr_tail": self._read_log_tail(),
            "command": self.cmd_text,
        }


class StreamManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.streams: dict[StreamKey, StreamInstance] = {}
        self.viewers: dict[str, ViewerSession] = {}
        self.worker_thread: threading.Thread | None = None
        self.worker_stop_event = threading.Event()

    def _key(self, camera_id: int, stream: str) -> StreamKey:
        return int(camera_id), str(stream or "main").lower()

    def _get_stream_locked(self, camera_id: int, stream: str) -> StreamInstance:
        key = self._key(camera_id, stream)
        instance = self.streams.get(key)
        if not instance:
            instance = StreamInstance(camera_id=key[0], stream=key[1])
            self.streams[key] = instance
        return instance

    def _viewer_count_locked(self, camera_id: int, stream: str) -> int:
        key = self._key(camera_id, stream)
        return sum(1 for viewer in self.viewers.values() if self._key(viewer.camera_id, viewer.stream) == key)

    def _viewer_details_locked(self, camera_id: int, stream: str) -> list[dict]:
        now = time.time()
        key = self._key(camera_id, stream)
        return [
            {
                "id": viewer.id,
                "created_at": viewer.created_at,
                "last_heartbeat": viewer.last_seen,
                "age_seconds": round(now - viewer.created_at, 2),
                "idle_seconds": round(now - viewer.last_seen, 2),
            }
            for viewer in self.viewers.values()
            if self._key(viewer.camera_id, viewer.stream) == key
        ]

    def _cleanup_stale_viewers_locked(self, now: float | None = None):
        now = now or time.time()
        ttl = max(int(settings.live_viewer_ttl_seconds), 15)
        for viewer_id, viewer in list(self.viewers.items()):
            if now - viewer.last_seen > ttl:
                logger.warning(
                    "Live Engine viewer expired viewer_id=%s camera_id=%s stream=%s idle_seconds=%s",
                    viewer_id,
                    viewer.camera_id,
                    viewer.stream,
                    round(now - viewer.last_seen, 2),
                )
                self.viewers.pop(viewer_id, None)

    def ensure_stream(
        self,
        camera: Camera,
        stream: str,
        wait_for_ready: bool = True,
        allow_fallback_retry: bool = True,
    ) -> dict:
        with self.lock:
            instance = self._get_stream_locked(camera.id, stream)
            viewers = self._viewer_count_locked(camera.id, stream)

        result = instance.start(camera)
        if not result.get("ok"):
            return result

        if wait_for_ready:
            deadline = time.time() + max(int(settings.live_start_timeout_seconds), 2)
            while time.time() < deadline:
                if instance.note_process_exit_if_needed():
                    if allow_fallback_retry and instance.mode == "copy":
                        logger.warning(
                            "Live Engine copy mode failed before ready, switching to fallback camera_id=%s stream=%s",
                            camera.id,
                            stream,
                        )
                        with instance.lock:
                            instance.restart_count += 1
                            instance.last_fallback_reason = "copy_failed_before_ready"
                            result = instance.start(camera, force_mode="fallback_transcode")
                        if not result.get("ok"):
                            return result
                        deadline = time.time() + max(int(settings.live_start_timeout_seconds), 2)
                        allow_fallback_retry = False
                        continue
                    break
                if instance.is_ready():
                    break
                time.sleep(0.25)

        snapshot = instance.snapshot(viewers=viewers)
        if wait_for_ready and not snapshot["ready"]:
            if allow_fallback_retry and instance.mode == "copy" and snapshot["running"]:
                logger.warning(
                    "Live Engine copy mode did not become ready, switching to fallback camera_id=%s stream=%s",
                    camera.id,
                    stream,
                )
                with instance.lock:
                    instance.restart_count += 1
                    instance.last_fallback_reason = "copy_not_ready"
                    instance.stop(reason="copy_not_ready", cleanup_files=True)
                    result = instance.start(camera, force_mode="fallback_transcode")
                if not result.get("ok"):
                    return result
                return self.ensure_stream(
                    camera,
                    stream,
                    wait_for_ready=True,
                    allow_fallback_retry=False,
                )

            return {
                "ok": False,
                "error": snapshot.get("failure_reason") or snapshot.get("last_error") or "Live stream is not ready",
                **snapshot,
                "stream_url": f"/api/live/{camera.id}/{stream}/index.m3u8",
            }

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
            self.viewers[viewer_id] = ViewerSession(
                id=viewer_id,
                camera_id=camera.id,
                stream=stream,
                created_at=now,
                last_seen=now,
            )
            viewers = self._viewer_count_locked(camera.id, stream)

        result = self.ensure_stream(camera, stream, wait_for_ready=False)
        if not result.get("ok"):
            self.close_viewer(viewer_id)
            return result

        with self.lock:
            viewers = self._viewer_count_locked(camera.id, stream)
        snapshot = instance.snapshot(viewers=viewers)
        return {
            "ok": True,
            "viewer_id": viewer_id,
            "stream_url": f"/api/live/{camera.id}/{stream}/index.m3u8",
            **snapshot,
        }

    def close_viewer(self, viewer_id: str) -> bool:
        with self.lock:
            viewer = self.viewers.pop(viewer_id, None)
            if not viewer:
                return False
            instance = self.streams.get(self._key(viewer.camera_id, viewer.stream))
            if instance:
                instance.touch()
            return True

    def touch_viewer(self, viewer_id: str) -> bool:
        with self.lock:
            viewer = self.viewers.get(viewer_id)
            if not viewer:
                return False
            viewer.last_seen = time.time()
            instance = self.streams.get(self._key(viewer.camera_id, viewer.stream))
            if instance:
                instance.touch()
            return True

    def force_fallback(self, camera: Camera, stream: str, reason: str) -> dict:
        with self.lock:
            instance = self._get_stream_locked(camera.id, stream)
            viewers = self._viewer_count_locked(camera.id, stream)

        with instance.lock:
            instance.last_fallback_reason = reason or "client_fallback"
            if instance.is_running():
                instance.stop(reason=f"fallback_switch:{reason}", cleanup_files=True)
            result = instance.start(camera, force_mode="fallback_transcode")

        if not result.get("ok"):
            return result

        return {
            "ok": True,
            **instance.snapshot(viewers=viewers),
            "stream_url": f"/api/live/{camera.id}/{stream}/index.m3u8",
        }

    def get_playlist_file(self, camera_id: int, stream: str) -> Path:
        with self.lock:
            instance = self._get_stream_locked(camera_id, stream)
        instance.touch()
        return instance.playlist_path

    def get_segment_file(self, camera_id: int, stream: str, filename: str) -> Path:
        with self.lock:
            instance = self._get_stream_locked(camera_id, stream)
        instance.touch()
        return instance.stream_dir / filename

    def stop_stream(self, camera_id: int, stream: str, reason: str = "client_stop") -> bool:
        with self.lock:
            instance = self.streams.get(self._key(camera_id, stream))
            if not instance:
                return False
            for viewer_id, viewer in list(self.viewers.items()):
                if self._key(viewer.camera_id, viewer.stream) == self._key(camera_id, stream):
                    self.viewers.pop(viewer_id, None)
        instance.stop(reason=reason, cleanup_files=True)
        return True

    def stop_all_streams(self) -> int:
        with self.lock:
            instances = list(self.streams.values())
            self.viewers.clear()
        for instance in instances:
            instance.stop(reason="stop_all", cleanup_files=True)
        return len(instances)

    def cleanup(self):
        ttl = max(int(settings.live_idle_ttl_seconds), 5)
        now = time.time()
        with self.lock:
            self._cleanup_stale_viewers_locked(now)
            instances = list(self.streams.items())

        for key, instance in instances:
            instance.note_process_exit_if_needed()
            with self.lock:
                viewers = self._viewer_count_locked(instance.camera_id, instance.stream)
            if viewers > 0:
                continue
            if instance.is_running() and now - instance.last_access > ttl:
                instance.stop(reason=f"idle_ttl:{int(now - instance.last_access)}s", cleanup_files=True)

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
        return result

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
        }


manager = StreamManager()


def start_cleanup_worker():
    manager.start_cleanup_worker()


def stop_cleanup_worker():
    manager.stop_cleanup_worker()


def stop_all_streams() -> int:
    return manager.stop_all_streams()
