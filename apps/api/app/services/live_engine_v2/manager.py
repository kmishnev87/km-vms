from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.models.camera import Camera
from app.services.live_engine_v2.ffmpeg import (
    build_hls_command,
    choose_input_url,
    command_text,
    inspect_input_url,
    probe_video_codec,
)

logger = logging.getLogger(__name__)

StreamKey = tuple[int, str]
FFMPEG_PROGRESS_PATTERNS = (
    "frame=",
    "Opening '",
    "Opening \"",
    ".ts'",
    ".ts\"",
    ".m3u8.tmp",
)
FFMPEG_FATAL_PATTERNS = (
    "401 Unauthorized",
    "403 Forbidden",
    "Connection refused",
    "Connection timed out",
    "Invalid data found",
    "Server returned 4",
    "Could not write header",
    "Conversion failed",
)
STARTUP_INITIAL_TIMEOUT_SECONDS = 20
STARTUP_PROGRESS_GRACE_SECONDS = 90
STARTUP_HARD_TIMEOUT_SECONDS = 180
SLOW_TRANSCODE_FAIL_SECONDS = 75
PROGRESS_RE = re.compile(
    r"frame=\s*(?P<frame>\d+).*?fps=\s*(?P<fps>[\d.]+).*?time=(?P<time>\d+:\d+:\d+(?:\.\d+)?).*?speed=\s*(?P<speed>[\d.]+)x",
    re.S,
)


def _camera_source(camera: Camera):
    return SimpleNamespace(
        id=camera.id,
        rtsp_main_url=camera.rtsp_main_url,
        rtsp_sub_url=camera.rtsp_sub_url,
        rtsp_transport=camera.rtsp_transport,
    )


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
        self.camera_source = None
        self.start_deadline: float | None = None
        self.start_hard_deadline: float | None = None
        self.last_ffmpeg_progress_at: float | None = None
        self.progress_detected = False
        self.stderr_progress_size = 0
        self.last_frame: int | None = None
        self.last_fps: float | None = None
        self.last_speed: float | None = None
        self.last_progress_time: str | None = None
        self.too_slow_since: float | None = None
        self.stop_reason: str | None = None
        self.stopped_by_backend = False
        self.state_changed_at = time.time()
        self.last_state_transition = "initialized"

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

    def _set_state_locked(self, status: str, transition: str, failure_reason: str | None = None):
        if self.status != status or self.last_state_transition != transition:
            self.state_changed_at = time.time()
            self.last_state_transition = transition
        self.status = status
        self.failure_reason = failure_reason

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

    def _detect_progress_locked(self, stderr_tail: str | None = None) -> bool:
        tail = stderr_tail if stderr_tail is not None else self._read_log_tail()
        if not tail:
            return False
        path = self.stderr_path or self.default_stderr_path
        try:
            current_size = path.stat().st_size if path.exists() else 0
        except Exception:
            current_size = 0
        if current_size <= self.stderr_progress_size and self.progress_detected:
            return False
        if any(pattern in tail for pattern in FFMPEG_PROGRESS_PATTERNS):
            self.progress_detected = True
            self.stderr_progress_size = current_size
            self.last_ffmpeg_progress_at = time.time()
            self._parse_progress_locked(tail)
            return True
        return False

    def _parse_progress_locked(self, stderr_tail: str):
        matches = list(PROGRESS_RE.finditer(stderr_tail or ""))
        if not matches:
            return

        match = matches[-1]
        try:
            self.last_frame = int(match.group("frame"))
        except Exception:
            pass
        try:
            self.last_fps = float(match.group("fps"))
        except Exception:
            pass
        try:
            self.last_speed = float(match.group("speed"))
        except Exception:
            pass
        self.last_progress_time = match.group("time")

        if self.last_speed is not None and self.last_speed < 0.25:
            self.too_slow_since = self.too_slow_since or time.time()
        else:
            self.too_slow_since = None

    def _speed_state(self) -> str:
        if self.last_speed is None:
            return "unknown"
        if self.last_speed >= 0.9:
            return "normal"
        if self.last_speed >= 0.5:
            return "degraded"
        if self.last_speed >= 0.25:
            return "slow"
        return "too_slow"

    def _detect_fatal_error(self, stderr_tail: str | None = None) -> str | None:
        tail = stderr_tail if stderr_tail is not None else self._read_log_tail()
        if not tail:
            return None
        for pattern in FFMPEG_FATAL_PATTERNS:
            if pattern in tail:
                return pattern
        return None

    def _mark_process_exit_locked(self, reason: str = "ffmpeg_exit", cleanup_files: bool = True):
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
        self._set_state_locked("failed", reason, failure_reason=reason)
        self.started_at = None
        self.start_deadline = None
        self.start_hard_deadline = None

        if cleanup_files:
            shutil.rmtree(self.stream_dir, ignore_errors=True)

    def _choose_mode(self, camera: Camera, input_url: str, force_mode: str | None = None) -> str:
        if force_mode == "fallback_transcode":
            self.last_fallback_reason = self.last_fallback_reason or "forced_fallback"
            return "fallback_transcode"

        if force_mode == "copy":
            return "copy"

        requested_policy = (settings.live_video_codec or "auto").lower()
        if settings.live_transcode or requested_policy in {"libx264", "h264", "transcode", "fallback_transcode"}:
            self.last_fallback_reason = "forced_transcode" if settings.live_transcode else f"settings_codec:{requested_policy}"
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
                self._set_state_locked("failed", "no_rtsp_url", failure_reason="no_rtsp_url")
                self.last_error = "No RTSP URL for selected stream"
                return {
                    "ok": False,
                    "error": "Не найден RTSP URL для выбранного потока",
                    "error_code": "no_rtsp_url",
                }

            inspect_input_url(camera, self.stream, input_url)
            self._cleanup_dir()
            self._set_state_locked("starting", "start_requested", failure_reason=None)
            self.camera_source = _camera_source(camera)
            self.progress_detected = False
            self.last_ffmpeg_progress_at = None
            self.stderr_progress_size = 0
            self.last_frame = None
            self.last_fps = None
            self.last_speed = None
            self.last_progress_time = None
            self.too_slow_since = None
            self.stop_reason = None
            self.stopped_by_backend = False
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
                self._set_state_locked("failed", "ffmpeg_start_failed", failure_reason="ffmpeg_start_failed")
                self.last_error = str(exc)
                logger.exception(
                    "Live Engine failed to start ffmpeg camera_id=%s stream=%s mode=%s command=%s",
                    camera.id,
                    self.stream,
                    self.mode,
                    self.cmd_text,
                )
                return {"ok": False, "error": str(exc), **self.snapshot(viewers=0)}

            self._set_state_locked("starting", "ffmpeg_started", failure_reason=None)
            self.started_at = time.time()
            initial_timeout = max(int(settings.live_start_timeout_seconds), STARTUP_INITIAL_TIMEOUT_SECONDS)
            self.start_deadline = self.started_at + initial_timeout
            self.start_hard_deadline = self.started_at + max(initial_timeout * 2, STARTUP_HARD_TIMEOUT_SECONDS)
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
            self._set_state_locked("stopping", f"stopping:{reason}", failure_reason=None)
            self.stop_reason = reason
            self.stopped_by_backend = True
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
            graceful = reason.startswith("idle") or reason in {"stop_all", "client_stop"}
            if graceful:
                self._set_state_locked("stopped", reason, failure_reason=None)
            else:
                self._set_state_locked("failed", reason, failure_reason=reason)
            self.started_at = None
            self.start_deadline = None
            self.start_hard_deadline = None

            if cleanup_files:
                shutil.rmtree(self.stream_dir, ignore_errors=True)

    def maintain_startup(self) -> bool:
        with self.lock:
            if self.is_ready():
                self._set_state_locked("ready", "hls_ready", failure_reason=None)
                return False

            stderr_tail = self._read_log_tail()
            fatal_error = self._detect_fatal_error(stderr_tail)

            if self.proc and self.proc.poll() is not None:
                previous_mode = self.mode
                source = self.camera_source
                reason = "copy_failed" if previous_mode == "copy" else "ffmpeg_exit"
                if previous_mode != "copy" and fatal_error:
                    reason = "rtsp_error" if "Connection" in fatal_error or "401" in fatal_error or "403" in fatal_error else "transcode_failed"
                self._mark_process_exit_locked(reason=reason, cleanup_files=True)
                if previous_mode == "copy" and source:
                    self.restart_count += 1
                    self.last_fallback_reason = "copy_failed"
                    self.start(source, force_mode="fallback_transcode")
                    return True
                return False

            if not self.is_running() or not self.start_deadline:
                return False

            self._detect_progress_locked(stderr_tail)
            now = time.time()
            if now <= self.start_deadline:
                return False

            if fatal_error:
                self.stop(reason="rtsp_error", cleanup_files=True)
                self.last_error = stderr_tail
                return True

            source = self.camera_source
            if self.mode == "copy" and source:
                logger.warning(
                    "Live Engine copy mode timed out before HLS ready, switching to transcode camera_id=%s stream=%s",
                    self.camera_id,
                    self.stream,
                )
                self.restart_count += 1
                self.last_fallback_reason = "copy_not_ready"
                self.stop(reason="copy_not_ready", cleanup_files=True)
                self.start(source, force_mode="fallback_transcode")
                return True

            last_progress = self.last_ffmpeg_progress_at or self.started_at or now
            hard_deadline = self.start_hard_deadline or (now + STARTUP_HARD_TIMEOUT_SECONDS)
            if (
                self.last_speed is not None
                and self.last_speed < 0.25
                and self.too_slow_since
                and not self.is_ready()
                and now - self.too_slow_since > SLOW_TRANSCODE_FAIL_SECONDS
            ):
                self.stop(reason="slow_transcode_no_hls", cleanup_files=True)
                self.last_error = stderr_tail
                return True

            if self.progress_detected and now - last_progress <= STARTUP_PROGRESS_GRACE_SECONDS and now <= hard_deadline:
                self._set_state_locked("starting", "ffmpeg_progress", failure_reason=None)
                return False

            if now <= hard_deadline and self.playlist_path.exists():
                self._set_state_locked("starting", "hls_partial_output", failure_reason=None)
                return False

            reason = "startup_timeout_no_progress" if not self.progress_detected else "startup_timeout_no_hls"
            self.stop(reason=reason, cleanup_files=True)
            self.last_error = stderr_tail
            return True

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
            "startup_elapsed_seconds": round(now - self.started_at, 2) if self.started_at and not ready else 0,
            "startup_deadline_seconds": round(self.start_deadline - now, 2) if self.start_deadline else None,
            "startup_hard_deadline_seconds": round(self.start_hard_deadline - now, 2) if self.start_hard_deadline else None,
            "last_ffmpeg_progress_at": self.last_ffmpeg_progress_at,
            "ffmpeg_progress_detected": self.progress_detected,
            "last_frame": self.last_frame,
            "last_fps": self.last_fps,
            "last_speed": self.last_speed,
            "last_progress_time": self.last_progress_time,
            "speed_state": self._speed_state(),
            "too_slow_seconds": round(now - self.too_slow_since, 2) if self.too_slow_since else 0,
            "stop_reason": self.stop_reason,
            "stopped_by_backend": self.stopped_by_backend,
            "state_changed_at": self.state_changed_at,
            "last_state_transition": self.last_state_transition,
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
            "stderr_tail": self._read_log_tail() or self.last_error,
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
        wait_for_ready: bool = False,
    ) -> dict:
        with self.lock:
            instance = self._get_stream_locked(camera.id, stream)
            viewers = self._viewer_count_locked(camera.id, stream)

        result = instance.start(camera)
        if not result.get("ok"):
            return result

        snapshot = instance.snapshot(viewers=viewers)
        if wait_for_ready and not snapshot["ready"]:
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
            if result.get("error_code") == "no_rtsp_url":
                self.close_viewer(viewer_id)
                return result

            with self.lock:
                viewers = self._viewer_count_locked(camera.id, stream)
            snapshot = instance.snapshot(viewers=viewers)
            return {
                "ok": True,
                "viewer_id": viewer_id,
                "stream_url": f"/api/live/{camera.id}/{stream}/index.m3u8",
                "recoverable_start_error": result.get("error") or snapshot.get("failure_reason"),
                **snapshot,
            }

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

    def get_playlist_file(self, camera_id: int, stream: str) -> Path:
        with self.lock:
            instance = self._get_stream_locked(camera_id, stream)
        return instance.playlist_path

    def get_segment_file(self, camera_id: int, stream: str, filename: str) -> Path:
        with self.lock:
            instance = self._get_stream_locked(camera_id, stream)
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
            instance.maintain_startup()
            with self.lock:
                viewers = self._viewer_count_locked(instance.camera_id, instance.stream)
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
