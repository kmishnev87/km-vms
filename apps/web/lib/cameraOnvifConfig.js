export const ONVIF_CONFIG_FIELDS = [
  "codec",
  "resolution",
  "fps",
  "bitrate",
  "iframe_interval",
  "quality",
];

const NUMERIC_FIELDS = new Set(["fps", "bitrate", "iframe_interval", "quality"]);

const ONVIF_CONFIG_ERROR_COPY_KEYS = {
  onvif_profile_not_found: "profileConfigUnavailable",
  video_encoder_configuration_mismatch: "profileConfigMismatch",
  video_encoder_configuration_read_failed: "profileConfigUnavailable",
  video_encoder_configuration_set_failed: "profileConfigSetFailed",
  video_encoder_configuration_token_mismatch: "profileConfigUnavailable",
  video_encoder_configuration_token_unavailable: "profileConfigUnavailable",
  video_encoder_configuration_unavailable: "profileConfigUnavailable",
  video_encoder_configuration_verify_failed: "profileConfigVerifyFailed",
};

function present(value) {
  return value !== undefined && value !== null && value !== "";
}

export function onvifConfigFromResult(result = {}) {
  const source = result?.config || {};
  return {
    codec: source.codec ?? "",
    resolution: source.resolution
      ?? (source.width && source.height ? `${source.width}x${source.height}` : ""),
    fps: source.fps ?? "",
    bitrate: source.bitrate ?? "",
    iframe_interval: source.iframe_interval ?? "",
    quality: source.quality ?? "",
    supported: result?.supported || {},
  };
}

export function onvifConfigValuesEqual(name, left, right) {
  if (!present(left) || !present(right)) return !present(left) && !present(right);
  if (NUMERIC_FIELDS.has(name)) {
    const leftNumber = Number(left);
    const rightNumber = Number(right);
    return Number.isFinite(leftNumber)
      && Number.isFinite(rightNumber)
      && Math.abs(leftNumber - rightNumber) < 1e-9;
  }
  return String(left).trim().toLowerCase() === String(right).trim().toLowerCase();
}

export function buildOnvifConfigDiff(draft = {}, baseline = {}, supported = {}) {
  const result = {};
  for (const name of ONVIF_CONFIG_FIELDS) {
    const meta = supported?.[name] || {};
    const value = draft?.[name];
    if (meta.writable !== true || !present(value)) continue;
    if (onvifConfigValuesEqual(name, value, baseline?.[name])) continue;
    result[name] = NUMERIC_FIELDS.has(name) ? Number(value) : String(value);
  }
  return result;
}

export function onvifConfigTruthState(result = {}) {
  const status = String(result?.status || "").toLowerCase();
  if (status === "ok" || status === "unsupported") return "current";
  if (status === "error" || status === "unavailable") return "unavailable";
  return "not_checked";
}

export function normalizeOnvifConfigError(error = {}, copy = {}) {
  const code = String(
    error?.code
      || error?.detail?.code
      || error?.data?.detail?.code
      || "",
  ).trim();
  const copyKey = ONVIF_CONFIG_ERROR_COPY_KEYS[code];
  if (copyKey) return copy?.[copyKey] || copy?.actionFailed || "";
  if (code.startsWith("video_encoder_")) return copy?.actionFailed || "";
  return null;
}
