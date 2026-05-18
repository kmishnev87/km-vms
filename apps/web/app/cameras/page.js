"use client";

import { useEffect, useRef, useState } from "react";
import Layout from "../../components/Layout";
import OperatorProblemBanners from "../../components/OperatorProblemBanners";
import { apiFetch } from "../../lib/api";
import { useCurrentUser } from "../../lib/currentUser";
import {
  applyCameraFormPatch,
  loadCamerasViewMode,
  isRtspPortManualOverrideForEdit,
  saveCamerasViewMode,
  smartRtspPort,
} from "../../lib/cameraStage16";
import { useI18n, useLocaleText } from "../../lib/i18n";

const initialForm = {
  name: "",
  enabled: true,
  protocol: "rtsp",
  host: "",
  port: 554,
  username: "",
  password: "",
  rtsp_main_url: "",
  rtsp_sub_url: "",
  rtsp_host: "",
  rtsp_port: 554,
  rtsp_port_manually_set: false,
  rtsp_transport: "tcp",
  onvif_path: "",
  onvif_profile_token: "",
  onvif_channel_id: "",
  recording_mode: "always",
  default_live_stream: "main",
  default_record_stream: "main",
  segment_minutes: 5,
  retention_days: 30,
  storage_quota_gb: 50,
  preview_token: null,
  validation_token: null,
  onvif_probe_token: null,
  manual_confirm_unverified: false,
};

const initialOnvifConfig = {
  codec: "",
  resolution: "",
  fps: "",
  bitrate: "",
  iframe_interval: "",
  quality: "",
  supported: {},
};

const PREVIEW_SENSITIVE_FIELDS = new Set([
  "protocol",
  "host",
  "port",
  "username",
  "password",
  "rtsp_main_url",
  "rtsp_sub_url",
  "rtsp_host",
  "rtsp_port",
  "rtsp_transport",
  "onvif_path",
  "onvif_profile_token",
  "onvif_channel_id",
]);

function prettyRtspValue(value) {
  if (!value) return "";
  if (value.toLowerCase().startsWith("rtsp://")) {
    try {
      const url = new URL(value);
      return `${url.pathname || ""}${url.search || ""}`;
    } catch {
      return value;
    }
  }
  return value;
}

function rtspHostFromValue(value) {
  if (!value || !String(value).toLowerCase().startsWith("rtsp://")) return "";
  try {
    return new URL(value).hostname || "";
  } catch {
    return "";
  }
}

function rtspPortFromValue(value) {
  if (!value || !String(value).toLowerCase().startsWith("rtsp://")) return "";
  try {
    return new URL(value).port || "";
  } catch {
    return "";
  }
}

function profileResolution(profile) {
  const width = profile?.video?.width;
  const height = profile?.video?.height;
  return width && height ? `${width}x${height}` : "-";
}

function hasValue(value) {
  return value !== undefined && value !== null && value !== "";
}

function hasSubStream(source) {
  return Boolean(String(source?.rtsp_sub_url || "").trim());
}

function availableCameraStreams(source) {
  const streams = [{ key: "main", label: "Main" }];
  if (hasSubStream(source)) {
    streams.push({ key: "sub", label: "Sub" });
  }
  return streams;
}

function normalizeCameraStreamDefaults(source) {
  const hasSub = hasSubStream(source);
  return {
    ...source,
    default_live_stream: hasSub && source.default_live_stream === "sub" ? "sub" : "main",
    default_record_stream: hasSub && source.default_record_stream === "sub" ? "sub" : "main",
  };
}

function rtspReachableHost(source) {
  return String(source?.rtsp_host || source?.host || "").trim();
}

function rtspReachablePort(source) {
  const value = Number(source?.rtsp_port || 0);
  return value || 554;
}

function writableSettings(supported = {}) {
  return Object.values(supported || {}).filter((item) => item?.writable);
}

function canApplyOnvifSettings(config, profileToken) {
  return Boolean(profileToken && writableSettings(config?.supported).length);
}

function profileSettingMeta(supported = {}, key) {
  return supported?.[key] || { readable: false, writable: false, options: [], range: null };
}

function profileSettingDisplayValue(config, key) {
  const value = config?.[key];
  if (value === undefined || value === null || value === "") return "";
  return String(value);
}

function profileSettingState(meta, value, requiresOptions = false) {
  const hasOptions = Array.isArray(meta?.options) && meta.options.length > 0;
  if (requiresOptions && !hasOptions) {
    if (meta?.readable || value) return "readonly";
    return "unavailable";
  }
  if (meta?.writable) return "editable";
  if (meta?.readable || value) return "readonly";
  return "unavailable";
}

function configVideo(profile) {
  const current = profile?.current || profile?.settings || profile?.config || {};
  const video = profile?.video || {};
  return {
    codec: current.codec || video.codec,
    resolution: current.resolution || profileResolution(profile),
    fps: current.fps || video.fps,
    bitrate: current.bitrate || video.bitrate_limit,
    iframe_interval: current.iframe_interval || video.encoding_interval,
    quality: current.quality || video.quality,
  };
}

function profileVideoSummary(profile, copy = {}) {
  const video = configVideo(profile);
  const parts = [];
  if (hasValue(video.codec)) parts.push(video.codec);
  if (hasValue(video.resolution) && video.resolution !== "-") parts.push(video.resolution);
  if (hasValue(video.fps)) parts.push(`fps ${video.fps}`);
  if (parts.length) return `${copy.onvifVideo || copy.video || ""}: ${parts.join(" · ")}`;
  if (profile?.rtsp_probe?.video) {
    const probe = profile.rtsp_probe.video;
    const probeParts = [];
    if (hasValue(probe.codec)) probeParts.push(probe.codec);
    if (hasValue(probe.width) && hasValue(probe.height)) probeParts.push(`${probe.width}x${probe.height}`);
    if (hasValue(probe.fps)) probeParts.push(`fps ${probe.fps}`);
    if (probeParts.length) return `${copy.rtspProbeVideo || copy.video || ""}: ${probeParts.join(" · ")}`;
  }
  return copy.videoParametersUnavailable || "";
}

function profileAudioSummary(profile, copy = {}) {
  const audio = profile?.audio || profile?.rtsp_probe?.audio;
  if (!audio) return copy.audioNone || "";
  const parts = [];
  if (hasValue(audio.codec)) parts.push(audio.codec);
  if (hasValue(audio.channels)) parts.push(`${copy.channels || ""} ${audio.channels}`.trim());
  if (hasValue(audio.sample_rate)) parts.push(`${copy.sampleRate || ""} ${audio.sample_rate}`.trim());
  return parts.length ? `${copy.audio || ""}: ${parts.join(" · ")}` : (copy.audioNone || "");
}

function profileNameText(profile) {
  return `${profile?.name || ""} ${profile?.token || ""}`.toLowerCase();
}

function profileDisplayName(profile, copy = {}) {
  return String(profile?.name || profile?.token || copy.profileFallback || "Profile").trim();
}

function profilePixels(profile) {
  const width = Number(profile?.video?.width || 0);
  const height = Number(profile?.video?.height || 0);
  return width * height;
}

function profileRole(profile, data) {
  const token = profile?.token || "";
  if (token && token === data?.suggested_main_profile_token) return "main";
  if (token && token === data?.suggested_sub_profile_token) return "sub";

  const name = profileNameText(profile);
  if (/(main|primary|stream1|profile1|high)/.test(name)) return "main";
  if (/(sub|secondary|stream2|profile2|low)/.test(name)) return "sub";

  const profiles = data?.profiles || [];
  if (profiles.length > 1) {
    const maxPixels = Math.max(...profiles.map(profilePixels));
    if (profilePixels(profile) && profilePixels(profile) < maxPixels) return "sub";
  }
  return "main";
}

function profileConfigWarning(profile, copy = {}) {
  const warnings = profile?.warnings || [];
  if (profile?.video_config_state === "unavailable" || warnings.some((item) => String(item).includes("video_encoder"))) {
    return copy.onvifVideoConfigUnavailable;
  }
  return "";
}

function profileByToken(data, token) {
  return (data?.profiles || []).find((item) => item.token === token) || null;
}

function profileStreamValue(profile) {
  return profile?.stream_path || prettyRtspValue(profile?.stream_uri || "");
}

function profileMatchesStream(profile, value) {
  const left = profileStreamValue(profile);
  const right = prettyRtspValue(value || "");
  return Boolean(left && right && left === right);
}

function profileTokenSummary(profile) {
  const token = String(profile?.token || "").trim();
  if (!token) return "";
  return token.length > 28 ? `${token.slice(0, 14)}...${token.slice(-8)}` : token;
}

function profileAssignmentSummary(profile, fallbackPath, copy = {}) {
  if (!profile) {
    return fallbackPath ? `${copy.pathLabel} ${prettyRtspValue(fallbackPath)}` : copy.unassigned;
  }
  const parts = [profileDisplayName(profile, copy)];
  const resolution = profileResolution(profile);
  const token = profileTokenSummary(profile);
  if (resolution) parts.push(resolution);
  if (token) parts.push(copy.tokenSummary ? copy.tokenSummary.replace("{token}", token) : `Token: ${token}`);
  return parts.join(" / ");
}

function configFromResult(result) {
  return {
    codec: result.config?.codec || "",
    resolution: result.config?.resolution || (result.config?.width && result.config?.height ? `${result.config.width}x${result.config.height}` : ""),
    fps: result.config?.fps || "",
    bitrate: result.config?.bitrate || "",
    iframe_interval: result.config?.iframe_interval || "",
    quality: result.config?.quality || "",
    supported: result.supported || {},
  };
}

function mergeProfileConfig(profile, result) {
  if (!profile || profile.token !== result?.profile_token) return profile;
  const current = configFromResult(result);
  const resolution = current.resolution || profileResolution(profile);
  const [width, height] = resolution && resolution.includes("x") ? resolution.split("x") : [null, null];
  return {
    ...profile,
    settings: current,
    current,
    supported: result.supported || {},
    video: {
      ...(profile.video || {}),
      codec: current.codec || profile.video?.codec,
      width: width ? Number(width) : profile.video?.width,
      height: height ? Number(height) : profile.video?.height,
      fps: current.fps || profile.video?.fps,
      bitrate_limit: current.bitrate || profile.video?.bitrate_limit,
      encoding_interval: current.iframe_interval || profile.video?.encoding_interval,
      quality: current.quality || profile.video?.quality,
    },
  };
}

function cameraPayloadFromForm(source, editingCameraId) {
  const normalizedSource = normalizeCameraStreamDefaults(source);
  const protocol = String(source.protocol || "rtsp").toLowerCase();
  const payload = {
    ...normalizedSource,
    camera_id: editingCameraId || null,
    port: Number(normalizedSource.port),
    segment_minutes: Number(normalizedSource.segment_minutes),
    retention_days: Number(normalizedSource.retention_days),
    storage_quota_gb: Number(normalizedSource.storage_quota_gb),
  };
  delete payload.rtsp_port_manually_set;
  if (protocol === "onvif") {
    payload.rtsp_host = rtspReachableHost(normalizedSource);
    payload.rtsp_port = rtspReachablePort(normalizedSource);
  } else {
    delete payload.rtsp_host;
    delete payload.rtsp_port;
  }
  return payload;
}

function normalizeCameraError(message, copy) {
  const text = String(message || "").trim();
  if (!text) return copy.actionFailed;
  if (text.includes("camera_validation_required")) {
    return copy.validationRequired;
  }
  if (text.includes("ffprobe") || text.includes("Invalid data") || text.includes("Server returned")) {
    return copy.badCameraResponse;
  }
  if (text.length > 180) {
    return copy.cameraConnectFailed;
  }
  return text;
}

function normalizeRuntimeError(message, copy) {
  const text = String(message || "").trim();
  if (!text) return "";
  if (text.includes("401") || text.toLowerCase().includes("auth")) {
    return copy.authError;
  }
  if (text.toLowerCase().includes("timeout")) {
    return copy.timeoutError;
  }
  if (text.toLowerCase().includes("storage")) {
    return copy.storageError;
  }
  if (text.toLowerCase().includes("ffmpeg")) {
    return copy.ffmpegError;
  }
  if (text.length > 140) {
    return copy.recordingUnavailable;
  }
  return text;
}

function archiveCleanupMessage(result, copy) {
  const warnings = result?.warnings || result?.archive_cleanup?.warnings || result?.recordings?.warnings || [];
  const reasons = result?.archive_cleanup?.reason_counts || result?.recordings?.reason_counts || {};
  if (!result?.camera_removed) return "";
  if (!warnings.length && !Object.keys(reasons).length) return copy.cameraDeleted;
  if (reasons.delete_recordings_permission_missing) {
    return copy.archiveDeleteNoPermission;
  }
  if (reasons.active_job) {
    return copy.archiveDeleteActiveJob;
  }
  if (reasons.unowned) {
    return copy.archiveDeleteUnowned;
  }
  if (reasons.path_outside_storage) {
    return copy.archiveDeleteUnsafePath;
  }
  if (warnings.includes("archive_retained") || reasons.recordings_exist_delete_files_false_requires_safe_policy) {
    return copy.archiveRetained;
  }
  return copy.archiveCleanupPartial;
}

function cameraEndpointLabel(camera) {
  const protocol = String(camera?.protocol || "").toUpperCase();
  return `${camera?.host || "-"}:${camera?.port || "-"} · ${protocol || "-"}`;
}

function cameraRecordingLabel(camera, copy) {
  if (camera?.recording_mode === "always") return copy.always;
  return camera?.recording_mode || "-";
}

function cameraStreamsLabel(camera, copy = {}) {
  const rec = String(camera?.default_record_stream || "main").toUpperCase();
  const live = String(camera?.default_live_stream || "main").toUpperCase();
  return copy.streamCountSummary
    ? copy.streamCountSummary.replace("{rec}", rec).replace("{live}", live)
    : `${rec} · ${live}`;
}

function getCameraRuntimeBadge(camera, runtime, recorderStatus, storageAvailable, copy) {
  if (!camera.enabled || runtime?.enabled === false || camera.status === "disabled") {
    return { text: copy.disabled, cls: "warn" };
  }

  if (storageAvailable === false) {
    return { text: copy.error, cls: "err" };
  }

  if (!recorderStatus) {
    if (camera.status === "error") return { text: copy.error, cls: "err" };
    return { text: copy.unknownStatus, cls: "warn" };
  }

  const runtimeError = normalizeRuntimeError(runtime?.last_error || runtime?.camera_last_error || recorderStatus?.last_error, copy);
  const jobState = String(runtime?.job_state || camera.status || "").toLowerCase();
  const currentFailure = runtime?.current_failure === true;
  const staleCurrentSegment = runtime?.stale_current_segment === true || String(runtime?.recording_health || "").toLowerCase() === "degraded";

  if (recorderStatus?.heartbeat?.status === "stale_or_unavailable") {
    return { text: copy.unknownStatus, cls: "warn" };
  }

  if (jobState === "restarting") return { text: copy.restarting, cls: "warn" };
  if (jobState === "starting") return { text: copy.starting, cls: "warn" };
  if (jobState === "stopping") return { text: copy.stopping, cls: "warn" };
  if (jobState === "recording" && staleCurrentSegment) return { text: copy.recordingStale, cls: "warn" };
  if (jobState === "recording" && !currentFailure) return { text: copy.recordingNow, cls: "ok" };
  if (currentFailure || runtimeError || jobState === "error") {
    return { text: copy.error, cls: "err" };
  }
  if (jobState === "recording") return { text: copy.recordingNow, cls: "ok" };
  if (runtime?.recording_mode && runtime.recording_mode !== "always") {
    return { text: copy.recordingWaiting, cls: "warn" };
  }

  return { text: copy.recordingWaiting, cls: "warn" };
}

export default function CamerasPage() {
  const { t } = useI18n();
  const copy = useLocaleText("cameras");
  const { currentUser } = useCurrentUser();
  const [cameras, setCameras] = useState([]);
  const [camerasLoadState, setCamerasLoadState] = useState("idle");
  const [viewMode, setViewMode] = useState("list");
  const [storage, setStorage] = useState(null);
  const [recorderStatus, setRecorderStatus] = useState(null);
  const [secondaryStatusState, setSecondaryStatusState] = useState("idle");
  const [showEditor, setShowEditor] = useState(false);
  const [editorMode, setEditorMode] = useState("create");
  const [editingCameraId, setEditingCameraId] = useState(null);
  const [form, setForm] = useState(initialForm);
  const [error, setError] = useState("");
  const [deleteNotice, setDeleteNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [onvifBusy, setOnvifBusy] = useState(false);
  const [onvifDiscoveryBusy, setOnvifDiscoveryBusy] = useState(false);
  const [onvifProbeBusy, setOnvifProbeBusy] = useState(false);
  const [onvifDiscovery, setOnvifDiscovery] = useState(null);
  const [onvifData, setOnvifData] = useState(null);
  const [onvifConfigBusy, setOnvifConfigBusy] = useState(false);
  const [onvifConfig, setOnvifConfig] = useState(initialOnvifConfig);
  const [selectedOnvifProfileToken, setSelectedOnvifProfileToken] = useState("");
  const [onvifStatus, setOnvifStatus] = useState("");
  const [profileToast, setProfileToast] = useState(null);
  const profileToastTimerRef = useRef(null);

  const [deleteModalOpen, setDeleteModalOpen] = useState(false);
  const [cameraToDelete, setCameraToDelete] = useState(null);
  const [deleteFiles, setDeleteFiles] = useState(false);
  const viewModeLoadedRef = useRef(false);

  async function load() {
    setCamerasLoadState((prev) => (prev === "loaded" || prev === "refreshing" ? "refreshing" : "loading"));
    try {
      const cams = await apiFetch("/cameras");
      setCameras(cams);
      setError("");
      setCamerasLoadState("loaded");
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
      setCamerasLoadState((prev) => (prev === "loaded" || prev === "refreshing" ? "loaded" : "error"));
    }
  }

  async function loadSecondaryStatus() {
    setSecondaryStatusState((prev) => (prev === "loaded" || prev === "refreshing" ? "refreshing" : "loading"));
    try {
      const [st, recorder] = await Promise.all([
        apiFetch("/storage/status").catch(() => null),
        apiFetch("/system/recorder/summary").catch(() => null),
      ]);
      setStorage(st);
      setRecorderStatus(recorder);
      setSecondaryStatusState("loaded");
    } catch (_) {
      setSecondaryStatusState("error");
    }
  }

  const camerasLoaded = camerasLoadState === "loaded" || camerasLoadState === "refreshing";
  const camerasFirstLoading = camerasLoadState === "idle" || camerasLoadState === "loading";
  const storagePathChecks = storage?.storage_path_checks || {};
  const storageAvailable = storagePathChecks.path_exists ?? storage?.storage_root_exists;
  const recorderCameraMap = new Map(
    (recorderStatus?.camera_recording_states || []).map((item) => [String(item.camera_id), item])
  );

  useEffect(() => {
    load();
    loadSecondaryStatus();
    const timer = setInterval(load, 5000);
    const secondaryTimer = setInterval(loadSecondaryStatus, 5000);
    return () => {
      clearInterval(timer);
      clearInterval(secondaryTimer);
      window.clearTimeout(profileToastTimerRef.current);
    };
  }, []);

  useEffect(() => {
    setViewMode(loadCamerasViewMode(window.localStorage, currentUser, "list"));
    viewModeLoadedRef.current = true;
  }, [currentUser?.id, currentUser?.username]);

  useEffect(() => {
    if (!viewModeLoadedRef.current) return;
    saveCamerasViewMode(window.localStorage, currentUser, viewMode);
  }, [currentUser?.id, currentUser?.username, viewMode]);

  function showProfileToast(title, text) {
    setProfileToast({ title, text, variant: "success" });
    window.clearTimeout(profileToastTimerRef.current);
    profileToastTimerRef.current = window.setTimeout(() => setProfileToast(null), 2200);
  }

  function patch(key, value) {
    setForm((prev) => {
      const next = applyCameraFormPatch(prev, key, value, PREVIEW_SENSITIVE_FIELDS);
      return normalizeCameraStreamDefaults(next);
    });
    if (PREVIEW_SENSITIVE_FIELDS.has(key)) {
      setTestResult(null);
    }
  }

  function patchOnvifConfig(key, value) {
    setOnvifConfig((prev) => ({ ...prev, [key]: value }));
  }

  function updateProfileConfigState(result) {
    if (!result?.profile_token) return;
    setOnvifData((prev) => {
      if (!prev?.profiles) return prev;
      return {
        ...prev,
        profiles: prev.profiles.map((profile) => mergeProfileConfig(profile, result)),
      };
    });
  }

  function updateProfileProbeState(profileToken, result) {
    if (!profileToken || !result) return;
    setOnvifData((prev) => {
      if (!prev?.profiles) return prev;
      return {
        ...prev,
        profiles: prev.profiles.map((profile) => (
          profile.token === profileToken
            ? { ...profile, rtsp_probe: { video: result.video || null, audio: result.audio || null, format: result.format || null } }
            : profile
        )),
      };
    });
  }

  function openCreate() {
    setEditorMode("create");
    setEditingCameraId(null);
    setForm(initialForm);
    setError("");
    setDeleteNotice("");
    setTestResult(null);
    setOnvifDiscovery(null);
    setOnvifData(null);
    setOnvifConfig(initialOnvifConfig);
    setSelectedOnvifProfileToken("");
    setOnvifStatus("");
    setShowEditor(true);
  }

  function openEdit(camera) {
    const explicitEditRtspPort = camera.rtsp_port;
    const editRtspPort = camera.rtsp_reachable_port || rtspPortFromValue(camera.rtsp_main_url || camera.rtsp_sub_url || "") || 554;
    setEditorMode("edit");
    setEditingCameraId(camera.id);
    setForm(normalizeCameraStreamDefaults({
      name: camera.name || "",
      enabled: camera.enabled ?? true,
      protocol: camera.protocol || "rtsp",
      host: camera.host || "",
      port: camera.port || 554,
      username: camera.username || "",
      password: "",
      rtsp_main_url: prettyRtspValue(camera.rtsp_main_url || ""),
      rtsp_sub_url: prettyRtspValue(camera.rtsp_sub_url || ""),
      rtsp_host: camera.rtsp_reachable_host || rtspHostFromValue(camera.rtsp_main_url || camera.rtsp_sub_url || "") || camera.host || "",
      rtsp_port: editRtspPort,
      rtsp_port_manually_set: isRtspPortManualOverrideForEdit(camera.port, explicitEditRtspPort),
      rtsp_transport: camera.rtsp_transport || "tcp",
      onvif_path: camera.onvif_path || "",
      onvif_profile_token: camera.onvif_profile_token || "",
      onvif_channel_id: camera.onvif_channel_id || "",
      recording_mode: camera.recording_mode || "always",
      default_live_stream: camera.default_live_stream || "sub",
      default_record_stream: camera.default_record_stream || "main",
      segment_minutes: camera.segment_minutes || 5,
      retention_days: camera.retention_days || 30,
      storage_quota_gb: camera.storage_quota_gb || 50,
      validation_token: null,
      onvif_probe_token: null,
      manual_confirm_unverified: false,
    }));
    setError("");
    setTestResult(null);
    setOnvifDiscovery(null);
    setOnvifData(null);
    setOnvifConfig(initialOnvifConfig);
    setSelectedOnvifProfileToken(camera.onvif_profile_token || "");
    setOnvifStatus("");
    setShowEditor(true);
  }

  function openDeleteModal(camera) {
    setCameraToDelete(camera);
    setDeleteFiles(false);
    setDeleteModalOpen(true);
  }

  function closeDeleteModal() {
    setDeleteModalOpen(false);
    setCameraToDelete(null);
    setDeleteFiles(false);
  }

  async function runTest(formOverride = form) {
    setError("");
    setTesting(true);
    setTestResult(null);
    try {
      const payload = cameraPayloadFromForm(formOverride, editingCameraId);
      const result = await apiFetch("/cameras/test", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });
      setTestResult(result);
      setForm((prev) => ({
        ...prev,
        preview_token: result.preview_token || prev.preview_token,
        validation_token: result.validation_token || prev.validation_token,
        manual_confirm_unverified: false,
      }));
      updateProfileProbeState(formOverride.onvif_profile_token || selectedOnvifProfileToken, result);
      return result;
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
      setTestResult(null);
      return null;
    } finally {
      setTesting(false);
    }
  }

  async function loadOnvifProfiles() {
    setError("");
    setOnvifStatus("");
    setOnvifBusy(true);
    setOnvifData(null);
    try {
      const payload = {
        camera_id: editingCameraId || null,
        host: form.host,
        port: Number(form.port || 80),
        rtsp_host: rtspReachableHost(form),
        rtsp_port: rtspReachablePort(form),
        username: form.username,
        password: form.password,
      };
      const result = await apiFetch("/cameras/onvif/profiles", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });
      setOnvifData(result);
      const mainToken = result.suggested_main_profile_token || result.profiles?.[0]?.token || "";
      const subToken = result.suggested_sub_profile_token || "";
      const mainProfile = profileByToken(result, mainToken);
      const subProfile = profileByToken(result, subToken);
      const nextForm = normalizeCameraStreamDefaults({
        ...form,
        rtsp_host: result.rtsp_reachable?.host || rtspReachableHost(form),
        rtsp_port: smartRtspPort(form, result.rtsp_reachable?.port || rtspReachablePort(form)),
        onvif_profile_token: mainToken,
        rtsp_main_url: mainProfile?.stream_path || prettyRtspValue(mainProfile?.stream_uri || form.rtsp_main_url),
        rtsp_sub_url: subProfile?.stream_path || prettyRtspValue(subProfile?.stream_uri || form.rtsp_sub_url),
        default_record_stream: "main",
        default_live_stream: subProfile ? "sub" : form.default_live_stream,
      });
      setForm(nextForm);
      setSelectedOnvifProfileToken(mainToken);
      if (mainToken) {
        await loadOnvifProfileConfig(mainToken);
      }
      if (mainProfile?.rtsp_ready && nextForm.rtsp_host && nextForm.rtsp_main_url) {
        await runTest(nextForm);
        setOnvifStatus(copy.mainSelectedChecked);
      } else {
        setOnvifStatus(copy.profilesLoadedNeedRtsp);
      }
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
      setOnvifData(null);
    } finally {
      setOnvifBusy(false);
    }
  }

  async function discoverOnvifCameras() {
    setError("");
    setOnvifStatus("");
    setOnvifDiscoveryBusy(true);
    try {
      const result = await apiFetch("/cameras/onvif/discover", {
        method: "POST",
        body: JSON.stringify({ timeout_seconds: 5 }),
        headers: { "Content-Type": "application/json" },
      });
      setOnvifDiscovery(result);
      if (result.candidates?.length) {
        setOnvifStatus(t("cameras.onvifFound", { count: result.candidates.length }));
      } else {
        setOnvifStatus(result.message || copy.onvifNotFound);
      }
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
    } finally {
      setOnvifDiscoveryBusy(false);
    }
  }

  function selectOnvifCandidate(candidate) {
    setForm((prev) => normalizeCameraStreamDefaults({
      ...prev,
      protocol: "onvif",
      host: candidate.host || prev.host,
      port: candidate.port || prev.port || 80,
      onvif_path: candidate.xaddr_path || prev.onvif_path || "",
      rtsp_host: candidate.host || prev.rtsp_host || "",
      rtsp_port: smartRtspPort(prev, candidate.port || prev.port || 554),
      preview_token: null,
      validation_token: null,
      onvif_probe_token: null,
      manual_confirm_unverified: false,
    }));
    setOnvifStatus(copy.onvifEndpointFilled);
  }

  async function probeOnvifCamera() {
    setError("");
    setOnvifStatus("");
    setOnvifProbeBusy(true);
    try {
      const payload = {
        host: form.host,
        port: Number(form.port || 80),
        rtsp_host: rtspReachableHost(form),
        rtsp_port: rtspReachablePort(form),
        username: form.username,
        password: form.password,
        timeout_seconds: 6,
      };
      const result = await apiFetch("/cameras/onvif/probe", {
        method: "POST",
        body: JSON.stringify(payload),
        headers: { "Content-Type": "application/json" },
      });
      setOnvifData(result);
      const mainToken = result.suggested_main_profile_token || result.profiles?.[0]?.token || "";
      const subToken = result.suggested_sub_profile_token || "";
      const mainProfile = profileByToken(result, mainToken);
      const subProfile = profileByToken(result, subToken);
      setForm((prev) => normalizeCameraStreamDefaults({
        ...prev,
        rtsp_host: result.rtsp_reachable?.host || rtspReachableHost(prev),
        rtsp_port: smartRtspPort(prev, result.rtsp_reachable?.port || rtspReachablePort(prev)),
        onvif_profile_token: mainToken || prev.onvif_profile_token,
        rtsp_main_url: mainProfile?.stream_path || prev.rtsp_main_url,
        rtsp_sub_url: subProfile?.stream_path || prev.rtsp_sub_url,
        default_record_stream: "main",
        default_live_stream: subProfile ? "sub" : prev.default_live_stream,
        onvif_probe_token: result.onvif_probe_token || null,
        manual_confirm_unverified: false,
      }));
      setSelectedOnvifProfileToken(mainToken);
      setOnvifStatus(copy.onvifSuccess);
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
      setOnvifData(null);
    } finally {
      setOnvifProbeBusy(false);
    }
  }

  function useProfileAsMain(profile) {
    const token = profile.token || "";
    const name = profileDisplayName(profile, copy);
    setForm((prev) => normalizeCameraStreamDefaults({
      ...prev,
      onvif_profile_token: token,
      rtsp_main_url: profileStreamValue(profile),
      default_record_stream: "main",
      preview_token: null,
      validation_token: null,
      onvif_probe_token: null,
      manual_confirm_unverified: false,
    }));
    setTestResult(null);
    setSelectedOnvifProfileToken(token);
    if (token) {
      loadOnvifProfileConfig(token);
    }
    showProfileToast(copy.mainStreamSelected, name);
  }

  function useProfileAsSub(profile) {
    const token = profile.token || "";
    const name = profileDisplayName(profile, copy);
    setForm((prev) => normalizeCameraStreamDefaults({
      ...prev,
      rtsp_sub_url: profileStreamValue(profile),
      onvif_profile_token: prev.onvif_profile_token || token,
      default_live_stream: "sub",
      preview_token: null,
      validation_token: null,
      onvif_probe_token: null,
      manual_confirm_unverified: false,
    }));
    setTestResult(null);
    setSelectedOnvifProfileToken(token);
    if (token) {
      loadOnvifProfileConfig(token);
    }
    showProfileToast(copy.subStreamSelected, name);
  }

  async function selectOnvifProfile(profile) {
    const token = profile?.token || "";
    setSelectedOnvifProfileToken(token);
    if (token) {
      await loadOnvifProfileConfig(token);
    }
  }

  async function loadOnvifProfileConfig(profileToken = selectedOnvifProfileToken || form.onvif_profile_token) {
    if (!profileToken) {
      setError(copy.selectOnvifProfileFirst);
      return;
    }

    setError("");
    setOnvifConfigBusy(true);
    try {
      const result = await apiFetch("/cameras/onvif/profile_config", {
        method: "POST",
        body: JSON.stringify({
          camera_id: editingCameraId || null,
          host: form.host,
          port: Number(form.port),
          username: form.username,
          password: form.password,
          profile_token: profileToken,
        }),
        headers: { "Content-Type": "application/json" },
      });

      setSelectedOnvifProfileToken(profileToken);
      setOnvifConfig(configFromResult(result));
      updateProfileConfigState(result);
      return result;
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
      return null;
    } finally {
      setOnvifConfigBusy(false);
    }
  }

  async function applyOnvifProfileConfig() {
    const profileToken = selectedOnvifProfileToken || form.onvif_profile_token;
    if (!profileToken) {
      setError(copy.selectOnvifProfileFirst);
      return;
    }

    setError("");
    setOnvifConfigBusy(true);
    try {
      const supported = onvifConfig.supported || {};
      const config = {};
      if (supported.codec?.writable && supported.codec?.options?.length && onvifConfig.codec) config.codec = onvifConfig.codec;
      if (supported.resolution?.writable && onvifConfig.resolution) config.resolution = onvifConfig.resolution;
      if (supported.fps?.writable && onvifConfig.fps) config.fps = Number(onvifConfig.fps);
      if (supported.bitrate?.writable && onvifConfig.bitrate) config.bitrate = Number(onvifConfig.bitrate);
      if (supported.iframe_interval?.writable && onvifConfig.iframe_interval) config.iframe_interval = Number(onvifConfig.iframe_interval);
      if (supported.quality?.writable && onvifConfig.quality !== "") config.quality = Number(onvifConfig.quality);
      if (!Object.keys(config).length) {
        setError(copy.noWritableOnvifSettings);
        return;
      }

      await apiFetch("/cameras/onvif/update_profile", {
        method: "POST",
        body: JSON.stringify({
          camera_id: editingCameraId || null,
          host: form.host,
          port: Number(form.port),
          username: form.username,
          password: form.password,
          profile_token: profileToken,
          config,
        }),
        headers: { "Content-Type": "application/json" },
      });

      await loadOnvifProfileConfig(profileToken);
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
    } finally {
      setOnvifConfigBusy(false);
    }
  }

  async function saveCamera() {
    setError("");
    setBusy(true);
    try {
      const payload = cameraPayloadFromForm(form, editingCameraId);

      if (!payload.password) delete payload.password;

      if (editorMode === "create") {
        await apiFetch("/cameras", {
          method: "POST",
          body: JSON.stringify(payload),
          headers: { "Content-Type": "application/json" },
        });
      } else {
        await apiFetch(`/cameras/${editingCameraId}`, {
          method: "PUT",
          body: JSON.stringify(payload),
          headers: { "Content-Type": "application/json" },
        });
      }

      setShowEditor(false);
      setEditingCameraId(null);
      setForm(initialForm);
      setTestResult(null);
      setOnvifDiscovery(null);
      setOnvifData(null);
      setOnvifConfig(initialOnvifConfig);
      setSelectedOnvifProfileToken("");
      setOnvifStatus("");
      await load();
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
    } finally {
      setBusy(false);
    }
  }

  async function toggleCamera(camera) {
    setError("");
    try {
      await apiFetch(`/cameras/${camera.id}/${camera.enabled ? "disable" : "enable"}`, {
        method: "POST",
      });
      await load();
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
    }
  }

  async function confirmDeleteCamera() {
    if (!cameraToDelete) return;

    setError("");
    setBusy(true);
    try {
      const result = await apiFetch(
        `/cameras/${cameraToDelete.id}?delete_files=${deleteFiles ? "true" : "false"}`,
        { method: "DELETE" }
      );
      closeDeleteModal();
      setDeleteNotice(archiveCleanupMessage(result, copy) || copy.cameraDeleted);
      await load();
    } catch (err) {
      setError(normalizeCameraError(err.message, copy));
    } finally {
      setBusy(false);
    }
  }

  const canApplySelectedOnvifSettings = canApplyOnvifSettings(onvifConfig, selectedOnvifProfileToken);
  const formStreamOptions = availableCameraStreams(form);
  const onvifProfiles = onvifData?.profiles || [];
  const selectedOnvifProfile = profileByToken(onvifData, selectedOnvifProfileToken || form.onvif_profile_token);
  const assignedMainProfile = onvifProfiles.find((profile) => (
    profile.token === form.onvif_profile_token || profileMatchesStream(profile, form.rtsp_main_url)
  )) || null;
  const assignedSubProfile = onvifProfiles.find((profile) => profileMatchesStream(profile, form.rtsp_sub_url)) || null;
  const hasVerifiedCameraPayload = Boolean(form.validation_token && testResult?.ok);
  const showManualUnverifiedBypass = !hasVerifiedCameraPayload && (form.protocol === "onvif" || editorMode === "create");

  function renderProfileSettingSlot(slot) {
    const meta = profileSettingMeta(onvifConfig.supported, slot.key);
    const value = profileSettingDisplayValue(onvifConfig, slot.key);
    const state = profileSettingState(meta, value, slot.requiresOptions);
    const hasOptions = Array.isArray(meta.options) && meta.options.length > 0;
    const range = meta.range;
    const statusText = state === "editable" ? copy.writable : state === "readonly" ? copy.readonly : copy.unavailable;

    let control = null;
    if (state === "editable" && hasOptions) {
      control = (
        <select className="select cameraSettingControl" value={value} onChange={(e) => patchOnvifConfig(slot.key, e.target.value)}>
          {meta.options.map((item) => (
            <option key={item} value={item}>{item}</option>
          ))}
        </select>
      );
    } else if (state === "editable") {
      control = (
        <input
          className="input cameraSettingControl"
          value={value}
          type={slot.numeric ? "number" : "text"}
          min={range?.min ?? undefined}
          max={range?.max ?? undefined}
          onChange={(e) => patchOnvifConfig(slot.key, e.target.value)}
        />
      );
    } else {
      control = <div className="cameraSettingValue">{value || slot.empty}</div>;
    }

    return (
      <div className={`cameraSettingSlot ${state}`} key={slot.key}>
        <div className="cameraSettingSlotHead">
          <span>{slot.label}</span>
          <em>{statusText}</em>
        </div>
        {control}
        {state === "editable" && range ? <div className="cameraSettingHint">{copy.range}: {range.min ?? "?"} - {range.max ?? "?"}</div> : null}
        {state === "unavailable" ? <div className="cameraSettingHint">{slot.unavailable}</div> : null}
      </div>
    );
  }

  const profileSettingSlots = [
    { key: "codec", label: copy.codecCompression, empty: copy.notReceived, unavailable: copy.codecUnavailable, requiresOptions: true },
    { key: "encode_strategy", label: copy.encodingStrategy, empty: copy.notReceived, unavailable: copy.standardOnvifUnavailable },
    { key: "resolution", label: copy.resolution, empty: copy.notReceived, unavailable: copy.resolutionUnavailable },
    { key: "fps", label: copy.fps, empty: copy.notReceived, unavailable: copy.fpsUnavailable, numeric: true },
    { key: "bitrate_type", label: copy.bitrateType, empty: copy.notReceived, unavailable: copy.bitrateUnavailable },
    { key: "quality", label: copy.vbrQuality, empty: copy.notReceived, unavailable: copy.qualityUnavailable, numeric: true },
    { key: "bitrate", label: copy.maxBitrate, empty: copy.notReceived, unavailable: copy.maxBitrateUnavailable, numeric: true },
    { key: "iframe_interval", label: copy.iframeInterval, empty: copy.notReceived, unavailable: copy.iframeUnavailable, numeric: true },
    { key: "codec_profile", label: copy.codecProfile, empty: copy.notReceived, unavailable: copy.codecProfileUnavailable },
    { key: "audio_codec", label: copy.audio, empty: copy.notReceived, unavailable: copy.audioUnavailable },
  ];

  return (
    <Layout>
      <div className="standardPage">
      <div className="pageHeader cameraPageHeader">
        <div>
          <h1 className="pageTitle">{copy.title}</h1>
          <div className="pageSubtitle">{copy.subtitle}</div>
        </div>
        <div className="cameraHeaderActions">
          <div className="cameraViewToggle" role="group" aria-label={copy.displayMode}>
            <button type="button" className={viewMode === "list" ? "active" : ""} onClick={() => setViewMode("list")}>{copy.list}</button>
            <button type="button" className={viewMode === "cards" ? "active" : ""} onClick={() => setViewMode("cards")}>{copy.cards}</button>
          </div>
          <button className="button" onClick={openCreate}>{copy.addCamera}</button>
        </div>
      </div>

      <OperatorProblemBanners domains={["cameras", "recorder"]} className="pageWarnings" limit={4} />

      {error && !showEditor ? <div className="badge err" style={{ marginBottom: 14 }}>{error}</div> : null}
      {deleteNotice && !showEditor ? <div className="badge ok" style={{ marginBottom: 14 }}>{deleteNotice}</div> : null}

      <div className={viewMode === "cards" ? "cameraTileGrid" : "cameraCards"}>
        {camerasFirstLoading ? (
          <div className="card">{t("common.loading")}</div>
        ) : camerasLoadState === "error" ? (
          <div className="card">{error}</div>
        ) : camerasLoaded && !cameras.length ? (
          <div className="card">{copy.noCameras}</div>
        ) : (
          cameras.map((camera) => {
            const runtime = recorderCameraMap.get(String(camera.id));
            const badge = getCameraRuntimeBadge(camera, runtime, recorderStatus, storageAvailable, copy);
            if (viewMode === "cards") {
              return (
                <article className="cameraTileCard" key={camera.id}>
                  <div className="cameraTilePreview">
                    {camera.preview_url ? (
                      <img src={camera.preview_url} alt="" />
                    ) : (
                      <div className="cameraTilePreviewEmpty">{copy.noFrame}</div>
                    )}
                  </div>
                  <div className="cameraTileBody">
                    <div className="cameraTileTitleRow">
                      <div className="cameraTileIdentity">
                        <div className="cameraTileName" title={camera.name}>{camera.name}</div>
                        <div className="cameraTileEndpoint" title={cameraEndpointLabel(camera)}>{cameraEndpointLabel(camera)}</div>
                      </div>
                      <div className="cameraTileActions">
                        <button className="cameraTileIconButton" onClick={() => openEdit(camera)} title={copy.edit} aria-label={copy.edit}>
                          {"\u270e"}
                        </button>
                        <button className="cameraTileIconButton" onClick={() => toggleCamera(camera)} title={camera.enabled ? copy.disable : copy.enable} aria-label={camera.enabled ? copy.disable : copy.enable}>
                          {camera.enabled ? "\u23fb" : "\u2713"}
                        </button>
                        <button className="cameraTileIconButton danger" onClick={() => openDeleteModal(camera)} title={copy.delete} aria-label={copy.delete}>
                          {"\ud83d\uddd1"}
                        </button>
                      </div>
                    </div>
                    <div className="cameraTileSystem">
                      <div><span>{copy.recording}</span><strong>{cameraRecordingLabel(camera, copy)}</strong></div>
                      <div><span>{copy.segment}</span><strong>{camera.segment_minutes} {copy.minutesShort}</strong></div>
                      <div><span>{copy.retention}</span><strong>{camera.retention_days} {copy.daysShort}</strong></div>
                      <div><span>{copy.limit}</span><strong>{camera.storage_quota_gb} {copy.gbShort}</strong></div>
                    </div>
                    <div className="cameraTileFoot">
                      <span>{cameraStreamsLabel(camera, copy)}</span>
                      <span className={`badge ${badge.cls}`}>{badge.text}</span>
                    </div>
                  </div>
                </article>
              );
            }
            return (
              <div className="cameraCard card" key={camera.id}>
                <div className="cameraCardGrid">
                  <div className="cameraField cameraNameField">
                    <div className="cameraFieldLabel">{copy.camera}</div>
                    <div className="cameraPrimary" title={camera.name}>{camera.name}</div>
                    <div className="cameraSecondary" title={`${copy.folder}: ${camera.storage_folder_name}`}>{copy.folder}: {camera.storage_folder_name}</div>
                    <div className="cameraActions">
                      <button className="cameraIconButton" onClick={() => openEdit(camera)} title={copy.edit} aria-label={copy.edit}>
                        {"\u270e"}
                      </button>
                      <button className="cameraIconButton" onClick={() => toggleCamera(camera)} title={camera.enabled ? copy.disable : copy.enable} aria-label={camera.enabled ? copy.disable : copy.enable}>
                        {camera.enabled ? "\u23fb" : "\u2713"}
                      </button>
                      <button className="cameraIconButton danger" onClick={() => openDeleteModal(camera)} title={copy.delete} aria-label={copy.delete}>
                        {"\ud83d\uddd1"}
                      </button>
                    </div>
                  </div>

                  <div className="cameraPreviewField" aria-label={copy.cameraFrame}>
                    {camera.preview_url ? (
                      <img src={camera.preview_url} alt="" />
                    ) : (
                      <div className="cameraPreviewPlaceholder">{copy.noFrame}</div>
                    )}
                  </div>

                  <div className="cameraMetaGrid">
                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.protocol}</div>
                      <div>{camera.protocol?.toUpperCase()}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.address}</div>
                      <div>{camera.host}:{camera.port}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.segment}</div>
                      <div>{camera.segment_minutes} {copy.minutesShort}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.retention}</div>
                      <div>{camera.retention_days} {copy.daysShort}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.limit}</div>
                      <div>{camera.storage_quota_gb} {copy.gbShort}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">{copy.status}</div>
                      <div className="cameraStatusStack">
                        <span className={`badge ${badge.cls}`}>{badge.text}</span>
                      </div>
                    </div>
                  </div>
                </div>

              </div>
            );
          })
        )}
      </div>

      {showEditor ? (
        <div className="modalBackdrop">
          <div className="modal modalWide cameraEditorModal" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <div className="cameraModalTitleBlock">
                <h2>{editorMode === "create" ? copy.addTitle : copy.editTitle}</h2>
                <p>{copy.editorSubtitle}</p>
              </div>
              <button className="iconCloseButton" onClick={() => setShowEditor(false)} aria-label={copy.close}>×</button>
            </div>

            {profileToast ? (
              <div className={`cameraProfileToast ${profileToast.variant || "info"}`}>
                <strong>{profileToast.title}</strong>
                {profileToast.text ? <span>{profileToast.text}</span> : null}
              </div>
            ) : null}

            {error ? <div className="cameraModalError">{error}</div> : null}

            <div className="cameraEditorGrid">
              <div className="cameraEditorColumn">
                <section className="cameraModalSection">
                  <h3>{copy.connection}</h3>
                  <div className="formGrid">
                    <div>
                      <div className="formLabel">{copy.cameraName}</div>
                      <input className="input" value={form.name} onChange={(e) => patch("name", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">{copy.protocol}</div>
                      <select className="select" value={form.protocol} onChange={(e) => patch("protocol", e.target.value)}>
                        <option value="rtsp">RTSP</option>
                        <option value="onvif">ONVIF</option>
                      </select>
                    </div>
                    <div>
                      <div className="formLabel">{form.protocol === "onvif" ? copy.onvifHostIp : copy.ipHost}</div>
                      <input className="input" value={form.host} onChange={(e) => patch("host", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">{form.protocol === "onvif" ? copy.onvifPort : copy.rtspPort}</div>
                      <input className="input" value={form.port} onChange={(e) => patch("port", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">{copy.login}</div>
                      <input className="input" value={form.username} onChange={(e) => patch("username", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">{copy.password} {editorMode === "edit" ? copy.passwordEditHint : ""}</div>
                      <input className="input" type="password" value={form.password} onChange={(e) => patch("password", e.target.value)} />
                    </div>
                  </div>
                </section>

                <section className="cameraModalSection">
                  <h3>{copy.streamsRecording}</h3>
                  <div className="cameraStreamsGrid">
                    <div className="cameraStreamField wide">
                      <div className="formLabel">{copy.rtspMainPathUrl}</div>
                      <input className="input" value={form.rtsp_main_url} onChange={(e) => patch("rtsp_main_url", e.target.value)} />
                    </div>
                    <div className="cameraStreamField wide">
                      <div className="formLabel">{copy.rtspSubPathUrl}</div>
                      <input className="input" value={form.rtsp_sub_url} onChange={(e) => patch("rtsp_sub_url", e.target.value)} />
                    </div>
                    <div className="cameraStreamPolicyGrid">
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.rtspReachableHost}</div>
                        <input className="input" value={form.rtsp_host} onChange={(e) => patch("rtsp_host", e.target.value)} />
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.rtspReachablePort}</div>
                        <input className="input" type="number" min="1" value={form.rtsp_port} onChange={(e) => patch("rtsp_port", e.target.value)} />
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.rtspTransport}</div>
                        <select className="select" value={form.rtsp_transport} onChange={(e) => patch("rtsp_transport", e.target.value)}>
                          <option value="tcp">TCP</option>
                          <option value="udp">UDP</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.recordStream}</div>
                        <select className="select" value={form.default_record_stream} onChange={(e) => patch("default_record_stream", e.target.value)}>
                          {formStreamOptions.map((stream) => (
                            <option key={stream.key} value={stream.key}>{stream.label}</option>
                          ))}
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.liveStream}</div>
                        <select className="select" value={form.default_live_stream} onChange={(e) => patch("default_live_stream", e.target.value)}>
                          {formStreamOptions.map((stream) => (
                            <option key={stream.key} value={stream.key}>{stream.label}</option>
                          ))}
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.recordingMode}</div>
                        <select className="select" value={form.recording_mode} onChange={(e) => patch("recording_mode", e.target.value)}>
                          <option value="always">{copy.always}</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.segmentDuration}</div>
                        <select className="select" value={form.segment_minutes} onChange={(e) => patch("segment_minutes", e.target.value)}>
                          <option value="5">5 {copy.minutesShort}</option>
                          <option value="15">15 {copy.minutesShort}</option>
                          <option value="30">30 {copy.minutesShort}</option>
                          <option value="45">45 {copy.minutesShort}</option>
                          <option value="60">60 {copy.minutesShort}</option>
                          <option value="120">120 {copy.minutesShort}</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.retentionDays}</div>
                        <select className="select" value={form.retention_days} onChange={(e) => patch("retention_days", e.target.value)}>
                          <option value="7">7 {copy.daysShort}</option>
                          <option value="14">14 {copy.daysShort}</option>
                          <option value="21">21 {copy.daysShort}</option>
                          <option value="30">30 {copy.daysShort}</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">{copy.archiveLimitGb}</div>
                        <input className="input" type="number" min="1" value={form.storage_quota_gb} onChange={(e) => patch("storage_quota_gb", e.target.value)} />
                      </div>
                    </div>
                  </div>
                </section>

                {form.protocol === "onvif" ? <section className="cameraModalSection cameraOnvifSection">
                  <h3>3. ONVIF</h3>
                  <div className="cameraOnvifOnboarding">
                    <div className="toolbar cameraOnvifActions">
                      <button className="button secondary small" onClick={discoverOnvifCameras} disabled={onvifDiscoveryBusy}>
                        {onvifDiscoveryBusy ? copy.searching : copy.findOnvif}
                      </button>
                      <button className="button secondary small" onClick={probeOnvifCamera} disabled={onvifProbeBusy || !form.host}>
                        {onvifProbeBusy ? copy.checking : copy.checkOnvif}
                      </button>
                    </div>
                    <div className="cameraModalNote">
                      {copy.onvifHelp}
                    </div>
                    {onvifDiscovery?.candidates?.length ? (
                      <div className="cameraDiscoveryList">
                        {onvifDiscovery.candidates.map((candidate) => (
                          <button type="button" className="cameraDiscoveryItem" key={candidate.id} onClick={() => selectOnvifCandidate(candidate)}>
                            <span>{candidate.host}:{candidate.port || 80}</span>
                            <em>{candidate.xaddr_path || candidate.source || "ws_discovery"}</em>
                          </button>
                        ))}
                      </div>
                    ) : null}
                    {onvifDiscovery && !onvifDiscovery.candidates?.length ? (
                      <div className="cameraPreviewEmpty">{onvifDiscovery.message || copy.discoveryUnavailable}</div>
                    ) : null}
                  </div>
                  {onvifData ? <div className="cameraDeviceInfo">
                    <div><strong>{copy.manufacturer}:</strong> {onvifData.device?.manufacturer || "-"}</div>
                    <div><strong>{copy.model}:</strong> {onvifData.device?.model || "-"}</div>
                    <div><strong>{copy.firmware}:</strong> {onvifData.device?.firmware || "-"}</div>
                    <div><strong>{copy.serialNumber}:</strong> {onvifData.device?.serial_number || "-"}</div>
                    <div><strong>{copy.profileToken}</strong> {form.onvif_profile_token || "-"}</div>
                    <div><strong>{copy.channelId}</strong> {form.onvif_channel_id || "-"}</div>
                    <div><strong>{copy.onvifPath}:</strong> {form.onvif_path || "-"}</div>
                  </div> : (
                    <div className="cameraPreviewEmpty">
                      {copy.onvifAutoSettings}
                    </div>
                  )}
                </section> : null}
              </div>

              <aside className="cameraPreviewPanel">
                <div className="cameraPreviewHead">
                  <h3>{copy.previewTest}</h3>
                </div>
                <div className="toolbar cameraPreviewActions">
                  <button className="button secondary small" onClick={() => runTest()} disabled={testing}>
                    {copy.test}
                  </button>
                  {form.protocol === "onvif" ? (
                    <button className="button secondary small" onClick={loadOnvifProfiles} disabled={onvifBusy}>
                      {copy.onvifProfileShort}
                    </button>
                  ) : null}
                </div>
              {testResult ? (
                <div className="cameraTestResult">
                  <div className="cameraTestPreview">
                    {testResult.preview_url ? (
                      <img src={`${testResult.preview_url}?v=${Date.now()}`} alt="" />
                    ) : (
                      <div className="cameraPreviewPlaceholder">{copy.frameUnavailable}</div>
                    )}
                  </div>
                  <div className="cameraTestStatus">{copy.testOk}</div>
                  <div className="cameraTestDetails">
                    <div><strong>{copy.path}:</strong> <span className="cameraTestPath">{testResult.display_path || copy.rtspPathProvided}</span></div>
                    <div><strong>{copy.transport}:</strong> {testResult.transport}</div>
                    <div><strong>{copy.video}:</strong> {testResult.video ? `${testResult.video.codec || "-"}, ${testResult.video.width || "-"}x${testResult.video.height || "-"}, fps ${testResult.video.fps || "-"}` : copy.no}</div>
                    <div><strong>{copy.audio}:</strong> {testResult.audio ? `${testResult.audio.codec || "-"}, ${copy.channels} ${testResult.audio.channels || "-"}, ${copy.sampleRate} ${testResult.audio.sample_rate || "-"}` : copy.no}</div>
                    <div><strong>{copy.format}:</strong> {testResult.format?.format_name || "-"}</div>
                    <div><strong>{copy.bitrate}:</strong> {testResult.format?.bit_rate || "-"}</div>
                    {testResult.preview_message ? <div className="cameraTestPreviewNote">{testResult.preview_message}</div> : null}
                  </div>
                </div>
              ) : (
                <div className="cameraPreviewEmpty">
                  {copy.runTestHint}
                </div>
              )}
                {onvifStatus ? <div className="cameraModalNote">{onvifStatus}</div> : null}
              </aside>
            </div>

            {form.protocol === "onvif" ? (
              <section className="cameraProfileStrip">
                  {onvifData ? <div className="cameraProfileWorkspace">
                    <div className="cameraProfileGrid">
                    {onvifProfiles.map((profile) => {
                      const isMain = form.onvif_profile_token === profile.token;
                      const isSub = profileMatchesStream(profile, form.rtsp_sub_url);
                      const displayName = profileDisplayName(profile, copy);
                      const tokenLabel = profile.token && profile.token !== displayName ? profile.token : "";
                      return (
                      <div
                        key={profile.token}
                        role="button"
                        tabIndex={0}
                        className={`cameraProfileCard ${selectedOnvifProfileToken === profile.token ? "selected" : ""} ${isMain ? "main" : ""} ${isSub ? "sub" : ""}`}
                        onClick={() => selectOnvifProfile(profile)}
                        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") selectOnvifProfile(profile); }}
                      >
                        <div className="cameraProfileChoice" aria-hidden="true"></div>
                        <div className="cameraProfileBody">
                          <div className="cameraProfileHead">
                            <span title={displayName}>
                              {displayName}
                              {isMain ? <em className="cameraProfileBadge main">Main</em> : null}
                              {isSub ? <em className="cameraProfileBadge sub">Sub</em> : null}
                            </span>
                            {tokenLabel ? <span title={tokenLabel}>{tokenLabel}</span> : null}
                          </div>
                          <div className="cameraProfileMeta">
                            <div className="cameraProfileVideo">{profileVideoSummary(profile, copy)}</div>
                            <div className="cameraProfileAudio">{profileAudioSummary(profile, copy)}</div>
                            {profileConfigWarning(profile, copy) ? <div className="cameraProfileWarn">{profileConfigWarning(profile, copy)}</div> : null}
                            <div className="cameraProfileReady">
                              {profile.rtsp_ready ? copy.rtspReady : copy.rtspPathMissing} <span>|</span> {profile.rtsp_ready ? copy.systemReady : copy.systemCheckRequired}
                            </div>
                          </div>
                        </div>
                      </div>
                    );})}
                    </div>
                    <aside className="cameraProfileAssignPanel">
                      <div className="cameraProfileAssignHead">
                        <h3>Main / Sub</h3>
                        <span>{selectedOnvifProfile ? profileDisplayName(selectedOnvifProfile, copy) : copy.profileNotSelected}</span>
                      </div>
                      <div className="cameraProfileAssignActions">
                        <button type="button" className="button secondary small" onClick={() => useProfileAsMain(selectedOnvifProfile)} disabled={!selectedOnvifProfile}>
                          {copy.useAsMain}
                        </button>
                        <div className="cameraProfileAssignedValue">{profileAssignmentSummary(assignedMainProfile, form.rtsp_main_url, copy)}</div>
                        <button type="button" className="button secondary small" onClick={() => useProfileAsSub(selectedOnvifProfile)} disabled={!selectedOnvifProfile}>
                          {copy.useAsSub}
                        </button>
                        <div className="cameraProfileAssignedValue">{profileAssignmentSummary(assignedSubProfile, form.rtsp_sub_url, copy)}</div>
                      </div>
                      {!selectedOnvifProfile ? <div className="cameraProfileAssignHint">{copy.selectProfileHint}</div> : null}
                    </aside>
                  </div> : (
                    <div className="cameraEmptyState">
                      {copy.loadProfilesHint}
                    </div>
                  )}
              </section>
            ) : null}

            {form.protocol === "onvif" ? (
              <section className="cameraModalSection cameraProfileSettingsSection">
                <div className="cameraSettingsHead">
                  <div>
                    <h3>{copy.selectedProfileSettings}</h3>
                    <p>{copy.writableOnlyHint}</p>
                  </div>
                  <button className="button secondary small" onClick={applyOnvifProfileConfig} disabled={onvifConfigBusy || !canApplySelectedOnvifSettings}>
                    {onvifConfigBusy ? copy.applying : copy.applyProfileSettings}
                  </button>
                </div>

                <div className="cameraSettingsGrid">
                  {profileSettingSlots.map(renderProfileSettingSlot)}
                </div>
              </section>
            ) : null}

            {hasVerifiedCameraPayload ? (
              <div className="cameraVerifiedNotice">
                {copy.cameraVerified}
              </div>
            ) : showManualUnverifiedBypass ? (
              <label className="cameraUnverifiedConfirm">
                <input
                  type="checkbox"
                  checked={Boolean(form.manual_confirm_unverified)}
                  onChange={(e) => patch("manual_confirm_unverified", e.target.checked)}
                />
                <span>{copy.saveUnverified}</span>
              </label>
            ) : null}

            <div className="actions">
              <button className="button secondary" onClick={() => setShowEditor(false)}>{copy.cancel}</button>
              <button className="button" onClick={saveCamera} disabled={busy}>
                {busy ? copy.saving : copy.save}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {deleteModalOpen ? (
        <div className="modalBackdrop">
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 560 }}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>{copy.deleteTitle}</h2>
              <button className="iconCloseButton" onClick={closeDeleteModal} aria-label={copy.close}>×</button>
            </div>
            <p style={{ color: "#475569", marginBottom: 16 }}>
              {copy.cameraLabel}: <strong>{cameraToDelete?.name}</strong>
            </p>

            <label style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18 }}>
              <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
              <span>{copy.deleteRecordsFolder}</span>
            </label>

            <div className="badge warn" style={{ marginBottom: 18 }}>
              {copy.deleteHint}
            </div>

            <div className="actions">
              <button className="button secondary" onClick={closeDeleteModal}>{copy.cancel}</button>
              <button className="button secondary" onClick={confirmDeleteCamera} disabled={busy}>
                {busy ? copy.deleting : copy.delete}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      </div>
    </Layout>
  );
}
