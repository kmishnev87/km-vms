"use client";

import { useEffect, useRef, useState } from "react";
import Layout from "../../components/Layout";
import OperatorProblemBanners from "../../components/OperatorProblemBanners";
import { apiFetch } from "../../lib/api";

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

function profileVideoSummary(profile) {
  const video = configVideo(profile);
  const parts = [];
  if (hasValue(video.codec)) parts.push(video.codec);
  if (hasValue(video.resolution) && video.resolution !== "-") parts.push(video.resolution);
  if (hasValue(video.fps)) parts.push(`fps ${video.fps}`);
  if (parts.length) return `ONVIF video: ${parts.join(" · ")}`;
  if (profile?.rtsp_probe?.video) {
    const probe = profile.rtsp_probe.video;
    const probeParts = [];
    if (hasValue(probe.codec)) probeParts.push(probe.codec);
    if (hasValue(probe.width) && hasValue(probe.height)) probeParts.push(`${probe.width}x${probe.height}`);
    if (hasValue(probe.fps)) probeParts.push(`fps ${probe.fps}`);
    if (probeParts.length) return `RTSP probe video: ${probeParts.join(" · ")}`;
  }
  return "Video parameters unavailable";
}

function profileAudioSummary(profile) {
  const audio = profile?.audio || profile?.rtsp_probe?.audio;
  if (!audio) return "Audio: none";
  const parts = [];
  if (hasValue(audio.codec)) parts.push(audio.codec);
  if (hasValue(audio.channels)) parts.push(`channels ${audio.channels}`);
  if (hasValue(audio.sample_rate)) parts.push(`sample_rate ${audio.sample_rate}`);
  return parts.length ? `Audio: ${parts.join(" · ")}` : "Audio: none";
}

function profileNameText(profile) {
  return `${profile?.name || ""} ${profile?.token || ""}`.toLowerCase();
}

function profileDisplayName(profile) {
  return String(profile?.name || profile?.token || "Профиль").trim();
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

function profileConfigWarning(profile) {
  const warnings = profile?.warnings || [];
  if (profile?.video_config_state === "unavailable" || warnings.some((item) => String(item).includes("video_encoder"))) {
    return "ONVIF video config unavailable";
  }
  return "";
}

function profileByToken(data, token) {
  return (data?.profiles || []).find((item) => item.token === token) || null;
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
  if (protocol === "onvif") {
    payload.rtsp_host = rtspReachableHost(normalizedSource);
    payload.rtsp_port = rtspReachablePort(normalizedSource);
  } else {
    delete payload.rtsp_host;
    delete payload.rtsp_port;
  }
  return payload;
}

function normalizeCameraError(message) {
  const text = String(message || "").trim();
  if (!text) return "Не удалось выполнить действие. Проверьте параметры и повторите попытку.";
  if (text.includes("ffprobe") || text.includes("Invalid data") || text.includes("Server returned")) {
    return "Камера не ответила корректно. Проверьте RTSP path, логин, пароль и сетевой доступ.";
  }
  if (text.length > 180) {
    return "Не удалось подключиться к камере. Проверьте адрес, порт и параметры потока.";
  }
  return text;
}

function normalizeRuntimeError(message) {
  const text = String(message || "").trim();
  if (!text) return "";
  if (text.includes("401") || text.toLowerCase().includes("auth")) {
    return "Ошибка подключения: проверьте логин и пароль камеры.";
  }
  if (text.toLowerCase().includes("timeout")) {
    return "Камера не отвечает: проверьте сеть и RTSP path.";
  }
  if (text.toLowerCase().includes("storage")) {
    return "Запись недоступна: хранилище недоступно.";
  }
  if (text.toLowerCase().includes("ffmpeg")) {
    return "Запись недоступна: ошибка видеопроцесса.";
  }
  if (text.length > 140) {
    return "Запись недоступна: проверьте параметры камеры и хранилище.";
  }
  return text;
}

function getCameraRuntimeBadge(camera, runtime, recorderStatus, storageAvailable) {
  if (!camera.enabled || runtime?.enabled === false || camera.status === "disabled") {
    return { text: "Отключена", cls: "warn" };
  }

  if (storageAvailable === false) {
    return { text: "Ошибка", cls: "err" };
  }

  if (!recorderStatus) {
    if (camera.status === "error") return { text: "Ошибка", cls: "err" };
    return { text: "Статус неизвестен", cls: "warn" };
  }

  const runtimeError = normalizeRuntimeError(runtime?.last_error || runtime?.camera_last_error || recorderStatus?.last_error);
  const jobState = String(runtime?.job_state || camera.status || "").toLowerCase();
  const currentFailure = runtime?.current_failure === true;

  if (recorderStatus?.heartbeat?.status === "stale_or_unavailable") {
    return { text: "Статус неизвестен", cls: "warn" };
  }

  if (jobState === "restarting") return { text: "Перезапуск", cls: "warn" };
  if (jobState === "starting") return { text: "Запускается", cls: "warn" };
  if (jobState === "stopping") return { text: "Останавливается", cls: "warn" };
  if (jobState === "recording" && !currentFailure) return { text: "Идёт запись", cls: "ok" };
  if (currentFailure || runtimeError || jobState === "error") {
    return { text: "Ошибка", cls: "err" };
  }
  if (jobState === "recording") return { text: "Идёт запись", cls: "ok" };
  if (runtime?.recording_mode && runtime.recording_mode !== "always") {
    return { text: "Ожидание записи", cls: "warn" };
  }

  return { text: "Ожидание записи", cls: "warn" };
}

export default function CamerasPage() {
  const [cameras, setCameras] = useState([]);
  const [storage, setStorage] = useState(null);
  const [recorderStatus, setRecorderStatus] = useState(null);
  const [showEditor, setShowEditor] = useState(false);
  const [editorMode, setEditorMode] = useState("create");
  const [editingCameraId, setEditingCameraId] = useState(null);
  const [form, setForm] = useState(initialForm);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState(null);
  const [testing, setTesting] = useState(false);
  const [onvifBusy, setOnvifBusy] = useState(false);
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

  async function load() {
    try {
      const [cams, st, recorder] = await Promise.all([
        apiFetch("/cameras"),
        apiFetch("/storage/status"),
        apiFetch("/system/recorder/status").catch(() => null),
      ]);
      setCameras(cams);
      setStorage(st);
      setRecorderStatus(recorder);
    } catch (err) {
      setError(normalizeCameraError(err.message));
    }
  }

  const storagePathChecks = storage?.storage_path_checks || {};
  const storageAvailable = storagePathChecks.path_exists ?? storage?.storage_root_exists;
  const recorderCameraMap = new Map(
    (recorderStatus?.camera_recording_states || []).map((item) => [String(item.camera_id), item])
  );

  useEffect(() => {
    load();
    const timer = setInterval(load, 5000);
    return () => {
      clearInterval(timer);
      window.clearTimeout(profileToastTimerRef.current);
    };
  }, []);

  function showProfileToast(title, text) {
    setProfileToast({ title, text, variant: "success" });
    window.clearTimeout(profileToastTimerRef.current);
    profileToastTimerRef.current = window.setTimeout(() => setProfileToast(null), 2200);
  }

  function patch(key, value) {
    setForm((prev) => {
      const next = {
        ...prev,
        [key]: value,
        ...(PREVIEW_SENSITIVE_FIELDS.has(key) ? { preview_token: null } : {}),
      };

      if (key === "protocol" && value === "onvif") {
        next.rtsp_host = prev.host || "";
        next.rtsp_port = prev.rtsp_port || 554;
      }
      if (prev.protocol === "onvif" && key === "host" && (!prev.rtsp_host || prev.rtsp_host === prev.host)) {
        next.rtsp_host = value;
      }

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
    setTestResult(null);
    setOnvifData(null);
    setOnvifConfig(initialOnvifConfig);
    setSelectedOnvifProfileToken("");
    setOnvifStatus("");
    setShowEditor(true);
  }

  function openEdit(camera) {
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
      rtsp_port: camera.rtsp_reachable_port || rtspPortFromValue(camera.rtsp_main_url || camera.rtsp_sub_url || "") || 554,
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
    }));
    setError("");
    setTestResult(null);
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
      if (result.preview_token) {
        patch("preview_token", result.preview_token);
      }
      updateProfileProbeState(formOverride.onvif_profile_token || selectedOnvifProfileToken, result);
      return result;
    } catch (err) {
      setError(normalizeCameraError(err.message));
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
        rtsp_port: result.rtsp_reachable?.port || rtspReachablePort(form),
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
        setOnvifStatus("Main поток выбран и проверен через RTSP reachable endpoint.");
      } else {
        setOnvifStatus("Профили загружены. Для автотеста укажи RTSP reachable host/port и путь Main потока.");
      }
    } catch (err) {
      setError(normalizeCameraError(err.message));
      setOnvifData(null);
    } finally {
      setOnvifBusy(false);
    }
  }

  function useProfileAsMain(profile) {
    const token = profile.token || "";
    const name = profileDisplayName(profile);
    setForm((prev) => normalizeCameraStreamDefaults({
      ...prev,
      onvif_profile_token: token,
      rtsp_main_url: profile.stream_path || prettyRtspValue(profile.stream_uri || ""),
      default_record_stream: "main",
      preview_token: null,
    }));
    setTestResult(null);
    setSelectedOnvifProfileToken(token);
    if (token) {
      loadOnvifProfileConfig(token);
    }
    showProfileToast("Main stream выбран", name);
  }

  function useProfileAsSub(profile) {
    const token = profile.token || "";
    const name = profileDisplayName(profile);
    setForm((prev) => normalizeCameraStreamDefaults({
      ...prev,
      rtsp_sub_url: profile.stream_path || prettyRtspValue(profile.stream_uri || ""),
      onvif_profile_token: prev.onvif_profile_token || token,
      default_live_stream: "sub",
      preview_token: null,
    }));
    setTestResult(null);
    setSelectedOnvifProfileToken(token);
    if (token) {
      loadOnvifProfileConfig(token);
    }
    showProfileToast("Sub stream выбран", name);
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
      setError("Сначала выбери ONVIF профиль");
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
      setError(normalizeCameraError(err.message));
      return null;
    } finally {
      setOnvifConfigBusy(false);
    }
  }

  async function applyOnvifProfileConfig() {
    const profileToken = selectedOnvifProfileToken || form.onvif_profile_token;
    if (!profileToken) {
      setError("Сначала выбери ONVIF профиль");
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
        setError("No writable ONVIF settings available for the selected profile.");
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
      setError(normalizeCameraError(err.message));
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
      setOnvifData(null);
      setOnvifConfig(initialOnvifConfig);
      setSelectedOnvifProfileToken("");
      setOnvifStatus("");
      await load();
    } catch (err) {
      setError(normalizeCameraError(err.message));
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
      setError(normalizeCameraError(err.message));
    }
  }

  async function confirmDeleteCamera() {
    if (!cameraToDelete) return;

    setError("");
    setBusy(true);
    try {
      await apiFetch(
        `/cameras/${cameraToDelete.id}?delete_files=${deleteFiles ? "true" : "false"}`,
        { method: "DELETE" }
      );
      closeDeleteModal();
      await load();
    } catch (err) {
      setError(normalizeCameraError(err.message));
    } finally {
      setBusy(false);
    }
  }

  const canApplySelectedOnvifSettings = canApplyOnvifSettings(onvifConfig, selectedOnvifProfileToken);
  const formStreamOptions = availableCameraStreams(form);

  function renderProfileSettingSlot(slot) {
    const meta = profileSettingMeta(onvifConfig.supported, slot.key);
    const value = profileSettingDisplayValue(onvifConfig, slot.key);
    const state = profileSettingState(meta, value, slot.requiresOptions);
    const hasOptions = Array.isArray(meta.options) && meta.options.length > 0;
    const range = meta.range;
    const statusText = state === "editable" ? "Доступно для записи" : state === "readonly" ? "Только чтение" : "Недоступно";

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
        {state === "editable" && range ? <div className="cameraSettingHint">Диапазон: {range.min ?? "?"} - {range.max ?? "?"}</div> : null}
        {state === "unavailable" ? <div className="cameraSettingHint">{slot.unavailable}</div> : null}
      </div>
    );
  }

  const profileSettingSlots = [
    { key: "codec", label: "Кодек / сжатие", empty: "Не получено", unavailable: "Камера не отдала список кодеков.", requiresOptions: true },
    { key: "encode_strategy", label: "Стратегия кодирования", empty: "Не получено", unavailable: "Не возвращается стандартным ONVIF ответом." },
    { key: "resolution", label: "Разрешение", empty: "Не получено", unavailable: "Камера не отдала варианты разрешения." },
    { key: "fps", label: "Частота кадров", empty: "Не получено", unavailable: "Диапазон FPS не получен.", numeric: true },
    { key: "bitrate_type", label: "Тип битрейта", empty: "Не получено", unavailable: "CBR/VBR не возвращается этой камерой." },
    { key: "quality", label: "Качество VBR", empty: "Не получено", unavailable: "Диапазон качества не получен.", numeric: true },
    { key: "bitrate", label: "Макс. битрейт", empty: "Не получено", unavailable: "Диапазон битрейта не получен.", numeric: true },
    { key: "iframe_interval", label: "Интервал I-кадров", empty: "Не получено", unavailable: "Диапазон GOP/I-frame не получен.", numeric: true },
    { key: "codec_profile", label: "Профиль кодека", empty: "Не получено", unavailable: "Профиль кодека не возвращается." },
    { key: "audio_codec", label: "Аудио", empty: "Не получено", unavailable: "ONVIF encoder audio options не получены." },
  ];

  return (
    <Layout>
      <div className="standardPage">
      <div className="pageHeader">
        <div>
          <h1 className="pageTitle">Камеры</h1>
          <div className="pageSubtitle">Добавление, редактирование и управление камерами</div>
        </div>
        <button className="button" onClick={openCreate}>Добавить камеру</button>
      </div>

      <OperatorProblemBanners domains={["cameras", "recorder"]} className="pageWarnings" limit={4} />

      {error && !showEditor ? <div className="badge err" style={{ marginBottom: 14 }}>{error}</div> : null}

      <div className="cameraCards">
        {!cameras.length ? (
          <div className="card">Камеры ещё не добавлены.</div>
        ) : (
          cameras.map((camera) => {
            const runtime = recorderCameraMap.get(String(camera.id));
            const badge = getCameraRuntimeBadge(camera, runtime, recorderStatus, storageAvailable);
            return (
              <div className="cameraCard card" key={camera.id}>
                <div className="cameraCardGrid">
                  <div className="cameraField cameraNameField">
                    <div className="cameraFieldLabel">Камера</div>
                    <div className="cameraPrimary" title={camera.name}>{camera.name}</div>
                    <div className="cameraSecondary" title={`папка: ${camera.storage_folder_name}`}>папка: {camera.storage_folder_name}</div>
                    <div className="cameraActions">
                      <button className="cameraIconButton" onClick={() => openEdit(camera)} title="Редактировать" aria-label="Редактировать">
                        {"\u270e"}
                      </button>
                      <button className="cameraIconButton" onClick={() => toggleCamera(camera)} title={camera.enabled ? "Отключить" : "Включить"} aria-label={camera.enabled ? "Отключить" : "Включить"}>
                        {camera.enabled ? "\u23fb" : "\u2713"}
                      </button>
                      <button className="cameraIconButton danger" onClick={() => openDeleteModal(camera)} title="Удалить" aria-label="Удалить">
                        {"\ud83d\uddd1"}
                      </button>
                    </div>
                  </div>

                  <div className="cameraPreviewField" aria-label="Кадр камеры">
                    {camera.preview_url ? (
                      <img src={camera.preview_url} alt="" />
                    ) : (
                      <div className="cameraPreviewPlaceholder">Нет кадра</div>
                    )}
                  </div>

                  <div className="cameraMetaGrid">
                    <div className="cameraField">
                      <div className="cameraFieldLabel">Протокол</div>
                      <div>{camera.protocol?.toUpperCase()}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">Адрес</div>
                      <div>{camera.host}:{camera.port}</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">Сегм.</div>
                      <div>{camera.segment_minutes} мин</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">Хран.</div>
                      <div>{camera.retention_days} дн</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">Лимит</div>
                      <div>{camera.storage_quota_gb} ГБ</div>
                    </div>

                    <div className="cameraField">
                      <div className="cameraFieldLabel">Статус</div>
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
                <h2>{editorMode === "create" ? "Добавить камеру" : "Редактировать камеру"}</h2>
                <p>Подключение, тест и параметры записи</p>
              </div>
              <button className="iconCloseButton" onClick={() => setShowEditor(false)} aria-label="Закрыть">×</button>
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
                  <h3>1. Подключение</h3>
                  <div className="formGrid">
                    <div>
                      <div className="formLabel">Имя камеры</div>
                      <input className="input" value={form.name} onChange={(e) => patch("name", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">Протокол</div>
                      <select className="select" value={form.protocol} onChange={(e) => patch("protocol", e.target.value)}>
                        <option value="rtsp">RTSP</option>
                        <option value="onvif">ONVIF</option>
                      </select>
                    </div>
                    <div>
                      <div className="formLabel">{form.protocol === "onvif" ? "ONVIF Host / IP" : "IP / Host"}</div>
                      <input className="input" value={form.host} onChange={(e) => patch("host", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">{form.protocol === "onvif" ? "ONVIF Port" : "RTSP Port"}</div>
                      <input className="input" value={form.port} onChange={(e) => patch("port", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">Логин</div>
                      <input className="input" value={form.username} onChange={(e) => patch("username", e.target.value)} />
                    </div>
                    <div>
                      <div className="formLabel">Пароль {editorMode === "edit" ? "(оставь пустым, если не менять)" : ""}</div>
                      <input className="input" type="password" value={form.password} onChange={(e) => patch("password", e.target.value)} />
                    </div>
                  </div>
                </section>

                <section className="cameraModalSection">
                  <h3>2. Потоки и запись</h3>
                  <div className="cameraStreamsGrid">
                    <div className="cameraStreamField wide">
                      <div className="formLabel">RTSP Main Path / URL</div>
                      <input className="input" value={form.rtsp_main_url} onChange={(e) => patch("rtsp_main_url", e.target.value)} />
                    </div>
                    <div className="cameraStreamField wide">
                      <div className="formLabel">RTSP Sub Path / URL</div>
                      <input className="input" value={form.rtsp_sub_url} onChange={(e) => patch("rtsp_sub_url", e.target.value)} />
                    </div>
                    <div className="cameraStreamPolicyGrid">
                      <div className="cameraStreamField policy">
                        <div className="formLabel">RTSP reachable host</div>
                        <input className="input" value={form.rtsp_host} onChange={(e) => patch("rtsp_host", e.target.value)} />
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">RTSP reachable port</div>
                        <input className="input" type="number" min="1" value={form.rtsp_port} onChange={(e) => patch("rtsp_port", e.target.value)} />
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">RTSP Transport</div>
                        <select className="select" value={form.rtsp_transport} onChange={(e) => patch("rtsp_transport", e.target.value)}>
                          <option value="tcp">TCP</option>
                          <option value="udp">UDP</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">Поток для записи</div>
                        <select className="select" value={form.default_record_stream} onChange={(e) => patch("default_record_stream", e.target.value)}>
                          {formStreamOptions.map((stream) => (
                            <option key={stream.key} value={stream.key}>{stream.label}</option>
                          ))}
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">Поток для онлайн</div>
                        <select className="select" value={form.default_live_stream} onChange={(e) => patch("default_live_stream", e.target.value)}>
                          {formStreamOptions.map((stream) => (
                            <option key={stream.key} value={stream.key}>{stream.label}</option>
                          ))}
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">Режим записи</div>
                        <select className="select" value={form.recording_mode} onChange={(e) => patch("recording_mode", e.target.value)}>
                          <option value="always">Постоянно</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">Длительность сегмента</div>
                        <select className="select" value={form.segment_minutes} onChange={(e) => patch("segment_minutes", e.target.value)}>
                          <option value="5">5 минут</option>
                          <option value="15">15 минут</option>
                          <option value="30">30 минут</option>
                          <option value="45">45 минут</option>
                          <option value="60">60 минут</option>
                          <option value="120">120 минут</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">Срок хранения</div>
                        <select className="select" value={form.retention_days} onChange={(e) => patch("retention_days", e.target.value)}>
                          <option value="7">7 дней</option>
                          <option value="14">14 дней</option>
                          <option value="21">21 день</option>
                          <option value="30">30 дней</option>
                        </select>
                      </div>
                      <div className="cameraStreamField policy">
                        <div className="formLabel">Лимит архива (ГБ)</div>
                        <input className="input" type="number" min="1" value={form.storage_quota_gb} onChange={(e) => patch("storage_quota_gb", e.target.value)} />
                      </div>
                    </div>
                  </div>
                </section>

                {form.protocol === "onvif" ? <section className="cameraModalSection cameraOnvifSection">
                  <h3>3. ONVIF</h3>
                  {onvifData ? <div className="cameraDeviceInfo">
                    <div><strong>Производитель:</strong> {onvifData.device?.manufacturer || "-"}</div>
                    <div><strong>Модель:</strong> {onvifData.device?.model || "-"}</div>
                    <div><strong>Прошивка:</strong> {onvifData.device?.firmware || "-"}</div>
                    <div><strong>Серийный номер:</strong> {onvifData.device?.serial_number || "-"}</div>
                    <div><strong>Profile Token:</strong> {form.onvif_profile_token || "-"}</div>
                    <div><strong>Channel ID:</strong> {form.onvif_channel_id || "-"}</div>
                    <div><strong>ONVIF Path:</strong> {form.onvif_path || "-"}</div>
                  </div> : (
                    <div className="cameraPreviewEmpty">
                      ONVIF-параметры будут получены автоматически после чтения профилей.
                    </div>
                  )}
                </section> : null}
              </div>

              <aside className="cameraPreviewPanel">
                <div className="cameraPreviewHead">
                  <h3>Предпросмотр и тест</h3>
                </div>
                <div className="toolbar cameraPreviewActions">
                  <button className="button secondary small" onClick={() => runTest()} disabled={testing}>
                    Тест
                  </button>
                  {form.protocol === "onvif" ? (
                    <button className="button secondary small" onClick={loadOnvifProfiles} disabled={onvifBusy}>
                      ONVIF проф.
                    </button>
                  ) : null}
                </div>
              {testResult ? (
                <div className="cameraTestResult">
                  <div className="cameraTestPreview">
                    {testResult.preview_url ? (
                      <img src={`${testResult.preview_url}?v=${Date.now()}`} alt="" />
                    ) : (
                      <div className="cameraPreviewPlaceholder">Кадр недоступен</div>
                    )}
                  </div>
                  <div className="cameraTestStatus">Тест: OK</div>
                  <div className="cameraTestDetails">
                    <div><strong>Путь:</strong> <span className="cameraTestPath">{testResult.display_path || "RTSP path указан"}</span></div>
                    <div><strong>Транспорт:</strong> {testResult.transport}</div>
                    <div><strong>Видео:</strong> {testResult.video ? `${testResult.video.codec || "-"}, ${testResult.video.width || "-"}x${testResult.video.height || "-"}, fps ${testResult.video.fps || "-"}` : "нет"}</div>
                    <div><strong>Аудио:</strong> {testResult.audio ? `${testResult.audio.codec || "-"}, channels ${testResult.audio.channels || "-"}, sample_rate ${testResult.audio.sample_rate || "-"}` : "нет"}</div>
                    <div><strong>Формат:</strong> {testResult.format?.format_name || "-"}</div>
                    <div><strong>Битрейт:</strong> {testResult.format?.bit_rate || "-"}</div>
                    {testResult.preview_message ? <div className="cameraTestPreviewNote">{testResult.preview_message}</div> : null}
                  </div>
                </div>
              ) : (
                <div className="cameraPreviewEmpty">
                  Нажми «Тест», чтобы проверить подключение и получить параметры потока.
                </div>
              )}
                {onvifStatus ? <div className="cameraModalNote">{onvifStatus}</div> : null}
              </aside>
            </div>

            {form.protocol === "onvif" ? (
              <section className="cameraProfileStrip">
                  {onvifData ? <div className="cameraProfileGrid">
                    {onvifData.profiles?.map((profile) => {
                      const role = profileRole(profile, onvifData);
                      const isMain = form.onvif_profile_token === profile.token;
                      const isSub = form.rtsp_sub_url && (form.rtsp_sub_url === profile.stream_path || form.rtsp_sub_url === prettyRtspValue(profile.stream_uri || ""));
                      const displayName = profileDisplayName(profile);
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
                            <span title={displayName}>{displayName}</span>
                            {tokenLabel ? <span title={tokenLabel}>{tokenLabel}</span> : null}
                          </div>
                          <div className="cameraProfileMeta">
                            <div className="cameraProfileVideo">{profileVideoSummary(profile)}</div>
                            <div className="cameraProfileAudio">{profileAudioSummary(profile)}</div>
                            {profileConfigWarning(profile) ? <div className="cameraProfileWarn">{profileConfigWarning(profile)}</div> : null}
                            <div className="cameraProfileReady">RTSP: {profile.rtsp_ready ? "Ready" : "Path missing"} <span>|</span> System: {profile.rtsp_ready ? "Ready" : "Check required"}</div>
                          </div>
                        </div>
                        <div className="toolbar cameraProfileActions">
                          {role === "main" ? (
                            <button type="button" className={`button secondary small ${isMain ? "active" : ""}`} onClick={(e) => { e.stopPropagation(); useProfileAsMain(profile); }}>
                              Использовать как Main
                            </button>
                          ) : null}
                          {role === "sub" ? (
                            <button type="button" className={`button secondary small ${isSub ? "active" : ""}`} onClick={(e) => { e.stopPropagation(); useProfileAsSub(profile); }}>
                              Использовать как Sub
                            </button>
                          ) : null}
                        </div>
                      </div>
                    );})}
                  </div> : (
                    <div className="cameraEmptyState">
                      Загрузите ONVIF профили, чтобы выбрать Main/Sub и прочитать настройки профиля.
                    </div>
                  )}
              </section>
            ) : null}

            {form.protocol === "onvif" ? (
              <section className="cameraModalSection cameraProfileSettingsSection">
                <div className="cameraSettingsHead">
                  <div>
                    <h3>Настройки выбранного профиля</h3>
                    <p>Редактируются только параметры, которые камера отдала как writable.</p>
                  </div>
                  <button className="button secondary small" onClick={applyOnvifProfileConfig} disabled={onvifConfigBusy || !canApplySelectedOnvifSettings}>
                    {onvifConfigBusy ? "Применяем..." : "Применить настройки профиля"}
                  </button>
                </div>

                <div className="cameraSettingsGrid">
                  {profileSettingSlots.map(renderProfileSettingSlot)}
                </div>
              </section>
            ) : null}

            <div className="actions">
              <button className="button secondary" onClick={() => setShowEditor(false)}>Отмена</button>
              <button className="button" onClick={saveCamera} disabled={busy}>
                {busy ? "Сохраняем..." : "Сохранить"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {deleteModalOpen ? (
        <div className="modalBackdrop">
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 560 }}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>Удалить камеру</h2>
              <button className="iconCloseButton" onClick={closeDeleteModal} aria-label="Закрыть">×</button>
            </div>
            <p style={{ color: "#475569", marginBottom: 16 }}>
              Камера: <strong>{cameraToDelete?.name}</strong>
            </p>

            <label style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 18 }}>
              <input type="checkbox" checked={deleteFiles} onChange={(e) => setDeleteFiles(e.target.checked)} />
              <span>Удалить также все записи и папку камеры</span>
            </label>

            <div className="badge warn" style={{ marginBottom: 18 }}>
              Если галочка не установлена, камера удалится только из системы, а архив останется на диске.
            </div>

            <div className="actions">
              <button className="button secondary" onClick={closeDeleteModal}>Отмена</button>
              <button className="button secondary" onClick={confirmDeleteCamera} disabled={busy}>
                {busy ? "Удаляем..." : "Удалить"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      </div>
    </Layout>
  );
}
