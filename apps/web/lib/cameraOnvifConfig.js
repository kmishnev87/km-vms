export const ONVIF_CONFIG_FIELDS = [
  "codec",
  "resolution",
  "fps",
  "bitrate",
  "iframe_interval",
  "quality",
  "bitrate_mode",
  "codec_profile",
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
  video_encoder_configuration_result_unknown: "profileConfigResultUnknown",
  video_encoder_values_incompatible: "profileConfigIncompatible",
  onvif_authentication_failed: "profileConfigAuthFailed",
  onvif_media_timeout: "profileConfigTimeout",
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
    bitrate_mode: source.bitrate_mode ?? "",
    codec_profile: source.codec_profile ?? "",
    supported: result?.supported || {},
    options_by_codec: result?.options_by_codec || {},
    shared_profile_tokens: result?.shared_profile_tokens || [],
  };
}

export function onvifConfigFieldsFor(draft = {}) {
  const group = draft.options_by_codec?.[draft.codec];
  return group?.by_resolution?.[draft.resolution] || group?.fields || draft.supported || {};
}

export function changeOnvifConfigField(draft, name, value) {
  let next = { ...draft, [name]: value };
  if (name === "codec" || name === "resolution") {
    let fields = onvifConfigFieldsFor(next);
    const resolutions = fields.resolution?.options || [];
    if (resolutions.length && !resolutions.includes(next.resolution)) next.resolution = resolutions[0];
    fields = onvifConfigFieldsFor(next);
    for (const field of ONVIF_CONFIG_FIELDS) {
      if (field === "codec" || field === "resolution") continue;
      const meta = fields[field] || {};
      if (!meta.writable || !present(next[field])) continue;
      const options = meta.options || [];
      if (options.length && !options.some((option) => onvifConfigValuesEqual(field, next[field], option))) {
        next[field] = NUMERIC_FIELDS.has(field)
          ? options.reduce((best, option) => Math.abs(Number(option) - Number(next[field])) < Math.abs(Number(best) - Number(next[field])) ? option : best)
          : options[0];
      }
      if (NUMERIC_FIELDS.has(field) && meta.range) {
        let numeric = Number(next[field]);
        if (meta.range.min != null) numeric = Math.max(numeric, Number(meta.range.min));
        if (meta.range.max != null) numeric = Math.min(numeric, Number(meta.range.max));
        next[field] = numeric;
      }
    }
  }
  return { ...next, supported: onvifConfigFieldsFor(next) };
}

export function invalidOnvifConfigField(draft = {}, baseline = null) {
  const fields = onvifConfigFieldsFor(draft);
  return ONVIF_CONFIG_FIELDS.find((name) => {
    const meta = fields[name] || {};
    if (!meta.writable) return false;
    const value = draft[name];
    if (baseline && onvifConfigValuesEqual(name, value, baseline[name])) return false;
    if (!present(value)) return true;
    if (meta.options?.length && !meta.options.some((option) => onvifConfigValuesEqual(name, value, option))) return true;
    if (!NUMERIC_FIELDS.has(name)) return false;
    const number = Number(value);
    return !Number.isFinite(number)
      || (["bitrate", "iframe_interval"].includes(name) || meta.number_type === "integer") && !Number.isInteger(number)
      || meta.range?.min != null && number < Number(meta.range.min)
      || meta.range?.max != null && number > Number(meta.range.max);
  }) || null;
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
  if (result.current_read && !result.options_read) return "read_only";
  if (status === "error" || status === "unavailable") return "unavailable";
  return "not_checked";
}

export function onvifConfigDisplayTruth(state, dirty) {
  if (state === "mismatch" || state === "unavailable") return state;
  return dirty ? "changed" : state === "changed" ? "current" : state;
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
