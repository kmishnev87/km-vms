const DEFAULT_LOCALE = "ru";
const LOCALE_ALIASES = {
  ru: "ru",
  en: "en",
  zh: "zh-CN",
  "zh-cn": "zh-CN",
  zh_cn: "zh-CN",
  cn: "zh-CN",
  chinese: "zh-CN",
};

let normalizeLocaleImpl = (value, fallback = DEFAULT_LOCALE) => {
  const raw = String(value || "").trim();
  if (raw === "ru" || raw === "en" || raw === "zh-CN") return raw;
  return LOCALE_ALIASES[raw.toLowerCase()] || fallback;
};
let translateTextImpl = (_locale, text) => text;

export function configureSettingsPageHelpers({ normalizeLocale, translateText } = {}) {
  if (typeof normalizeLocale === "function") normalizeLocaleImpl = normalizeLocale;
  if (typeof translateText === "function") translateTextImpl = translateText;
}

export const UTC_TIMEZONES = Array.from({ length: 27 }, (_, index) => {
  const offset = index - 12;
  const sign = offset >= 0 ? "+" : "-";
  const label = offset === 0 ? "UTC+00:00" : `UTC${sign}${String(Math.abs(offset)).padStart(2, "0")}:00`;
  const value = offset === 0 ? "UTC" : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`;
  return { offset, label, value };
});

export const HARDWARE_OPTIONS = ["auto", "qsv", "amf", "nvenc", "cpu", "vaapi"];
export const AUDIT_CATEGORIES = ["auth", "users", "settings", "cameras", "live", "records", "chronology", "security", "diagnostics", "system", "recorder", "storage", "retention", "reconciliation"];
export const AUDIT_SEVERITIES = ["info", "warning", "error", "security"];
export const AUDIT_LIMIT = 50;
const AUDIT_LABELS = {
  category: {
    auth: { ru: "Авторизация", en: "Auth", "zh-CN": "授权" },
    users: { ru: "Пользователи", en: "Users", "zh-CN": "用户" },
    settings: { ru: "Настройки", en: "Settings", "zh-CN": "设置" },
    cameras: { ru: "Камеры", en: "Cameras", "zh-CN": "摄像机" },
    live: { ru: "Live", en: "Live", "zh-CN": "实时" },
    records: { ru: "Записи", en: "Records", "zh-CN": "录像" },
    chronology: { ru: "Хронология", en: "Chronology", "zh-CN": "时间轴" },
    security: { ru: "Безопасность", en: "Security", "zh-CN": "安全" },
    diagnostics: { ru: "Диагностика", en: "Diagnostics", "zh-CN": "诊断" },
    system: { ru: "Система", en: "System", "zh-CN": "系统" },
    recorder: { ru: "Recorder", en: "Recorder", "zh-CN": "录像服务" },
    storage: { ru: "Хранилище", en: "Storage", "zh-CN": "存储" },
    retention: { ru: "Retention", en: "Retention", "zh-CN": "保留" },
    reconciliation: { ru: "Reconciliation", en: "Reconciliation", "zh-CN": "一致性检查" },
  },
  severity: {
    info: { ru: "Инфо", en: "Info", "zh-CN": "信息" },
    warning: { ru: "Предупреждение", en: "Warning", "zh-CN": "告警" },
    error: { ru: "Ошибка", en: "Error", "zh-CN": "错误" },
    security: { ru: "Security", en: "Security", "zh-CN": "安全" },
  },
};
const BACKEND_LABELS = {
  auto: { ru: "Автоматический режим", en: "Automatic mode", "zh-CN": "自动模式" },
  qsv: { ru: "Intel Quick Sync / QSV", en: "Intel Quick Sync / QSV", "zh-CN": "Intel Quick Sync / QSV" },
  vaapi: { ru: "VAAPI", en: "VAAPI", "zh-CN": "VAAPI" },
  amf: { ru: "AMD AMF", en: "AMD AMF", "zh-CN": "AMD AMF" },
  nvenc: { ru: "NVIDIA NVENC/NVDEC", en: "NVIDIA NVENC/NVDEC", "zh-CN": "NVIDIA NVENC/NVDEC" },
  cpu: { ru: "Резервный режим CPU", en: "CPU fallback", "zh-CN": "CPU 后备模式" },
};

function localizedValue(value, lang) {
  if (!value || typeof value !== "object") return value;
  if (value[lang]) return value[lang];
  if (lang === "zh-CN") return translateTextImpl("zh-CN", value.ru || value.en || "");
  return value.ru || value.en || "";
}

export function languageOf(settings) {
  return normalizeLocaleImpl(settings?.language);
}

export function backendLabel(value, lang) {
  const key = value || "auto";
  return localizedValue(BACKEND_LABELS[key], lang) || key;
}

export function roleLabel(role, t) {
  if (!role) return "-";
  if (role === "owner") return t.roleOwner;
  if (role === "admin") return t.roleAdmin;
  if (role === "operator") return t.roleOperator;
  return t.roleViewer;
}

export function passwordLengthMessage(lang) {
  return lang === "en"
    ? "Password must be at least 8 characters."
    : "Пароль должен быть не менее 8 символов.";
}

export function passwordConfirmMessage(lang) {
  return lang === "en"
    ? "Passwords do not match."
    : "Пароли не совпадают.";
}

export function passwordHint(lang) {
  return lang === "en"
    ? "At least 8 characters. Enter the password twice to avoid mistakes."
    : "Не менее 8 символов. Введите пароль дважды, чтобы исключить ошибку.";
}

export function sortedUsersForTable(users) {
  const rolePriority = { owner: 0, admin: 1, operator: 2, viewer: 3 };
  return [...users].sort((left, right) => {
    const roleCompare = (rolePriority[left.role] ?? 99) - (rolePriority[right.role] ?? 99);
    if (roleCompare !== 0) return roleCompare;
    const leftName = String(left.display_name || left.username || "");
    const rightName = String(right.display_name || right.username || "");
    return leftName.localeCompare(rightName, "ru", { sensitivity: "base", numeric: true });
  });
}

export function settingsDraftFromApi(data) {
  return {
    system_name: data?.system_name || "KM VMS",
    timezone: data?.timezone || "UTC",
    language: normalizeLocaleImpl(data?.language),
    storage_path: data?.storage_path || "",
    archive_primary_path: data?.archive_primary_path || data?.storage_host_path || data?.storage_root || data?.storage_path || "",
    archive_host_path: data?.archive_host_path || data?.storage_host_path || "",
    storage_host_path: data?.storage_host_path || "",
    storage_root: data?.storage_root || data?.container_runtime_storage_root || "",
    storage_recordings_path: data?.storage_recordings_path || data?.container_recordings_namespace_root || "",
    storage_namespace: data?.storage_namespace || "",
    storage_change_requires: data?.storage_change_requires || "",
    auto_free_space_cleanup_enabled: Boolean(data?.auto_free_space_cleanup_enabled),
    auto_free_space_warning_threshold_percent: data?.auto_free_space_warning_threshold_percent ?? 10,
    auto_free_space_cleanup_threshold_percent: data?.auto_free_space_cleanup_threshold_percent ?? 5,
    auto_free_space_critical_threshold_percent: data?.auto_free_space_critical_threshold_percent ?? 1,
    recording_suspended_by_low_disk: Boolean(data?.recording_suspended_by_low_disk),
    recordingProfile: profileFromFormat(data?.recording_format),
    hardware_preferred_backend: data?.hardware_preferred_backend || null,
  };
}

export function payloadFromDraft(draft) {
  return {
    system_name: draft.system_name?.trim() || null,
    timezone: timezoneValueForSettings(draft.timezone),
    language: draft.language,
    recording_format: recordingFormatForProfile(draft.recordingProfile),
    hardware_preferred_backend: draft.hardware_preferred_backend || null,
    auto_free_space_cleanup_enabled: Boolean(draft.auto_free_space_cleanup_enabled),
  };
}

export function samePayload(left, right) {
  if (!left || !right) return true;
  return JSON.stringify(payloadFromDraft(left)) === JSON.stringify(payloadFromDraft(right));
}

export function formatBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes < 1024 ** 4) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  return `${(bytes / 1024 ** 4).toFixed(1)} TB`;
}

export function formatAuditTimestamp(value, lang) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat(lang === "en" ? "en-US" : "ru-RU", {
    year: "2-digit",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export const MAINTENANCE_DRY_RUN_ENDPOINTS = {
  db_adoption: { path: "/system/db-adoption/dry-run", body: {} },
  migration: { path: "/system/migrations/dry-run", body: {} },
  restore: { path: "/system/restore/dry-run", body: {} },
  update: { path: "/system/update/dry-run", body: {} },
};

export function maintenanceFlowRows(overview) {
  const flows = overview?.flows || {};
  return ["db_adoption", "migration", "restore", "update"].map((key) => ({ key, ...(flows[key] || {}) }));
}

export function maintenanceStatusText(status, t) {
  const key = status || "unknown";
  return t.maintenanceStatuses?.[key] || key;
}

export function maintenanceStatusClass(status) {
  if (["ok", "current", "available", "adopted", "already_adopted"].includes(status)) return "ok";
  if (["blocked", "no_artifacts", "not_configured"].includes(status)) return "blocked";
  if (["adoptable", "limited"].includes(status)) return "warning";
  return "neutral";
}

export function maintenanceDetailRows(flow, t) {
  const details = flow?.details || {};
  const labels = t.maintenanceLabels || {};
  const rows = [];
  if (details.pending_count !== null && details.pending_count !== undefined) rows.push([labels.pending, details.pending_count]);
  if (details.valid_artifact_count !== null && details.valid_artifact_count !== undefined) rows.push([labels.artifacts, `${details.valid_artifact_count}/${details.artifact_count || 0}`]);
  if (details.current_version !== null && details.current_version !== undefined) rows.push([labels.current, details.current_version]);
  if (details.target_version !== null && details.target_version !== undefined) rows.push([labels.target, details.target_version]);
  if (details.available_version) rows.push([labels.available, details.available_version]);
  rows.push([labels.backup, flow?.backup_required ? t.maintenanceBackupRequired : t.maintenanceBackupNotRequired]);
  if (flow?.requires_confirmation) rows.push([labels.confirm, t.maintenanceConfirmationRequired]);
  if (!flow?.can_apply) rows.push([labels.apply, t.maintenanceUnsupported]);
  return rows.slice(0, 5);
}

export function auditMessage(event, lang) {
  if (lang === "zh-CN") return translateTextImpl("zh-CN", event?.message_ru || event?.message_en || "");
  return lang === "en"
    ? event?.message_en || event?.message_ru || ""
    : event?.message_ru || event?.message_en || "";
}

export function auditLabel(kind, value, lang) {
  if (!value) return "";
  return localizedValue(AUDIT_LABELS[kind]?.[value], lang) || value;
}

export function auditTarget(event, t) {
  const parts = [event?.target_type, event?.target_name || event?.target_id].filter(Boolean);
  return parts.length ? parts.join(": ") : t.journalTargetEmpty;
}

export function safeMetadataRows(metadata) {
  if (!metadata || typeof metadata !== "object" || Array.isArray(metadata)) return [];
  return Object.entries(metadata).slice(0, 8).map(([key, value]) => {
    let rendered;
    if (value && typeof value === "object") {
      rendered = JSON.stringify(value);
    } else if (value === null || value === undefined) {
      rendered = "-";
    } else {
      rendered = String(value);
    }
    return {
      key: String(key).slice(0, 48),
      value: rendered.length > 160 ? `${rendered.slice(0, 157)}...` : rendered,
    };
  });
}

export function parseErrorDetail(message) {
  if (!message) return null;
  try {
    return JSON.parse(message);
  } catch {
    return null;
  }
}

export function humanErrorText(message, fallback) {
  if (!message) return fallback;
  const detail = parseErrorDetail(message);
  const value = detail?.detail ?? detail;
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value
      .map((item) => item?.msg || item?.message || "")
      .filter(Boolean)
      .join("; ") || fallback;
  }
  if (value && typeof value === "object") {
    if (typeof value.error === "string") return value.error;
    if (typeof value.message === "string") return value.message;
  }
  if (!message.startsWith("{")) return message;
  return fallback;
}

export function recordingFormatForProfile(profile) {
  return profile === "compatibility" ? "mp4" : "mkv";
}

export function profileFromFormat(format) {
  return format === "mp4" ? "compatibility" : "reliability";
}

export function offsetFromTimezone(timezone) {
  try {
    const value = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      timeZoneName: "shortOffset",
      hour: "2-digit",
    }).formatToParts(new Date()).find((part) => part.type === "timeZoneName")?.value || "GMT";
    const match = value.match(/^GMT([+-])(\d{1,2})(?::\d{2})?$/);
    if (!match) return 0;
    return (match[1] === "-" ? -1 : 1) * Number(match[2]);
  } catch {
    return 0;
  }
}

export function timezoneValueForSettings(timezone) {
  if (UTC_TIMEZONES.some((zone) => zone.value === timezone)) return timezone;
  return timezone || "UTC";
}

export function hardwareOptionState(backend, hardware, t) {
  if (backend === "auto" || backend === "cpu") return { selectable: true, reason: "" };
  const status = hardware?.backend_status?.[backend];
  const available = (hardware?.available_backends || []).includes(backend);
  if (available) return { selectable: true, reason: "" };
  if (status?.candidate) return { selectable: false, reason: status.reason || t.failedValidation };
  return { selectable: false, reason: t.notDetected };
}

export function userCanBeManaged(currentUser, user) {
  if (!currentUser || !user) return false;
  if (currentUser.role === "owner") return true;
  if (currentUser.role !== "admin") return false;
  return user.role !== "owner" && user.role !== "admin";
}

export function userCanBeDeleted(currentUser, user, users) {
  if (!currentUser || !user) return false;
  if (user.role === "owner") return false;
  if (user.id === currentUser.id) return false;
  if (!userCanBeManaged(currentUser, user)) return false;

  const activeCriticalUsers = users.filter((item) => (
    item.id !== user.id &&
    item.is_active &&
    (item.role === "owner" || item.role === "admin")
  ));
  return activeCriticalUsers.length > 0;
}

export function roleOptionsFor(currentUser) {
  if (currentUser?.role === "owner") return ["admin", "operator", "viewer"];
  if (currentUser?.role === "admin") return ["operator", "viewer"];
  return [];
}

