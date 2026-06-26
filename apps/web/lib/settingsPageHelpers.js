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
  const label = offset === 0 ? "GMT+00:00" : `GMT${sign}${String(Math.abs(offset)).padStart(2, "0")}:00`;
  const value = offset === 0 ? "UTC" : `Etc/GMT${offset > 0 ? "-" : "+"}${Math.abs(offset)}`;
  return { offset, label, value };
});

export const HARDWARE_OPTIONS = ["auto", "qsv", "amf", "nvenc", "cpu", "vaapi"];
export const AUDIT_CATEGORIES = ["auth", "users", "settings", "cameras", "live", "records", "chronology", "security", "diagnostics", "system", "recorder", "storage", "retention", "reconciliation"];
export const AUDIT_SEVERITIES = ["info", "warning", "error", "security"];
export const AUDIT_LIMIT = 50;
export const UPDATE_APPLY_RUNNING_STATUSES = ["queued", "starting_helper", "preflight", "downloading", "extracting", "validating_source", "applying", "compose_config", "rebuilding", "restarting", "health_check"];
const AUDIT_LABELS = {
  category: {
    auth: { ru: "Авторизация", en: "Auth", "zh-CN": "授权" },
    users: { ru: "Пользователи", en: "Users", "zh-CN": "用户" },
    settings: { ru: "Настройки", en: "Settings", "zh-CN": "设置" },
    cameras: { ru: "Камеры", en: "Cameras", "zh-CN": "摄像机" },
    live: { ru: "Онлайн", en: "Live", "zh-CN": "实时" },
    records: { ru: "Записи", en: "Records", "zh-CN": "录像" },
    chronology: { ru: "Хронология", en: "Chronology", "zh-CN": "时间轴" },
    security: { ru: "Безопасность", en: "Security", "zh-CN": "安全" },
    diagnostics: { ru: "Диагностика", en: "Diagnostics", "zh-CN": "诊断" },
    system: { ru: "Система", en: "System", "zh-CN": "系统" },
    recorder: { ru: "Запись", en: "Recorder", "zh-CN": "录像服务" },
    storage: { ru: "Хранилище", en: "Storage", "zh-CN": "存储" },
    retention: { ru: "Хранение", en: "Retention", "zh-CN": "保留" },
    reconciliation: { ru: "Целостность архива", en: "Reconciliation", "zh-CN": "一致性检查" },
  },
  severity: {
    info: { ru: "Инфо", en: "Info", "zh-CN": "信息" },
    warning: { ru: "Предупреждение", en: "Warning", "zh-CN": "告警" },
    error: { ru: "Ошибка", en: "Error", "zh-CN": "错误" },
    security: { ru: "Безопасность", en: "Security", "zh-CN": "安全" },
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
};

export function maintenanceFlowRows(overview) {
  const flows = overview?.flows || {};
  return ["db_adoption", "migration", "restore"].map((key) => ({ key, ...(flows[key] || {}) }));
}

export function maintenanceStatusText(status, t) {
  const key = status || "unknown";
  const labels = t.maintenanceStatuses || {};
  return labels[key] || labels.unknown || t.maintenanceStatusUnknown || "Unknown";
}

export function maintenanceStatusClass(status) {
  if (["ok", "current", "available", "adopted", "already_adopted", "complete", "completed", "drift_known_safe", "draft_known_safe", "update_available"].includes(status)) return "ok";
  if (["blocked", "no_artifacts", "not_configured", "failed", "cancelled"].includes(status)) return "blocked";
  if (["adoptable", "limited", "queued", "starting_helper", "preflight", "downloading", "extracting", "validating_source", "applying", "compose_config", "rebuilding", "restarting", "health_check", "reconnecting", "checking"].includes(status)) return "warning";
  return "neutral";
}

export function updateApplyIsRunning(status) {
  return UPDATE_APPLY_RUNNING_STATUSES.includes(status || "");
}

export function updateApplyEffectiveStatus(updateStatus, applyStatus, transientError = "") {
  const status = applyStatus?.status || updateStatus?.status || "unknown";
  if (transientError && updateApplyIsRunning(status)) return "reconnecting";
  if (status === "completed" && applyStatus?.expected_commit && applyStatus?.commit_verified === false) return "failed";
  return status;
}

export function shortCommit(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.length > 12 ? `${text.slice(0, 12)}...` : text;
}

export function updateApplyFactRows(updateStatus, applyStatus, t) {
  const installedRelease = updateStatus?.installed_release || {};
  const availableRelease = updateStatus?.available_release || {};
  const latest = updateStatus?.latest || updateStatus?.latest_release || {};
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  const labels = t.maintenanceLabels || {};
  const comparison = updateStatus?.comparison || {};
  const status = comparison.status || updateStatus?.status || "unknown";
  const title = availableRelease.title || latest.title || installedRelease.title || "-";
  const summary = availableRelease.summary || latest.release_notes_summary || installedRelease.summary || "-";
  const verification = applyStatus?.expected_commit
    ? (applyStatus.commit_verified ? t.updateCommitVerified : t.updateCommitPending)
    : t.updateCommitUnavailable;
  return [
    [labels.current, installedRelease.version || installed.app_version || updateStatus?.installed?.installed_version || "-"],
    [labels.available, availableRelease.version || latest.version || latest.latest_version || "-"],
    [labels.releaseTitle || "Release", title],
    [labels.releaseSummary || "Summary", summary],
    [labels.status || "Status", maintenanceStatusText(status, t)],
    [labels.verification, verification],
  ];
}

export function updateApplyTechnicalRows(updateStatus, applyStatus, t) {
  const installedRelease = updateStatus?.installed_release || {};
  const availableRelease = updateStatus?.available_release || {};
  const evidence = updateStatus?.evidence || {};
  const latest = updateStatus?.latest || updateStatus?.latest_release || {};
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  const labels = t.maintenanceLabels || {};
  const targetCommit = latest.commit || latest.build_id || applyStatus?.expected_commit || applyStatus?.source?.commit || "";
  const installedCommit = applyStatus?.installed_commit || installed.git_commit || installed.installed_commit || "";
  const sourceRef = availableRelease.tag || latest.git_ref || latest.source_ref || applyStatus?.source?.apply_ref || applyStatus?.source?.ref || updateStatus?.source_channel?.source_channel_id || "";
  return [
    [labels.source, sourceRef || "-"],
    [labels.installedCommit, installedRelease.commit_short || shortCommit(installedCommit) || "-"],
    [labels.targetCommit, availableRelease.commit_short || shortCommit(targetCommit) || "-"],
    [labels.gitHead || "Git HEAD", evidence.git_head_short || shortCommit(evidence.git_head) || "-"],
    [labels.metadataSource || "Metadata", installedRelease.metadata_source || "-"],
    [labels.provider || "Provider", availableRelease.provider || updateStatus?.source_channel?.trusted_source_type || "-"],
  ].filter(([, value]) => value !== "-");
}

export function updateApplyRecoveryText(status, applyStatus, t) {
  const effective = status || "unknown";
  if (effective === "reconnecting") return t.updateApplyRecoveryReconnecting;
  if (effective === "completed" && applyStatus?.commit_verified) return t.updateApplyRecoveryCompleted;
  if (effective === "completed" && applyStatus?.expected_commit && applyStatus?.commit_verified === false) return t.updateApplyRecoveryCommitMismatch;
  if (effective === "failed") return t.updateApplyRecoveryFailed;
  if (effective === "blocked" || effective === "not_configured") return t.updateApplyRecoveryBlocked;
  if (updateApplyIsRunning(effective)) return t.updateApplyRecoveryRunning;
  if (effective === "current") return t.updateApplyRecoveryCurrent;
  if (effective === "update_available") return t.updateApplyRecoveryAvailable;
  if (effective === "identity_incomplete" || effective === "installed_identity_drift" || effective === "metadata_stale") return t.updateApplyRecoveryIdentity;
  if (effective === "provider_unavailable" || effective === "no_release_published") return t.updateApplyRecoveryProvider;
  if (effective === "installed_newer_than_available") return t.updateApplyRecoveryInstalledNewer;
  return t.updateApplyRecoveryUnknown;
}

function normalizeUpdateNoticeCode(item) {
  const raw = String(item?.code || item?.category || item?.reason || item?.status || item?.phase || "").trim().toLowerCase();
  if (raw) return raw;
  const message = String(item?.message || item?.error_message || "").trim().toLowerCase();
  if (!message) return "";
  if (message.includes("installed source metadata is unavailable or invalid")) return "source_metadata_invalid";
  if (message.includes("last update metadata is unavailable or invalid")) return "update_metadata_invalid";
  if (message.includes("source metadata schema is unsupported")) return "source_metadata_unsupported_schema";
  if (message.includes("update metadata schema is unsupported")) return "update_metadata_unsupported_schema";
  if (message.includes("installed commit value is not a valid")) return "installed_commit_invalid";
  if (message.includes("trusted manifest") && message.includes("not configured")) return "trusted_manifest_not_configured";
  if (message.includes("commit does not match")) return "commit_mismatch";
  if (message.includes("token") && (message.includes("missing") || message.includes("configured"))) return "token_not_configured";
  if (message.includes("migration")) return "requires_migration";
  if (message.includes("backup")) return "requires_backup";
  if (message.includes("manual")) return "requires_manual_action";
  return "";
}

export function formatUpdateNotice(item, t, lang = "ru") {
  const code = normalizeUpdateNoticeCode(item);
  const labels = t.updateWarningLabels || {};
  if (code && labels[code]) return labels[code];
  if (code.startsWith("source_metadata_") && labels.source_metadata_invalid) return labels.source_metadata_invalid;
  if (code.startsWith("update_metadata_") && labels.update_metadata_invalid) return labels.update_metadata_invalid;
  if ((code === "requires_migration" || code === "release_requires_migration" || code === "migration_required") && labels.requires_migration) return labels.requires_migration;
  if ((code === "requires_backup" || code === "release_requires_backup" || code === "backup_required") && labels.requires_backup) return labels.requires_backup;
  if ((code === "requires_manual_action" || code === "manual_action_required") && labels.requires_manual_action) return labels.requires_manual_action;
  if ((code === "trusted_manifest_not_configured" || code === "manifest_not_configured" || code === "not_configured") && labels.trusted_manifest_not_configured) return labels.trusted_manifest_not_configured;
  if ((code === "private_token_missing" || code === "token_not_configured") && labels.token_not_configured) return labels.token_not_configured;
  const raw = String(item?.message || item?.error_message || item?.code || "").trim();
  if (lang === "en" && raw && !/stack|trace|authorization|bearer|token|secret|\.env|rtsp:|onvif/i.test(raw) && raw.length <= 140) {
    return labels[code] || raw;
  }
  return t.updateWarningGeneric || "Update warning is present.";
}

function normalizeMaintenanceBackendText(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  const lower = raw.toLowerCase();
  if (lower === "schema metadata is already valid.") return "schema_metadata_valid";
  if (lower === "schema metadata is already valid") return "schema_metadata_valid";
  if (lower === "schema is current; no pending migrations.") return "schema_current_no_pending_migrations";
  if (lower === "schema is current; no pending migrations") return "schema_current_no_pending_migrations";
  if (lower === "no valid restore artifacts are available in configured backup root.") return "restore_no_valid_artifacts";
  if (lower === "no valid restore artifacts are available in the configured backup root.") return "restore_no_valid_artifacts";
  if (lower.includes("no durable maintenance action history is available")) return "maintenance_history_limited";
  if (/^[a-z0-9_:-]+$/.test(lower)) return lower.replaceAll(":", "_").replaceAll("-", "_");
  return lower;
}

export function formatMaintenanceMessage(value, t, lang = "ru", context = "status") {
  const key = normalizeMaintenanceBackendText(value);
  const labels = t.maintenanceMessageLabels || {};
  if (key && labels[key]) return labels[key];
  if (key && t.maintenanceStatuses?.[key]) return t.maintenanceStatuses[key];
  if (context === "action" || context === "blocker" || context === "error") {
    return t.maintenanceActionFallback || t.maintenanceMessageFallback || maintenanceStatusText("unknown", t);
  }
  return t.maintenanceMessageFallback || maintenanceStatusText("unknown", t);
}

export function buildUpdateApplyConfirmation(t, updateStatus) {
  const latest = updateStatus?.latest || updateStatus?.latest_release || {};
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  const lines = [t.updateApplyConfirm];
  if (installed.app_version || updateStatus?.installed?.installed_version) lines.push(`${t.updateCurrent}: ${installed.app_version || updateStatus?.installed?.installed_version}`);
  if (latest.version || latest.latest_version) lines.push(`${t.updateLatest}: ${latest.version || latest.latest_version}`);
  if (latest.commit || latest.build_id) lines.push(`${t.maintenanceLabels?.targetCommit}: ${shortCommit(latest.commit || latest.build_id)}`);
  lines.push(t.updateApplyConfirmRestart);
  return lines.filter(Boolean).join("\n");
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
