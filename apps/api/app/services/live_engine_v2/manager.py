from __future__ import annotations

import logging
import os
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
from app.services.hardware import get_hardware_capabilities, hardware_capabilities_summary
from app.services.live_engine_v2.ffmpeg import (
    build_hls_command,
    choose_input_url,
    command_text,
    inspect_input_url,
    mask_rtsp_credentials,
    probe_video_codec,
    select_output_fps,
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
HARDWARE_STARTUP_TIMEOUT_SECONDS = 6
HARDWARE_HARD_TIMEOUT_SECONDS = 12
STARTUP_PROGRESS_GRACE_SECONDS = 90
STARTUP_HARD_TIMEOUT_SECONDS = 180
SLOW_TRANSCODE_FAIL_SECONDS = 75
FRAME_RE = re.compile(r"frame=\s*(?P<value>\d+)")
FPS_RE = re.compile(r"fps=\s*(?P<value>[\d.]+)")
TIME_RE = re.compile(r"time=(?P<value>\d+:\d+:\d+(?:\.\d+)?)")
SPEED_RE = re.compile(r"speed=\s*(?P<value>[\d.]+)x")
DUP_RE = re.compile(r"dup=\s*(?P<value>\d+)")
DROP_RE = re.compile(r"drop=\s*(?P<value>\d+)")


def _camera_source(camera: Camera):
    return SimpleNamespace(
        id=camera.id,
        rtsp_main_url=camera.rtsp_main_url,
        rtsp_sub_url=camera.rtsp_sub_url,
        rtsp_transport=camera.rtsp_transport,
    )


def _read_proc_cmdline(pid: int | None) -> str:
    if not pid:
        return ""
    path = Path("/proc") / str(pid) / "cmdline"
    try:
        raw = path.read_bytes()
    except Exception:
        return ""
    return mask_rtsp_credentials(raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip())


def _read_proc_state(pid: int | None) -> str:
    if not pid:
        return ""
    path = Path("/proc") / str(pid) / "stat"
    try:
        parts = path.read_text(encoding="utf-8", errors="replace").split()
        return parts[2] if len(parts) > 2 else ""
    except Exception:
        return ""


def _pid_exists(pid: int | None) -> bool:
    if not pid:
        return False
    if Path("/proc").exists():
        return (Path("/proc") / str(pid)).exists()
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


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
        self.input_codec: str | None = None
        self.input_width: int | None = None
        self.input_height: int | None = None
        self.input_fps: float | None = None
        self.output_fps: float | None = None
        self.force_stable_fps = False
        self.jitter_detected = False
        self.unstable_source = False
        self.restart_reason: str | None = None
        self.auto_restart_allowed = False
        self.copy_eligible = False
        self.browser_compatible = False
        self.reason_for_transcode: str | None = None
        self.high_cpu_risk = False
        self.resource_limit: str | None = None
        self.hw_backend: str | None = None
        self.hw_device: str | None = None
        self.hwaccel_mode = settings.live_hwaccel_mode
        self.selected_pipeline = "cpu"
        self.hw_decode = False
        self.hw_encode = False
        self.fallback_to_cpu = False
        self.hw_failure_reason: str | None = None
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
        self.dup_frames: int | None = None
        self.drop_frames: int | None = None
        self.last_progress_time: str | None = None
        self.too_slow_since: float | None = None
        self.unstable_since: float | None = None
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
        return self.process_info()["running_verified"]

    def process_info(self) -> dict:
        proc = self.proc
        poll_value = proc.poll() if proc else None
        pid = proc.pid if proc else None
        pid_exists = _pid_exists(pid)
        cmdline = _read_proc_cmdline(pid)
        state = _read_proc_state(pid)
        is_zombie = state == "Z"
        proc_fs_available = Path("/proc").exists()
        cmdline_ok = "ffmpeg" in cmdline.lower() if cmdline else not proc_fs_available
        running_verified = bool(proc and poll_value is None and pid_exists and not is_zombie and cmdline_ok)
        return {
            "pid": pid,
            "pid_exists": pid_exists,
            "pid_cmdline": cmdline,
            "process_poll": poll_value,
            "is_zombie": is_zombie,
            "running_verified": running_verified,
            "process_started_at": self.started_at,
            "process_age_seconds": round(time.time() - self.started_at, 2) if self.started_at else 0,
        }

    def is_ready(self) -> bool:
        try:
            segments = list(self.stream_dir.glob("seg_*.ts"))
            return self.playlist_path.exists() and self.playlist_path.stat().st_size > 0 and bool(segments)
        except Exception:
            return False

    def _hls_update_times(self) -> tuple[float | None, float | None]:
        try:
            playlist_updated_at = self.playlist_path.stat().st_mtime if self.playlist_path.exists() else None
            segment_times = [segment.stat().st_mtime for segment in self.stream_dir.glob("seg_*.ts")]
            return playlist_updated_at, max(segment_times) if segment_times else None
        except Exception:
            return None, None

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
            return mask_rtsp_credentials(path.read_text(encoding="utf-8", errors="replace")[-max_chars:])
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
        tail = stderr_tail or ""
        if "frame=" not in tail:
            return

        def last_value(pattern):
            matches = list(pattern.finditer(tail))
            return matches[-1].group("value") if matches else None

        try:
            frame = last_value(FRAME_RE)
            self.last_frame = int(frame) if frame is not None else self.last_frame
        except Exception:
            pass
        try:
            fps = last_value(FPS_RE)
            self.last_fps = float(fps) if fps is not None else self.last_fps
        except Exception:
            pass
        try:
            speed = last_value(SPEED_RE)
            self.last_speed = float(speed) if speed is not None else self.last_speed
        except Exception:
            pass
        try:
            dup = last_value(DUP_RE)
            self.dup_frames = int(dup) if dup is not None else self.dup_frames
        except Exception:
            pass
        try:
            drop = last_value(DROP_RE)
            self.drop_frames = int(drop) if drop is not None else self.drop_frames
        except Exception:
            pass
        progress_time = last_value(TIME_RE)
        self.last_progress_time = progress_time or self.last_progress_time

        if self.last_speed is not None and self.last_speed < 0.25:
            self.too_slow_since = self.too_slow_since or time.time()
        else:
            self.too_slow_since = None

        has_dup_drop = (self.dup_frames or 0) > 0 or (self.drop_frames or 0) > 0
        slow_for_preview = self.last_speed is not None and self.last_speed < 0.7
        self.jitter_detected = bool(has_dup_drop or slow_for_preview)
        if self.jitter_detected:
            self.unstable_since = self.unstable_since or time.time()
        else:
            self.unstable_since = None

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
        probe = probe_video_codec(input_url, (camera.rtsp_transport or "tcp").lower())
        self.last_error = mask_rtsp_credentials(probe.error)
        self.input_codec = probe.codec
        self.input_width = probe.width
        self.input_height = probe.height
        self.input_fps = probe.fps
        self.output_fps, forced_fps = select_output_fps(probe.fps, self.force_stable_fps)
        self.unstable_source = bool(forced_fps)
        self.jitter_detected = bool(forced_fps)
        self.copy_eligible = bool(probe.safe_for_copy)
        self.browser_compatible = probe.codec == "h264"
        self.high_cpu_risk = bool(
            (self.stream == "main" and (probe.codec in {"hevc", "h265"} or (probe.width or 0) >= 2560))
            or (probe.width or 0) >= 3840
            or (probe.fps or 0) > 30
        )

        if force_mode == "fallback_transcode":
            self.last_fallback_reason = self.last_fallback_reason or "forced_fallback"
            self.reason_for_transcode = "forced_fallback"
            self.selected_pipeline = "cpu_transcode"
            return "fallback_transcode"

        if force_mode == "copy":
            self.selected_pipeline = "copy"
            return "copy"

        if probe.safe_for_copy:
            self.last_fallback_reason = None
            self.reason_for_transcode = None
            self.selected_pipeline = "copy"
            return "copy"

        hw_mode = (settings.live_hwaccel_mode or "auto").lower()
        caps = get_hardware_capabilities()
        preferred_backend = caps.get("preferred_backend")
        if (
            probe.codec in {"hevc", "h265"}
            and hw_mode not in {"off", "disabled", "false", "0"}
            and caps.get("hardware_accel_available")
            and preferred_backend in {"vaapi", "qsv"}
        ):
            self.hw_backend = preferred_backend
            self.hw_device = caps.get("render_device") or settings.live_hwaccel_device
            self.hwaccel_mode = settings.live_hwaccel_mode
            self.hw_decode = True
            self.hw_encode = True
            self.fallback_to_cpu = False
            self.hw_failure_reason = None
            self.selected_pipeline = "hardware_transcode"
            self.last_fallback_reason = None
            self.reason_for_transcode = "codec_requires_h264_for_hls"
            return "hardware_transcode"

        requested_policy = (settings.live_video_codec or "auto").lower()
        if settings.live_transcode or requested_policy in {"libx264", "h264", "transcode", "fallback_transcode"}:
            self.last_fallback_reason = "forced_transcode" if settings.live_transcode else f"settings_codec:{requested_policy}"
            self.reason_for_transcode = self.last_fallback_reason
            self.selected_pipeline = "cpu_transcode"
            return "fallback_transcode"

        self.last_fallback_reason = f"codec_not_safe_for_copy:{probe.codec or 'unknown'}"
        self.reason_for_transcode = self.last_fallback_reason
        self.hw_backend = None
        self.hw_device = caps.get("render_device") or settings.live_hwaccel_device
        self.hwaccel_mode = settings.live_hwaccel_mode
        self.hw_decode = False
        self.hw_encode = False
        self.selected_pipeline = "cpu_transcode"
        if probe.codec in {"hevc", "h265"} and hw_mode not in {"off", "disabled", "false", "0"}:
            warnings = caps.get("warnings") or []
            errors = caps.get("errors") or []
            self.hw_failure_reason = "; ".join([*warnings, *errors])[:1200] or "hardware_accel_not_available"
        logger.warning(
            "Live Engine fallback selected camera_id=%s stream=%s codec=%s probe_error=%s",
            camera.id,
            self.stream,
            probe.codec,
            probe.error,
        )
        return "fallback_transcode"

    def start(self, camera: Camera, force_mode: str | None = None, transcode_allowed: bool = True) -> dict:
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
            self.dup_frames = None
            self.drop_frames = None
            self.last_progress_time = None
            self.too_slow_since = None
            self.unstable_since = None
            self.stop_reason = None
            self.stopped_by_backend = False
            self.input_codec = None
            self.input_width = None
            self.input_height = None
            self.input_fps = None
            self.output_fps = None
            self.jitter_detected = False
            self.unstable_source = bool(self.force_stable_fps)
            self.copy_eligible = False
            self.browser_compatible = False
            self.reason_for_transcode = None
            self.high_cpu_risk = False
            self.resource_limit = None
            self.hw_backend = None
            self.hw_device = settings.live_hwaccel_device
            self.hwaccel_mode = settings.live_hwaccel_mode
            self.selected_pipeline = "cpu"
            self.hw_decode = False
            self.hw_encode = False
            self.fallback_to_cpu = force_mode == "fallback_transcode" and bool(self.hw_failure_reason)
            self.mode = self._choose_mode(camera, input_url, force_mode=force_mode)
            self.requested_mode = force_mode or "auto"
            if self.mode != "copy" and not transcode_allowed:
                self.resource_limit = "max_concurrent_transcodes"
                self._set_state_locked("failed", "resource_limit", failure_reason="resource_limit")
                self.last_error = "Live transcode resource limit reached"
                return {
                    "ok": False,
                    "error": self.last_error,
                    "error_code": "resource_limit",
                    **self.snapshot(viewers=0),
                }
            cmd = build_hls_command(
                camera=camera,
                stream=self.stream,
                input_url=input_url,
                out_dir=self.stream_dir,
                mode=self.mode,
                input_fps=self.input_fps,
                force_stable_fps=self.force_stable_fps,
                hw_backend=self.hw_backend,
                hw_device=self.hw_device,
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
                self.last_error = mask_rtsp_credentials(str(exc))
                if self.mode == "hardware_transcode":
                    self.fallback_to_cpu = True
                    self.hw_failure_reason = self.last_error
                    self.last_fallback_reason = "hardware_start_failed"
                    self.restart_count += 1
                    self.restart_reason = "hardware_start_failed"
                    self.start(camera, force_mode="fallback_transcode", transcode_allowed=transcode_allowed)
                    return self.snapshot(viewers=0)
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
            if self.mode == "hardware_transcode":
                self.start_deadline = self.started_at + HARDWARE_STARTUP_TIMEOUT_SECONDS
                self.start_hard_deadline = self.started_at + HARDWARE_HARD_TIMEOUT_SECONDS
            else:
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
            stderr_tail = self._read_log_tail()
            fatal_error = self._detect_fatal_error(stderr_tail)
            process_info = self.process_info()
            if self.is_running():
                self._detect_progress_locked(stderr_tail)

            if self.is_ready():
                if self.jitter_detected:
                    self.unstable_source = True
                self._set_state_locked("ready", "hls_ready", failure_reason=None)
                return False

            if self.proc and self.proc.poll() is None and not process_info["running_verified"]:
                reason = "zombie_process" if process_info["is_zombie"] else "stale_process"
                self.stop(reason=reason, cleanup_files=True)
                return True

            if self.proc and self.proc.poll() is not None:
                previous_mode = self.mode
                source = self.camera_source
                reason = "copy_failed" if previous_mode == "copy" else "ffmpeg_exit"
                if previous_mode != "copy" and fatal_error:
                    reason = "rtsp_error" if "Connection" in fatal_error or "401" in fatal_error or "403" in fatal_error else "transcode_failed"
                self._mark_process_exit_locked(reason=reason, cleanup_files=True)
                if previous_mode == "copy" and source:
                    self.restart_count += 1
                    self.restart_reason = "copy_failed"
                    self.last_fallback_reason = "copy_failed"
                    self.start(source, force_mode="fallback_transcode")
                    return True
                if previous_mode == "hardware_transcode" and source:
                    self.restart_count += 1
                    self.restart_reason = "hardware_failed"
                    self.fallback_to_cpu = True
                    self.hw_failure_reason = self.last_error or stderr_tail or reason
                    self.last_fallback_reason = "hardware_failed"
                    self.start(source, force_mode="fallback_transcode")
                    return True
                return False

            if not self.is_running() or not self.start_deadline:
                return False

            now = time.time()
            source = self.camera_source
            if self.mode == "hardware_transcode" and fatal_error and source:
                self.restart_count += 1
                self.restart_reason = "hardware_fatal_error"
                self.fallback_to_cpu = True
                self.hw_failure_reason = stderr_tail or fatal_error
                self.last_fallback_reason = "hardware_fatal_error"
                self.stop(reason="hardware_fatal_error", cleanup_files=True)
                self.start(source, force_mode="fallback_transcode")
                return True

            if now <= self.start_deadline:
                return False

            if fatal_error:
                self.stop(reason="rtsp_error", cleanup_files=True)
                self.last_error = stderr_tail
                return True

            if self.mode == "copy" and source:
                logger.warning(
                    "Live Engine copy mode timed out before HLS ready, switching to transcode camera_id=%s stream=%s",
                    self.camera_id,
                    self.stream,
                )
                self.restart_count += 1
                self.restart_reason = "copy_not_ready"
                self.last_fallback_reason = "copy_not_ready"
                self.stop(reason="copy_not_ready", cleanup_files=True)
                self.start(source, force_mode="fallback_transcode")
                return True

            if self.mode == "hardware_transcode" and source:
                self.restart_count += 1
                self.restart_reason = "hardware_no_hls"
                self.fallback_to_cpu = True
                self.hw_failure_reason = stderr_tail or "hardware_transcode did not produce HLS before fast deadline"
                self.last_fallback_reason = "hardware_no_hls"
                self.stop(reason="hardware_no_hls", cleanup_files=True)
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
            if self.mode == "hardware_transcode" and source:
                self.restart_count += 1
                self.restart_reason = reason
                self.fallback_to_cpu = True
                self.hw_failure_reason = stderr_tail or reason
                self.last_fallback_reason = reason
                self.stop(reason=reason, cleanup_files=True)
                self.start(source, force_mode="fallback_transcode")
                return True
            self.stop(reason=reason, cleanup_files=True)
            self.last_error = stderr_tail
            return True

    def snapshot(self, viewers: int) -> dict:
        now = time.time()
        process_info = self.process_info()
        running = process_info["running_verified"]
        ready = self.is_ready()
        playlist_updated_at, last_segment_at = self._hls_update_times()
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
            **process_info,
            "running": running,
            "status": status,
            "mode": "copy" if self.mode == "copy" else "transcode",
            "requested_mode": self.requested_mode,
            "selected_mode": self.mode,
            "input_codec": self.input_codec,
            "input_resolution": f"{self.input_width}x{self.input_height}" if self.input_width and self.input_height else None,
            "input_width": self.input_width,
            "input_height": self.input_height,
            "input_fps": self.input_fps,
            "real_input_fps": self.input_fps,
            "output_fps": self.output_fps,
            "jitter_detected": self.jitter_detected,
            "unstable_source": self.unstable_source,
            "restart_reason": self.restart_reason,
            "auto_restart_allowed": self.auto_restart_allowed,
            "copy_eligible": self.copy_eligible,
            "browser_compatible": self.browser_compatible,
            "reason_for_transcode": self.reason_for_transcode,
            "high_cpu_risk": self.high_cpu_risk,
            "resource_limit": self.resource_limit,
            "hardware_accel_available": get_hardware_capabilities().get("hardware_accel_available"),
            "hw_backend": self.hw_backend,
            "hw_device": self.hw_device,
            "hwaccel_mode": self.hwaccel_mode,
            "selected_pipeline": self.selected_pipeline,
            "hw_decode": self.hw_decode,
            "hw_encode": self.hw_encode,
            "fallback_to_cpu": self.fallback_to_cpu,
            "hw_failure_reason": mask_rtsp_credentials(self.hw_failure_reason),
            "docker_device_access_ok": get_hardware_capabilities().get("docker_device_access_ok"),
            "hardware_misconfigured": get_hardware_capabilities().get("hardware_misconfigured"),
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
            "dup_frames": self.dup_frames,
            "drop_frames": self.drop_frames,
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
            "playlist_updated_at": playlist_updated_at,
            "last_segment_at": last_segment_at,
            "segment_count": len(list(self.stream_dir.glob("seg_*.ts"))) if self.stream_dir.exists() else 0,
            "segments_count": len(list(self.stream_dir.glob("seg_*.ts"))) if self.stream_dir.exists() else 0,
            "exit_code": self.last_exit_code,
            "fallback_reason": self.last_fallback_reason,
            "failure_reason": self.failure_reason,
            "last_error": mask_rtsp_credentials(self.last_error),
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

    def _running_transcodes_locked(self, exclude_key: StreamKey | None = None) -> int:
        count = 0
        for key, instance in self.streams.items():
            if exclude_key and key == exclude_key:
                continue
            if instance.mode != "copy" and instance.is_running():
                count += 1
        return count

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

    def start_or_reuse_stream(self, camera: Camera, stream: str) -> dict:
        with self.lock:
            instance = self._get_stream_locked(camera.id, stream)
            viewers = self._viewer_count_locked(camera.id, stream)
            transcode_limit = int(settings.live_max_concurrent_transcodes or 0)
            transcode_allowed = (
                True
                if transcode_limit <= 0
                else self._running_transcodes_locked(exclude_key=self._key(camera.id, stream)) < transcode_limit
            )

        result = instance.start(camera, transcode_allowed=transcode_allowed)
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
            self.viewers[viewer_id] = ViewerSession(
                id=viewer_id,
                camera_id=camera.id,
                stream=stream,
                created_at=now,
                last_seen=now,
            )
            viewers = self._viewer_count_locked(camera.id, stream)

        result = self.start_or_reuse_stream(camera, stream)
        if not result.get("ok"):
            if result.get("error_code") in {"no_rtsp_url", "resource_limit"}:
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
            "hardware_capabilities": hardware_capabilities_summary(),
        }


manager = StreamManager()


def start_cleanup_worker():
    manager.start_cleanup_worker()


def stop_cleanup_worker():
    manager.stop_cleanup_worker()


def stop_all_streams() -> int:
    return manager.stop_all_streams()
