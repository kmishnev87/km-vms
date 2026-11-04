from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from app.core.config import settings
from app.models.camera import Camera
from app.services.audit_log import create_event
from app.services.hardware import get_hardware_capabilities
from app.services.live_engine.process_log import BoundedProcessLog
from app.services.live_engine.pipeline_policy import PipelinePolicy
from app.services.live_engine.process_runtime import (
    process_info, hls_is_ready, hls_update_times,
    _pid_exists, _read_proc_cmdline, _read_proc_state,
)
from app.services.live_engine.ffmpeg import build_hls_command, choose_input_url, command_text, inspect_input_url, mask_rtsp_credentials, probe_video_codec

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
HARDWARE_STARTUP_TIMEOUT_SECONDS = 30
HARDWARE_MAIN_STARTUP_TIMEOUT_SECONDS = 45
HARDWARE_PROGRESS_GRACE_SECONDS = 120
HARDWARE_HARD_TIMEOUT_SECONDS = 180
STARTUP_PROGRESS_GRACE_SECONDS = 90
STARTUP_HARD_TIMEOUT_SECONDS = 180
SLOW_TRANSCODE_FAIL_SECONDS = 75
CPU_ESCALATION_SPEED_THRESHOLD = 0.9
CPU_ESCALATION_STABLE_SECONDS = 20
CPU_ESCALATION_COOLDOWN_SECONDS = 120
MAX_CPU_ESCALATIONS_PER_STREAM = 1
LIVE_MEDIA_STALE_SECONDS = 30
LIVE_RECOVERY_STABLE_SECONDS = 30
LIVE_RETRY_SECONDS = (5, 10, 20, 30, 60, 120)
LIVE_BLOCKED_RETRY_SECONDS = 300
LIVE_AUDIT_THROTTLE_SECONDS = 60
LIVE_AUDIT_THROTTLED_EVENTS = {
    "live.fallback",
    "live.ffmpeg_start_failed",
    "live.ffmpeg_exited",
}
FRAME_RE = re.compile(r"frame=\s*(?P<value>\d+)")
FPS_RE = re.compile(r"fps=\s*(?P<value>[\d.]+)")
TIME_RE = re.compile(r"time=(?P<value>\d+:\d+:\d+(?:\.\d+)?)")
SPEED_RE = re.compile(r"speed=\s*(?P<value>[\d.]+)x")
DUP_RE = re.compile(r"dup=\s*(?P<value>\d+)")
DROP_RE = re.compile(r"drop=\s*(?P<value>\d+)")


def _camera_source(camera: Camera):
    return SimpleNamespace(
        id=camera.id,
        name=camera.name,
        rtsp_main_url=camera.rtsp_main_url,
        rtsp_sub_url=camera.rtsp_sub_url,
        rtsp_transport=camera.rtsp_transport,
    )


def classify_live_failure(detail: str | None) -> str | None:
    value = str(detail or "").lower()
    # Frame counts, DTS values and hexadecimal FFmpeg addresses often contain
    # 401/403. Only an actual authentication message is evidence of bad access.
    if any(part in value for part in ("unauthorized", "forbidden", "authentication failed")) or re.search(
        r"(?:server returned|(?:http|rtsp)(?:/\d(?:\.\d)?)?|failed:?)\s+(?:401|403)\b", value
    ):
        return "auth_failed"
    if any(part in value for part in ("no route to host", "network is unreachable", "connection", "timed out", "timeout", "end of file", "i/o error")):
        return "camera_unreachable"
    return None











class StreamInstance:
    def __init__(self, camera_id: int, stream: str):
        self.camera_id = camera_id
        self.stream = stream
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.stderr_file = None
        self.stderr_path: Path | None = None
        self.stderr_log = BoundedProcessLog()
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
        self.input_audio_codec: str | None = None
        self.input_audio_channels: int | None = None
        self.input_audio_sample_rate: int | None = None
        self.audio_reason = "unknown"
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
        self.selected_backend = "cpu"
        self.configured_backend = "auto"
        self.effective_backend = "cpu"
        self.decision_source = "auto"
        self.decision_reason = ""
        self.heavy_stream = False
        self.copy_safe = False
        self.hardware_candidates: list[str] = []
        self.attempted_backends: list[str] = []
        self.failed_backends: dict[str, str] = {}
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
        self.cpu_slow_since: float | None = None
        self.last_cpu_escalation_at: float | None = None
        self.cpu_escalation_count = 0
        self.unstable_since: float | None = None
        self.stop_reason: str | None = None
        self.stopped_by_backend = False
        self.state_changed_at = time.time()
        self.last_state_transition = "initialized"
        self.audit_event_last_seen: dict[tuple, float] = {}
        self.viewer_managed = False
        self.start_cancelled = threading.Event()
        self.start_thread: threading.Thread | None = None
        self.retry_count = 0
        self.next_retry_at: float | None = None
        self.ever_ready = False
        self.healthy_since: float | None = None
        self.last_ready_at: float | None = None

    @property
    def sid(self) -> str:
        return f"{self.camera_id}_{self.stream}"

    @property
    def stream_dir(self) -> Path:
        return Path(settings.storage_previews) / "live" / str(self.camera_id) / self.stream

    @property
    def playlist_path(self) -> Path:
        return self.stream_dir / "index.m3u8"

    @property
    def default_stderr_path(self) -> Path:
        return self.stream_dir / "ffmpeg.log"

    def is_running(self) -> bool:
        return self.process_info()["running_verified"]

    def process_info(self) -> dict:
        return process_info(
            self.proc, self.started_at, pid_exists_fn=_pid_exists,
            read_cmdline=_read_proc_cmdline, read_state=_read_proc_state,
        )

    def is_ready(self) -> bool:
        return hls_is_ready(
            self.stream_dir, self.playlist_path, running=self.is_running(),
            stale_seconds=LIVE_MEDIA_STALE_SECONDS, now=time.time(),
        )

    def _hls_update_times(self) -> tuple[float | None, float | None]:
        return hls_update_times(self.stream_dir, self.playlist_path)

    def touch(self):
        self.last_access = time.time()

    def _camera_name(self) -> str:
        return str(getattr(self.camera_source, "name", None) or self.camera_id)

    def _audit_metadata(self, extra: dict | None = None) -> dict:
        payload = {
            "camera_id": self.camera_id,
            "camera_name": self._camera_name(),
            "stream_type": self.stream,
            "input_codec": self.input_codec,
            "input_width": self.input_width,
            "input_height": self.input_height,
            "input_fps": self.input_fps,
            "configured_backend": self.configured_backend,
            "effective_backend": self.effective_backend,
            "decision_source": self.decision_source,
            "decision_reason": self.decision_reason,
            "attempted_backends": self.attempted_backends,
            "failed_backends": self.failed_backends,
            "high_cpu_risk": self.high_cpu_risk,
            "last_speed": self.last_speed,
            "reason_for_transcode": self.reason_for_transcode,
            "selected_pipeline": self.selected_pipeline,
        }
        if extra:
            payload.update(extra)
        return payload

    def _audit_live_event(self, event_type: str, severity: str, message_ru: str, message_en: str, metadata: dict | None = None):
        payload = self._audit_metadata(metadata)
        if event_type in LIVE_AUDIT_THROTTLED_EVENTS:
            now = time.time()
            throttle_key = (
                event_type,
                severity,
                payload.get("reason"),
                payload.get("backend") or payload.get("effective_backend"),
                payload.get("fallback"),
            )
            last_seen = self.audit_event_last_seen.get(throttle_key)
            if last_seen and now - last_seen < LIVE_AUDIT_THROTTLE_SECONDS:
                return
            self.audit_event_last_seen[throttle_key] = now
        create_event(
            category="live",
            event_type=event_type,
            severity=severity,
            message_ru=message_ru,
            message_en=message_en,
            target_type="camera",
            target_id=self.camera_id,
            target_name=self._camera_name(),
            metadata=payload,
        )

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
            tail, rotated = self.stderr_log.read_tail(path, max_chars)
            if rotated:
                self.stderr_progress_size = 0
            return mask_rtsp_credentials(tail)
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

        if self.last_speed is not None and self.last_speed < CPU_ESCALATION_SPEED_THRESHOLD:
            self.cpu_slow_since = self.cpu_slow_since or time.time()
        else:
            self.cpu_slow_since = None

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

    def _hardware_progress_detected(self) -> bool:
        return self.mode == "hardware_transcode" and bool(
            self.progress_detected
            or self.last_ffmpeg_progress_at
            or (self.last_frame or 0) > 0
            or self.last_progress_time
        )

    def _hardware_readiness_elapsed(self) -> float:
        if self.mode != "hardware_transcode" or not self.started_at:
            return 0
        return round(time.time() - self.started_at, 2)

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
        self._audit_live_event(
            "live.ffmpeg_exited",
            "warning",
            f"FFmpeg завершил поток {self._camera_name()} / {self.stream}: {reason}",
            f"FFmpeg exited stream {self._camera_name()} / {self.stream}: {reason}",
            metadata={"reason": reason, "exit_code": self.last_exit_code},
        )
        self.proc = None
        self.stderr_file = None
        self._set_state_locked("failed", reason, failure_reason=reason)
        self.started_at = None
        self.start_deadline = None
        self.start_hard_deadline = None

        if cleanup_files:
            shutil.rmtree(self.stream_dir, ignore_errors=True)

    def _pipeline_policy(self) -> PipelinePolicy:
        return PipelinePolicy(
            stream=self.stream, failed_backends=self.failed_backends,
            attempted_backends=self.attempted_backends,
            last_fallback_reason=self.last_fallback_reason,
            decision_reason=self.decision_reason,
            force_stable_fps=self.force_stable_fps, high_cpu_risk=self.high_cpu_risk,
            audio_mode=self._audio_mode(), config=settings,
        )

    def _apply_pipeline_policy(self, policy: PipelinePolicy) -> None:
        for name, value in policy.changes().items():
            setattr(self, name, value)
        for args, kwargs in policy.audit_events:
            self._audit_live_event(*args, **kwargs)

    def _next_hw_backend(self, caps: dict) -> str | None:
        return self._pipeline_policy()._next_hw_backend(caps)

    def _mark_hw_backend_failed(self, reason: str, detail: str | None = None):
        backend = self.hw_backend
        if not backend:
            return
        failure = mask_rtsp_credentials(detail or reason or "hardware_backend_failed")
        self.failed_backends[backend] = failure
        self.hw_failure_reason = failure
        self.last_fallback_reason = reason
        self.fallback_to_cpu = False
        self._audit_live_event(
            "live.fallback",
            "warning",
            f"Live выполнил fallback для {self._camera_name()} / {self.stream}: {reason}",
            f"Live fallback for {self._camera_name()} / {self.stream}: {reason}",
            metadata={"backend": backend, "reason": reason, "detail": failure},
        )

    def _configured_backend(self, caps: dict | None = None) -> str:
        return self._pipeline_policy()._configured_backend(caps or get_hardware_capabilities())

    def _manual_hardware_backend(self, caps: dict | None = None) -> str | None:
        return self._pipeline_policy()._manual_hardware_backend(caps or get_hardware_capabilities())

    def _hardware_candidates(self, caps: dict | None = None) -> list[str]:
        return self._pipeline_policy()._hardware_candidates(caps or get_hardware_capabilities())

    def _is_heavy_stream(self, probe) -> bool:
        return self._pipeline_policy()._is_heavy_stream(probe)

    def _select_hardware_mode(self, backend: str, caps: dict, source: str, reason: str) -> str:
        policy = self._pipeline_policy()
        mode = policy._select_hardware_mode(backend, caps, source, reason)
        self._apply_pipeline_policy(policy)
        return mode

    def _start_hardware_or_cpu_locked(self, source, reason: str) -> bool:
        caps = get_hardware_capabilities()
        next_backend = None if self._configured_backend(caps) == "cpu" else self._next_hw_backend(caps)
        self.restart_count += 1
        self.restart_reason = reason
        self.last_fallback_reason = reason
        if next_backend:
            logger.warning(
                "Live Engine switching to hardware before CPU camera_id=%s stream=%s backend=%s reason=%s",
                self.camera_id,
                self.stream,
                next_backend,
                reason,
            )
            self.start(source, transcode_allowed=True, force_hw_backend=next_backend)
            return True

        self._audit_live_event(
            "live.fallback",
            "warning",
            f"Live выполнил fallback для {self._camera_name()} / {self.stream}: {reason}",
            f"Live fallback for {self._camera_name()} / {self.stream}: {reason}",
            metadata={"reason": reason, "fallback": "cpu"},
        )
        self.start(source, force_mode="fallback_transcode")
        return True

    def _maybe_escalate_cpu_to_hardware_locked(self, now: float, source) -> bool:
        if self.mode != "fallback_transcode" or not source:
            return False
        if self._configured_backend() == "cpu":
            self.decision_reason = self.decision_reason or "manual_cpu_selected"
            return False
        if not (self.heavy_stream or self.high_cpu_risk):
            return False
        if self.last_speed is None or self.last_speed >= CPU_ESCALATION_SPEED_THRESHOLD:
            return False
        if not self.cpu_slow_since or now - self.cpu_slow_since < CPU_ESCALATION_STABLE_SECONDS:
            return False
        if self.cpu_escalation_count >= MAX_CPU_ESCALATIONS_PER_STREAM:
            self.decision_reason = "cpu_slow_escalation_skipped:max_attempts"
            return False
        if self.last_cpu_escalation_at and now - self.last_cpu_escalation_at < CPU_ESCALATION_COOLDOWN_SECONDS:
            self.decision_reason = "cpu_slow_escalation_skipped:cooldown"
            return False

        caps = get_hardware_capabilities()
        next_backend = self._next_hw_backend(caps)
        if not next_backend:
            self.decision_reason = "cpu_slow_escalation_skipped:no_hardware_backend"
            return False

        self.cpu_escalation_count += 1
        self.last_cpu_escalation_at = now
        self.restart_count += 1
        self.restart_reason = "cpu_too_slow_escalation"
        self.last_fallback_reason = "cpu_too_slow_escalation"
        self.decision_source = "escalation"
        self.decision_reason = f"CPU speed {self.last_speed}x below realtime threshold; trying {next_backend}"
        logger.warning(
            "Live Engine CPU too slow, escalating to hardware camera_id=%s stream=%s speed=%s backend=%s",
            self.camera_id,
            self.stream,
            self.last_speed,
            next_backend,
        )
        self._audit_live_event(
            "live.cpu_escalation",
            "warning",
            f"Live переключает {self._camera_name()} / {self.stream} с CPU на hardware: скорость ниже realtime",
            f"Live switches {self._camera_name()} / {self.stream} from CPU to hardware: speed below realtime",
            metadata={"last_speed": self.last_speed, "backend": next_backend},
        )
        self.stop(reason="cpu_too_slow_escalation", cleanup_files=True)
        self.start(source, transcode_allowed=True, force_hw_backend=next_backend)
        return True

    def _retry_next_backend_or_cpu_locked(self, source, reason: str, detail: str | None = None) -> bool:
        self._mark_hw_backend_failed(reason, detail)
        caps = get_hardware_capabilities()
        next_backend = self._next_hw_backend(caps)
        self.restart_count += 1
        self.restart_reason = reason
        if next_backend:
            logger.warning(
                "Live Engine hardware backend failed, trying next backend camera_id=%s stream=%s failed_backend=%s next_backend=%s reason=%s",
                self.camera_id,
                self.stream,
                self.hw_backend,
                next_backend,
                mask_rtsp_credentials(detail or reason),
            )
            self.start(source, transcode_allowed=True, force_hw_backend=next_backend)
            return True

        self.fallback_to_cpu = True
        self.selected_backend = "cpu"
        self.effective_backend = "cpu"
        self.decision_source = "fallback"
        self.decision_reason = f"all hardware backends failed; CPU fallback after {reason}"
        logger.warning(
            "Live Engine all hardware backends failed, falling back to CPU camera_id=%s stream=%s failed_backends=%s",
            self.camera_id,
            self.stream,
            self.failed_backends,
        )
        self._audit_live_event(
            "live.fallback",
            "warning",
            f"Live выполнил fallback для {self._camera_name()} / {self.stream}: {reason}",
            f"Live fallback for {self._camera_name()} / {self.stream}: {reason}",
            metadata={"reason": reason, "failed_backends": self.failed_backends, "fallback": "cpu"},
        )
        self.start(source, force_mode="fallback_transcode")
        return True

    def _choose_mode(
        self,
        camera: Camera,
        input_url: str,
        force_mode: str | None = None,
        force_hw_backend: str | None = None,
    ) -> str:
        probe = probe_video_codec(input_url, (camera.rtsp_transport or "tcp").lower())
        caps = get_hardware_capabilities()
        policy = self._pipeline_policy()
        mode = policy._choose_mode(camera, probe, caps, force_mode, force_hw_backend)
        self._apply_pipeline_policy(policy)
        return mode

    def start(
        self,
        camera: Camera,
        force_mode: str | None = None,
        transcode_allowed: bool = True,
        force_hw_backend: str | None = None,
    ) -> dict:
        with self.lock:
            if self.viewer_managed and self.start_cancelled.is_set():
                return {"ok": False, "error_code": "client_stop"}
            self.touch()
            if self.is_running():
                return self.snapshot(viewers=0)

            if self.proc:
                if self.proc.poll() is not None:
                    self._mark_process_exit_locked(reason="restart_dead_process", cleanup_files=True)
                else:
                    self.stop(reason="restart_running_process", cleanup_files=True)

            self.next_retry_at = None
            self.last_ready_at = None
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
            self.input_audio_codec = None
            self.input_audio_channels = None
            self.input_audio_sample_rate = None
            self.audio_reason = "unknown"
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
            self.selected_backend = "cpu"
            self.configured_backend = "auto"
            self.effective_backend = "cpu"
            self.decision_source = "auto"
            self.decision_reason = ""
            self.heavy_stream = False
            self.copy_safe = False
            self.hardware_candidates = []
            self.hw_decode = False
            self.hw_encode = False
            self.cpu_slow_since = None
            if force_mode is None and force_hw_backend is None:
                self.attempted_backends = []
                self.failed_backends = {}
                self.fallback_to_cpu = False
                self.hw_failure_reason = None
                self.cpu_escalation_count = 0
                self.last_cpu_escalation_at = None
            else:
                self.fallback_to_cpu = force_mode == "fallback_transcode" and bool(self.failed_backends)
            self.mode = self._choose_mode(
                camera,
                input_url,
                force_mode=force_mode,
                force_hw_backend=force_hw_backend,
            )
            if self.viewer_managed and self.start_cancelled.is_set():
                self._set_state_locked("stopped", "client_stop")
                return {"ok": False, "error_code": "client_stop"}
            input_failure = classify_live_failure(self.last_error)
            if input_failure:
                self._schedule_retry_locked(input_failure)
                return {"ok": False, "error_code": input_failure, **self.snapshot(viewers=0)}
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
                if self.viewer_managed and self.start_cancelled.is_set():
                    self.stderr_file.close()
                    self.stderr_file = None
                    self._set_state_locked("stopped", "client_stop")
                    return {"ok": False, "error_code": "client_stop"}
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
                    self._audit_live_event(
                        "live.ffmpeg_start_failed",
                        "error",
                        f"FFmpeg не запустил поток {self._camera_name()} / {self.stream}: {self.last_error}",
                        f"FFmpeg failed to start stream {self._camera_name()} / {self.stream}: {self.last_error}",
                        metadata={"error": self.last_error, "mode": self.mode},
                    )
                    self._retry_next_backend_or_cpu_locked(camera, "hardware_start_failed", self.last_error)
                    return self.snapshot(viewers=0)
                logger.exception(
                    "Live Engine failed to start ffmpeg camera_id=%s stream=%s mode=%s command=%s",
                    camera.id,
                    self.stream,
                    self.mode,
                    self.cmd_text,
                )
                self._audit_live_event(
                    "live.ffmpeg_start_failed",
                    "error",
                    f"FFmpeg не запустил поток {self._camera_name()} / {self.stream}: {self.last_error}",
                    f"FFmpeg failed to start stream {self._camera_name()} / {self.stream}: {self.last_error}",
                    metadata={"error": self.last_error, "mode": self.mode},
                )
                return {"ok": False, "error": str(exc), **self.snapshot(viewers=0)}

            self._set_state_locked("starting", "ffmpeg_started", failure_reason=None)
            self.started_at = time.time()
            initial_timeout = max(int(settings.live_start_timeout_seconds), STARTUP_INITIAL_TIMEOUT_SECONDS)
            if self.mode == "hardware_transcode":
                hardware_timeout = (
                    HARDWARE_MAIN_STARTUP_TIMEOUT_SECONDS
                    if self.stream == "main"
                    else HARDWARE_STARTUP_TIMEOUT_SECONDS
                )
                self.start_deadline = self.started_at + hardware_timeout
                self.start_hard_deadline = self.started_at + HARDWARE_HARD_TIMEOUT_SECONDS
            else:
                self.start_deadline = self.started_at + initial_timeout
                self.start_hard_deadline = self.started_at + max(initial_timeout * 2, STARTUP_HARD_TIMEOUT_SECONDS)
            self.last_exit_code = None
            self.last_error = None
            self.next_retry_at = None
            self.healthy_since = None

            logger.info(
                "Live Engine started stream camera_id=%s stream=%s pid=%s mode=%s fallback_reason=%s",
                camera.id,
                self.stream,
                self.proc.pid,
                self.mode,
                self.last_fallback_reason,
            )
            backend_label = self.effective_backend or self.selected_backend or self.mode
            self._audit_live_event(
                "live.backend_selected",
                "info",
                f"Live выбрал кодирование {backend_label} для {self._camera_name()} / {self.stream}",
                f"Live selected {backend_label} for {self._camera_name()} / {self.stream}",
                metadata={"backend": backend_label, "mode": self.mode},
            )
            self._audit_live_event(
                "live.stream_started",
                "info",
                f"Live запустил поток {self._camera_name()} / {self.stream} через {backend_label}",
                f"Live started stream {self._camera_name()} / {self.stream} via {backend_label}",
                metadata={"pid": self.proc.pid if self.proc else None, "backend": backend_label, "mode": self.mode},
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
            self._audit_live_event(
                "live.stream_stopped",
                "info",
                f"Live остановил поток {self._camera_name()} / {self.stream}",
                f"Live stopped stream {self._camera_name()} / {self.stream}",
                metadata={"reason": reason, "exit_code": exit_code},
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

    def _schedule_retry_locked(self, reason: str):
        if self.next_retry_at is not None or self.start_cancelled.is_set():
            return
        self.retry_count += 1
        delay = LIVE_RETRY_SECONDS[min(self.retry_count - 1, len(LIVE_RETRY_SECONDS) - 1)]
        if reason in {"auth_failed", "no_rtsp_url", "resource_limit"}:
            delay = LIVE_BLOCKED_RETRY_SECONDS
        self.next_retry_at = time.time() + delay
        self.healthy_since = None
        self._set_state_locked("restarting", "retry_scheduled", failure_reason=reason)
        self._audit_live_event(
            "live.retry_scheduled", "warning",
            f"Live восстанавливает поток {self._camera_name()} / {self.stream}",
            f"Live is recovering {self._camera_name()} / {self.stream}",
            metadata={"reason": reason, "retry_count": self.retry_count, "retry_after_seconds": delay},
        )

    def maintain_startup(self) -> bool:
        with self.lock:
            if self.viewer_managed and self.start_cancelled.is_set():
                return False
            stderr_tail = self._read_log_tail()
            fatal_error = self._detect_fatal_error(stderr_tail)
            process_info = self.process_info()
            if self.is_running():
                self._detect_progress_locked(stderr_tail)

            if self.is_ready():
                source = self.camera_source
                if self._maybe_escalate_cpu_to_hardware_locked(time.time(), source):
                    return True
                if self.jitter_detected:
                    self.unstable_source = True
                self._set_state_locked("ready", "hls_ready", failure_reason=None)
                now = time.time()
                self.ever_ready = True
                playlist_at, segment_at = self._hls_update_times()
                self.last_ready_at = min(playlist_at, segment_at) if playlist_at and segment_at else now
                if self.healthy_since is None:
                    self.healthy_since = now
                if self.retry_count and now - self.healthy_since >= LIVE_RECOVERY_STABLE_SECONDS:
                    self.retry_count = 0
                    self.next_retry_at = None
                    self._audit_live_event(
                        "live.recovered", "info",
                        f"Онлайн-поток {self._camera_name()} восстановлен",
                        f"Live stream {self._camera_name()} recovered",
                    )
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
                source_failure = classify_live_failure(self.last_error or stderr_tail)
                if source_failure:
                    self._schedule_retry_locked(source_failure)
                    return True
                if previous_mode == "copy" and source:
                    self.restart_reason = "copy_failed"
                    self.last_fallback_reason = "copy_failed"
                    return self._start_hardware_or_cpu_locked(source, "copy_failed")
                if previous_mode == "hardware_transcode" and source:
                    return self._retry_next_backend_or_cpu_locked(
                        source,
                        "hardware_failed",
                        self.last_error or stderr_tail or reason,
                    )
                self._schedule_retry_locked("process_exit")
                return True

            if not self.is_running() or not self.start_deadline:
                return False

            now = time.time()
            source = self.camera_source
            # FFmpeg progress or an incomplete playlist is not playable HLS.
            # This deadline covers startup only; a healthy viewer has no limit.
            hard_deadline = self.start_hard_deadline or ((self.started_at or now) + STARTUP_HARD_TIMEOUT_SECONDS)
            if self.last_ready_at is None and now > hard_deadline:
                previous_mode = self.mode
                reason = "startup_timeout_no_hls"
                self.stop(reason=reason, cleanup_files=True)
                if previous_mode == "hardware_transcode" and source:
                    return self._retry_next_backend_or_cpu_locked(source, reason, stderr_tail or reason)
                self._schedule_retry_locked(reason)
                return True
            if self.last_ready_at is not None and now - self.last_ready_at > LIVE_MEDIA_STALE_SECONDS:
                self.stop(reason="stream_stalled", cleanup_files=True)
                self._schedule_retry_locked("stream_stalled")
                return True
            source_failure = classify_live_failure(fatal_error or stderr_tail)
            if source_failure:
                self.stop(reason=source_failure, cleanup_files=True)
                self._schedule_retry_locked(source_failure)
                return True
            if self.mode == "hardware_transcode" and fatal_error and source:
                self.stop(reason="hardware_fatal_error", cleanup_files=True)
                return self._retry_next_backend_or_cpu_locked(
                    source,
                    "hardware_fatal_error",
                    stderr_tail or fatal_error,
                )

            if self.last_ready_at is not None:
                # A brief readiness loss of an established stream is handled
                # by the media-staleness window above, not its old start timer.
                return False

            if now <= self.start_deadline:
                return False

            if fatal_error:
                self.stop(reason="rtsp_error", cleanup_files=True)
                self.last_error = stderr_tail
                return True

            if self.mode == "copy" and source:
                logger.warning(
                    "Live Engine copy mode timed out before HLS ready, switching to hardware/CPU transcode camera_id=%s stream=%s",
                    self.camera_id,
                    self.stream,
                )
                self.restart_reason = "copy_not_ready"
                self.last_fallback_reason = "copy_not_ready"
                self.stop(reason="copy_not_ready", cleanup_files=True)
                return self._start_hardware_or_cpu_locked(source, "copy_not_ready")

            if self.mode == "hardware_transcode" and source:
                if self._hardware_progress_detected():
                    last_progress = self.last_ffmpeg_progress_at or self.started_at or now
                    if now - last_progress <= HARDWARE_PROGRESS_GRACE_SECONDS:
                        self._set_state_locked("starting", "hardware_ffmpeg_progress", failure_reason=None)
                        return False

                if self.playlist_path.exists():
                    self._set_state_locked("starting", "hardware_hls_partial_output", failure_reason=None)
                    return False

                self.stop(reason="hardware_no_hls", cleanup_files=True)
                return self._retry_next_backend_or_cpu_locked(
                    source,
                    "hardware_no_hls",
                    stderr_tail or "hardware_transcode did not produce HLS and no ffmpeg progress was detected",
                )

            last_progress = self.last_ffmpeg_progress_at or self.started_at or now
            hard_deadline = self.start_hard_deadline or (now + STARTUP_HARD_TIMEOUT_SECONDS)
            if self._maybe_escalate_cpu_to_hardware_locked(now, source):
                return True
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
                if self._hardware_progress_detected():
                    self._set_state_locked("starting", "hardware_progress_no_hls", failure_reason=None)
                    return False
                self.stop(reason=reason, cleanup_files=True)
                return self._retry_next_backend_or_cpu_locked(source, reason, stderr_tail or reason)
            self.stop(reason=reason, cleanup_files=True)
            self.last_error = stderr_tail
            return True

    def snapshot(self, viewers: int) -> dict:
        now = time.time()
        process_info = self.process_info()
        running = process_info["running_verified"]
        ready = self.is_ready()
        playlist_updated_at, last_segment_at = self._hls_update_times()
        try:
            playlist_size = self.playlist_path.stat().st_size
        except OSError:
            playlist_size = 0
        if running and ready:
            status = "ready"
        elif running:
            status = self.status if self.status in {"starting", "restarting"} else "starting"
        else:
            status = self.status
        audio_mode = self._audio_mode()
        audio_enabled = audio_mode not in {"none", "off", "disable", "disabled"}
        audio_available = bool(audio_enabled and self.input_audio_codec)
        return {
            "stream_key": self.sid,
            "camera_id": self.camera_id,
            "viewer_managed": self.viewer_managed,
            "retry_count": self.retry_count,
            "next_retry_at": self.next_retry_at,
            "ever_ready": self.ever_ready,
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
            "audio_mode": "transcode" if audio_mode == "transcode" else audio_mode,
            "audio_enabled": audio_enabled,
            "audio_available": audio_available,
            "input_audio_codec": self.input_audio_codec,
            "input_audio_channels": self.input_audio_channels,
            "input_audio_sample_rate": self.input_audio_sample_rate,
            "audio_reason": "input_has_audio" if audio_available else self.audio_reason,
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
            "selected_backend": self.selected_backend,
            "configured_backend": self.configured_backend,
            "effective_backend": self.effective_backend,
            "decision_source": self.decision_source,
            "decision_reason": self.decision_reason,
            "copy_safe": self.copy_safe,
            "heavy_stream": self.heavy_stream,
            "hardware_candidates": self.hardware_candidates,
            "attempted_backends": self.attempted_backends,
            "failed_backends": self.failed_backends,
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
            "hardware_progress_detected": self._hardware_progress_detected(),
            "hardware_readiness_elapsed": self._hardware_readiness_elapsed(),
            "last_frame": self.last_frame,
            "last_fps": self.last_fps,
            "last_speed": self.last_speed,
            "dup_frames": self.dup_frames,
            "drop_frames": self.drop_frames,
            "last_progress_time": self.last_progress_time,
            "speed_state": self._speed_state(),
            "too_slow_seconds": round(now - self.too_slow_since, 2) if self.too_slow_since else 0,
            "cpu_slow_seconds": round(now - self.cpu_slow_since, 2) if self.cpu_slow_since else 0,
            "cpu_escalation_count": self.cpu_escalation_count,
            "stop_reason": self.stop_reason,
            "stopped_by_backend": self.stopped_by_backend,
            "state_changed_at": self.state_changed_at,
            "last_state_transition": self.last_state_transition,
            "ready": ready,
            "playlist_path": str(self.playlist_path),
            "playlist_exists": self.playlist_path.exists(),
            "playlist_size": playlist_size,
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

    def _audio_mode(self) -> str:
        value = str(settings.live_audio_mode or "aac").strip().lower()
        if value in {"none", "off", "disable", "disabled", "copy", "aac", "transcode"}:
            return value
        return "aac"
