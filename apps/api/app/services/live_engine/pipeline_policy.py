"""Pipeline decision isolated from viewers, process launch and retry lifecycle."""
from __future__ import annotations
import logging
from app.models.camera import Camera
from .ffmpeg import mask_rtsp_credentials, select_output_fps

logger = logging.getLogger(__name__)


class PipelinePolicy:
    RESULT_FIELDS = ["attempted_backends","audio_reason","browser_compatible","configured_backend","copy_eligible","copy_safe","decision_reason","decision_source","effective_backend","failed_backends","fallback_to_cpu","hardware_candidates","heavy_stream","high_cpu_risk","hw_backend","hw_decode","hw_device","hw_encode","hw_failure_reason","hwaccel_mode","input_audio_channels","input_audio_codec","input_audio_sample_rate","input_codec","input_fps","input_height","input_width","jitter_detected","last_error","last_fallback_reason","reason_for_transcode","selected_backend","selected_pipeline","unstable_source"]

    def __init__(self, *, stream, failed_backends, attempted_backends,
                 last_fallback_reason, decision_reason, force_stable_fps,
                 high_cpu_risk, audio_mode, config):
        self.stream = stream
        self.failed_backends = dict(failed_backends)
        self.attempted_backends = list(attempted_backends)
        self.last_fallback_reason = last_fallback_reason
        self.decision_reason = decision_reason
        self.force_stable_fps = force_stable_fps
        self.high_cpu_risk = high_cpu_risk
        self._audio_mode_value = audio_mode
        self._settings = config
        self.audit_events = []

    def _audio_mode(self):
        return self._audio_mode_value

    def _audit_live_event(self, *args, **kwargs):
        self.audit_events.append((args, kwargs))

    def changes(self):
        return {name: getattr(self, name) for name in self.RESULT_FIELDS if hasattr(self, name)}

    def _next_hw_backend(self, caps: dict) -> str | None:
        available = caps.get("available_backends") or []
        priority = caps.get("backend_priority") or available
        for backend in priority:
            if backend in available and backend not in self.failed_backends:
                return backend
        return None

    def _configured_backend(self, caps: dict | None = None) -> str:
        caps = caps or {}
        return str((caps.get("config") or {}).get("backend") or "auto").lower()

    def _manual_hardware_backend(self, caps: dict | None = None) -> str | None:
        configured = self._configured_backend(caps)
        return configured if configured in {"qsv", "vaapi", "nvenc", "amf"} else None

    def _hardware_candidates(self, caps: dict | None = None) -> list[str]:
        caps = caps or {}
        available = caps.get("available_backends") or []
        priority = caps.get("backend_priority") or available
        return [backend for backend in priority if backend in available and backend not in self.failed_backends]

    def _is_heavy_stream(self, probe) -> bool:
        return bool(
            probe.codec in {"hevc", "h265"}
            or not probe.safe_for_copy
            or self.stream == "main"
            or (probe.width or 0) >= 1920
            or (probe.height or 0) >= 1080
            or (probe.width or 0) >= 2560
            or (probe.fps or 0) >= 25
            or self.high_cpu_risk
        )

    def _select_hardware_mode(self, backend: str, caps: dict, source: str, reason: str) -> str:
        self.hw_backend = backend
        self.hw_device = caps.get("render_device") or self._settings.live_hwaccel_device
        self.hwaccel_mode = self._settings.live_hwaccel_mode
        self.hw_decode = True
        self.hw_encode = True
        self.fallback_to_cpu = False
        self.hw_failure_reason = None
        self.selected_pipeline = "hardware_transcode"
        self.selected_backend = backend
        self.effective_backend = backend
        self.decision_source = source
        self.decision_reason = reason
        if backend not in self.attempted_backends:
            self.attempted_backends.append(backend)
        self.last_fallback_reason = None
        self.reason_for_transcode = reason
        if source == "auto":
            self._audit_live_event(
                "live.auto_decision",
                "info",
                f"Auto hardware выбрал {backend}: {reason}",
                f"Auto hardware selected {backend}: {reason}",
                metadata={"backend": backend, "reason": reason},
            )
        return "hardware_transcode"

    def _choose_mode(
        self,
        camera: Camera,
        probe,
        caps: dict,
        force_mode: str | None = None,
        force_hw_backend: str | None = None,
    ) -> str:
        configured_backend = self._configured_backend(caps)
        hw_mode = (self._settings.live_hwaccel_mode or "auto").lower()
        requested_policy = (self._settings.live_video_codec or "auto").lower()
        self.last_error = mask_rtsp_credentials(probe.error)
        self.input_codec = probe.codec
        self.input_width = probe.width
        self.input_height = probe.height
        self.input_fps = probe.fps
        self.input_audio_codec = probe.audio_codec
        self.input_audio_channels = probe.audio_channels
        self.input_audio_sample_rate = probe.audio_sample_rate
        if self._audio_mode() in {"none", "off", "disable", "disabled"}:
            self.audio_reason = "disabled"
        elif probe.audio_codec:
            self.audio_reason = "input_has_audio"
        elif probe.audio_error:
            self.audio_reason = "probe_failed"
        else:
            self.audio_reason = "input_no_audio"
        self.output_fps, forced_fps = select_output_fps(probe.fps, self.force_stable_fps)
        self.unstable_source = bool(forced_fps)
        self.jitter_detected = bool(forced_fps)
        self.copy_eligible = bool(probe.safe_for_copy)
        self.copy_safe = bool(probe.safe_for_copy)
        self.browser_compatible = probe.codec == "h264"
        self.high_cpu_risk = bool(
            probe.codec in {"hevc", "h265"}
            or not probe.safe_for_copy
            or self.stream == "main"
            or (probe.width or 0) >= 1920
            or (probe.height or 0) >= 1080
            or (probe.width or 0) >= 2560
            or (probe.width or 0) >= 3840
            or (probe.fps or 0) >= 25
        )
        self.heavy_stream = self._is_heavy_stream(probe)
        self.hardware_candidates = self._hardware_candidates(caps)
        self.configured_backend = configured_backend
        self.hw_device = caps.get("render_device") or self._settings.live_hwaccel_device
        self.hwaccel_mode = self._settings.live_hwaccel_mode

        if force_mode == "fallback_transcode":
            self.last_fallback_reason = self.last_fallback_reason or "forced_fallback"
            self.reason_for_transcode = "forced_fallback"
            self.selected_pipeline = "cpu_transcode"
            self.selected_backend = "cpu"
            self.effective_backend = "cpu"
            self.decision_source = "fallback" if self.failed_backends else ("user_forced" if configured_backend == "cpu" else "auto")
            if self.failed_backends:
                failed = ", ".join(self.failed_backends.keys())
                self.decision_reason = f"CPU fallback selected after hardware failures: {failed}"
            else:
                self.decision_reason = self.decision_reason or "CPU fallback selected"
            self.hw_backend = None
            self.hw_decode = False
            self.hw_encode = False
            self.fallback_to_cpu = bool(self.failed_backends)
            return "fallback_transcode"

        if force_mode == "copy":
            self.selected_pipeline = "copy"
            self.selected_backend = "copy"
            self.effective_backend = "copy"
            self.decision_source = "auto"
            self.decision_reason = "copy mode forced"
            return "copy"

        manual_backend = self._manual_hardware_backend(caps)
        preferred_backend = None
        if hw_mode not in {"off", "disabled", "false", "0"} and configured_backend != "cpu":
            preferred_backend = force_hw_backend or manual_backend or self._next_hw_backend(caps)

        if force_hw_backend:
            if force_hw_backend in self.hardware_candidates or force_hw_backend in (caps.get("available_backends") or []):
                return self._select_hardware_mode(
                    force_hw_backend,
                    caps,
                    "fallback" if self.failed_backends else "auto",
                    f"retrying hardware backend {force_hw_backend} before CPU",
                )
            self.failed_backends[force_hw_backend] = "requested_backend_not_available"
            preferred_backend = self._next_hw_backend(caps)

        if manual_backend:
            if manual_backend in (caps.get("available_backends") or []):
                return self._select_hardware_mode(
                    manual_backend,
                    caps,
                    "user_forced",
                    f"manual hardware backend selected: {manual_backend}",
                )
            self.hw_failure_reason = f"manual hardware backend {manual_backend} is not available"
            preferred_backend = self._next_hw_backend(caps)

        if configured_backend == "cpu":
            self.reason_for_transcode = "manual_cpu_selected"
            self.selected_pipeline = "cpu_transcode"
            self.selected_backend = "cpu"
            self.effective_backend = "cpu"
            self.decision_source = "user_forced"
            self.decision_reason = "manual CPU backend selected"
            self.hw_backend = None
            self.hw_decode = False
            self.hw_encode = False
            return "fallback_transcode"

        if probe.safe_for_copy and not force_hw_backend and not manual_backend:
            self.last_fallback_reason = None
            self.reason_for_transcode = None
            self.selected_pipeline = "copy"
            self.selected_backend = "copy"
            self.effective_backend = "copy"
            self.decision_source = "auto"
            self.decision_reason = "H264 stream is browser-safe; using copy mode"
            return "copy"

        if (
            preferred_backend in {"qsv", "vaapi", "nvenc", "amf"}
            and caps.get("hardware_accel_available")
            and (self.heavy_stream or not probe.safe_for_copy or self.high_cpu_risk)
        ):
            reasons = []
            if probe.codec in {"hevc", "h265"}:
                reasons.append(f"codec={probe.codec}")
            if not probe.safe_for_copy:
                reasons.append("not_copy_safe")
            if self.stream == "main":
                reasons.append("main_stream")
            if self.input_width and self.input_height:
                reasons.append(f"resolution={self.input_width}x{self.input_height}")
            if self.input_fps:
                reasons.append(f"fps={self.input_fps}")
            return self._select_hardware_mode(
                preferred_backend,
                caps,
                "auto",
                "heavy stream; " + ", ".join(reasons or ["hardware backend available"]),
            )

        if self._settings.live_transcode or requested_policy in {"libx264", "h264", "transcode", "fallback_transcode"}:
            self.last_fallback_reason = "forced_transcode" if self._settings.live_transcode else f"settings_codec:{requested_policy}"
            self.reason_for_transcode = self.last_fallback_reason
            self.selected_pipeline = "cpu_transcode"
            self.selected_backend = "cpu"
            self.effective_backend = "cpu"
            self.decision_source = "auto"
            self.decision_reason = self.last_fallback_reason
            return "fallback_transcode"

        self.last_fallback_reason = f"codec_not_safe_for_copy:{probe.codec or 'unknown'}"
        self.reason_for_transcode = self.last_fallback_reason
        self.hw_backend = None
        self.hw_decode = False
        self.hw_encode = False
        self.selected_pipeline = "cpu_transcode"
        self.selected_backend = "cpu"
        self.effective_backend = "cpu"
        self.decision_source = "fallback" if self.failed_backends else "auto"
        self.decision_reason = "CPU selected because no runtime-ok hardware backend is available"
        self.fallback_to_cpu = bool(self.failed_backends)
        if (self.heavy_stream or probe.codec in {"hevc", "h265"}) and hw_mode not in {"off", "disabled", "false", "0"}:
            warnings = caps.get("warnings") or []
            errors = caps.get("errors") or []
            failed = [f"{backend}:{reason}" for backend, reason in self.failed_backends.items()]
            self.hw_failure_reason = "; ".join([*failed, *warnings, *errors])[:1200] or "hardware_accel_not_available"
        logger.warning(
            "Live Engine fallback selected camera_id=%s stream=%s codec=%s probe_error=%s",
            camera.id,
            self.stream,
            probe.codec,
            probe.error,
        )
        return "fallback_transcode"
