import {
  boundedContractText,
  boundedFiniteNumber,
  formatMaintenanceMessage,
  maintenanceStatusText,
} from "./settingsPageSharedHelpers.js";

export {
  formatMaintenanceMessage,
  maintenanceStatusText,
} from "./settingsPageSharedHelpers.js";

export {
  UPDATE_APPLY_POLL_INTERVAL_MS,
  UPDATE_APPLY_RUNNING_STATUSES,
  buildUpdateApplyConfirmation,
  formatDurationSeconds,
  formatUpdateNotice,
  shortCommit,
  updateApplyButtonText,
  updateApplyCandidateSnapshot,
  updateApplyEffectiveStatus,
  updateApplyErrorMessages,
  updateApplyFactRows,
  updateApplyIsRunning,
  updateApplyOperatorModel,
  updateApplyProgressText,
  updateApplyReconnectTiming,
  updateApplyRecoveryText,
  updateApplyStepRows,
  updateApplyTechnicalRows,
  updateApplyTransportPhase,
  updateApplyTrustedCandidateRelease,
} from "./settingsUpdateApplyHelpers.js";

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
export const UPDATE_APPLY_MODAL_GRACE_MS = 10000;
export const UPDATE_APPLY_PENDING_STORAGE_KEY = "km_vms_update_apply_pending_v1";
export const BACKUP_OPERATION_PENDING_STORAGE_KEY = "km_vms_backup_operation_pending_v1";
export const BACKUP_OPERATION_ADMISSION_GRACE_MS = 10000;
const UPDATE_APPLY_PENDING_SCHEMA = 1;
const UPDATE_APPLY_FULL_COMMIT_PATTERN = /^[0-9a-f]{40}$/i;
const UPDATE_APPLY_SUBMISSION_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UPDATE_APPLY_UNSAFE_DATA_FIELD = Symbol("update-apply-unsafe-data-field");
const UPDATE_APPLY_PENDING_STORAGE_LIMIT = 2048;
const BACKUP_OPERATION_PENDING_SCHEMA = 1;
const BACKUP_OPERATION_PENDING_STORAGE_LIMIT = 1024;
const BACKUP_OPERATION_PENDING_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const BACKUP_OPERATION_KINDS = new Set(["create", "check", "delete"]);
const BACKUP_ARTIFACT_ID_PATTERN = /^kmvms-db-\d{8}T\d{6}Z-[a-f0-9]{12}$/;
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
    timezone: data?.timezone || "UTC",
    language: normalizeLocaleImpl(data?.language),
    recordingProfile: profileFromFormat(data?.recording_format),
    hardware_preferred_backend: data?.hardware_preferred_backend || null,
  };
}

export function payloadFromDraft(draft) {
  return {
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

export function formatFileSize(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
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

export function maintenanceStatusClass(status) {
  if (["ok", "current", "available", "adopted", "already_adopted", "complete", "completed", "valid", "verified", "drift_known_safe", "draft_known_safe", "update_available"].includes(status)) return "ok";
  if (["blocked", "no_artifacts", "not_configured", "failed", "failed_rolled_back", "cancelled", "stalled"].includes(status)) return "blocked";
  if (["adoptable", "action_available", "attention", "limited", "unavailable", "queued", "starting_helper", "preflight", "acquire_source", "downloading", "extracting", "validating_source", "overlay", "applying", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification", "preparing", "staging", "activating", "reconnecting", "rolling_back", "checking"].includes(status)) return "warning";
  return "neutral";
}

function isPlainDataRecord(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  try {
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  } catch {
    return false;
  }
}

function ownDataField(record, key) {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(record, key);
    if (!descriptor) return undefined;
    return Object.prototype.hasOwnProperty.call(descriptor, "value")
      ? descriptor.value
      : UPDATE_APPLY_UNSAFE_DATA_FIELD;
  } catch {
    return UPDATE_APPLY_UNSAFE_DATA_FIELD;
  }
}

function exactBoundedDataText(value, maxLength) {
  if (typeof value !== "string" || value.length > maxLength) return "";
  const text = value.trim();
  return text && text.length <= maxLength ? text : "";
}

function isBoundedPlainDataRecord(value, maxFields = 16) {
  if (!isPlainDataRecord(value)) return false;
  try {
    const keys = Reflect.ownKeys(value);
    if (keys.length > maxFields || keys.some((key) => typeof key !== "string")) return false;
    return keys.every((key) => {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      return Boolean(descriptor && Object.prototype.hasOwnProperty.call(descriptor, "value"));
    });
  } catch {
    return false;
  }
}

function updateApplyRequestId(applyStatus) {
  return boundedContractText(applyStatus?.request_id || applyStatus?.operation_id, 160);
}

function updateApplyExpectedCommit(applyStatus) {
  return boundedContractText(applyStatus?.expected_commit || applyStatus?.source?.commit, 80).toLowerCase();
}

export function createUpdateApplyPending(submissionIdValue, candidate, submittedAtMs) {
  const submitted = boundedFiniteNumber(submittedAtMs, 0, 0, Number.MAX_SAFE_INTEGER);
  const version = boundedContractText(candidate?.version, 80);
  const commit = boundedContractText(candidate?.commit, 80).toLowerCase();
  const submissionId = boundedContractText(submissionIdValue, 36).toLowerCase();
  if (
    !version
    || !UPDATE_APPLY_FULL_COMMIT_PATTERN.test(commit)
    || !submitted
    || !UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)
  ) return null;
  return {
    schema: UPDATE_APPLY_PENDING_SCHEMA,
    submissionId,
    targetVersion: version,
    targetCommit: commit,
    submittedAtMs: submitted,
  };
}

export function sanitizeUpdateApplyPending(value, nowMs = Date.now()) {
  if (!value || typeof value !== "object" || Number(value.schema) !== UPDATE_APPLY_PENDING_SCHEMA) return null;
  const targetVersion = boundedContractText(value.targetVersion, 80);
  const targetCommit = boundedContractText(value.targetCommit, 80).toLowerCase();
  const submissionId = boundedContractText(value.submissionId, 36).toLowerCase();
  const submittedAtMs = boundedFiniteNumber(value.submittedAtMs, 0, 0, Number.MAX_SAFE_INTEGER);
  if (
    !targetVersion
    || !UPDATE_APPLY_FULL_COMMIT_PATTERN.test(targetCommit)
    || !UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)
    || !submittedAtMs
  ) return null;
  const now = boundedFiniteNumber(nowMs, submittedAtMs, 0, Number.MAX_SAFE_INTEGER);
  return {
    schema: UPDATE_APPLY_PENDING_SCHEMA,
    submissionId,
    targetVersion,
    targetCommit,
    submittedAtMs: Math.min(submittedAtMs, now),
  };
}

export function updateApplyPendingExactMatch(left, right, nowMs = Date.now()) {
  const first = sanitizeUpdateApplyPending(left, nowMs);
  const second = sanitizeUpdateApplyPending(right, nowMs);
  if (!first || !second) return false;
  return JSON.stringify(first) === JSON.stringify(second);
}

export function restoreUpdateApplyPending(rawValue, nowMs = Date.now()) {
  if (rawValue === null || rawValue === undefined) return null;
  if (typeof rawValue !== "string" || rawValue.length > UPDATE_APPLY_PENDING_STORAGE_LIMIT) return null;
  try {
    return sanitizeUpdateApplyPending(JSON.parse(rawValue), nowMs);
  } catch {
    return null;
  }
}

export function createBackupOperationPending(kindValue, artifactIdValue, submissionIdValue, createdAtMs) {
  const kind = boundedContractText(kindValue, 12).toLowerCase();
  const artifactId = boundedContractText(artifactIdValue, 80);
  const submissionId = boundedContractText(submissionIdValue, 36).toLowerCase();
  const created = boundedFiniteNumber(createdAtMs, 0, 0, Number.MAX_SAFE_INTEGER);
  if (
    !BACKUP_OPERATION_KINDS.has(kind)
    || !UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)
    || !created
    || (kind === "create" ? Boolean(artifactId) : !BACKUP_ARTIFACT_ID_PATTERN.test(artifactId))
  ) return null;
  return {
    schema: BACKUP_OPERATION_PENDING_SCHEMA,
    submissionId,
    kind,
    artifactId: kind === "create" ? null : artifactId,
    createdAtMs: created,
  };
}

export function sanitizeBackupOperationPending(value, nowMs = Date.now()) {
  if (!value || typeof value !== "object" || Number(value.schema) !== BACKUP_OPERATION_PENDING_SCHEMA) return null;
  const record = createBackupOperationPending(
    value.kind,
    value.artifactId,
    value.submissionId,
    value.createdAtMs,
  );
  if (!record) return null;
  const now = boundedFiniteNumber(nowMs, record.createdAtMs, 0, Number.MAX_SAFE_INTEGER);
  if (record.createdAtMs > now + 60_000 || now - record.createdAtMs > BACKUP_OPERATION_PENDING_MAX_AGE_MS) return null;
  return record;
}

export function restoreBackupOperationPending(rawValue, nowMs = Date.now()) {
  if (rawValue === null || rawValue === undefined) return null;
  if (typeof rawValue !== "string" || rawValue.length > BACKUP_OPERATION_PENDING_STORAGE_LIMIT) return null;
  try {
    return sanitizeBackupOperationPending(JSON.parse(rawValue), nowMs);
  } catch {
    return null;
  }
}

export function backupOperationWithinAdmissionGrace(
  pendingValue,
  nowMs = Date.now(),
) {
  const pending = sanitizeBackupOperationPending(pendingValue, nowMs);
  if (!pending) return false;
  return nowMs - pending.createdAtMs < BACKUP_OPERATION_ADMISSION_GRACE_MS;
}

function updateApplyAdmissionContract(applyStatus) {
  if (!isPlainDataRecord(applyStatus)) return null;
  const admission = ownDataField(applyStatus, "admission");
  if (!isBoundedPlainDataRecord(admission, 16)) return null;
  const schemaVersion = ownDataField(admission, "schema_version");
  const authority = exactBoundedDataText(ownDataField(admission, "authority"), 20).toLowerCase();
  const state = exactBoundedDataText(ownDataField(admission, "state"), 80).toLowerCase();
  const active = ownDataField(admission, "active");
  const submissionId = exactBoundedDataText(ownDataField(admission, "submission_id"), 80).toLowerCase();
  const requestId = exactBoundedDataText(ownDataField(admission, "request_id"), 160);
  const targetCommit = exactBoundedDataText(ownDataField(admission, "target_commit"), 40).toLowerCase();
  if (schemaVersion !== 3 || !["active", "inactive", "unknown"].includes(authority) || !state) return null;
  if (authority === "active" && active !== true) return null;
  if (authority === "inactive" && active !== false) return null;
  if (authority === "unknown" && active !== false) return null;
  if (submissionId && !UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)) return null;
  if (targetCommit && !UPDATE_APPLY_FULL_COMMIT_PATTERN.test(targetCommit)) return null;
  return { authority, state, submissionId, requestId, targetCommit };
}

export function reconcileUpdateApplyPending(record, applyStatus, observedAtMs = Date.now()) {
  const safeRecord = sanitizeUpdateApplyPending(record, observedAtMs);
  if (!safeRecord) return { outcome: "none", record: null };
  if (!isPlainDataRecord(applyStatus) || applyStatus.schema_version !== 1) {
    return { outcome: "pending", record: safeRecord };
  }
  const admission = updateApplyAdmissionContract(applyStatus);
  const requestId = updateApplyRequestId(applyStatus);
  const submissionId = boundedContractText(applyStatus?.submission_id, 80).toLowerCase();
  const expectedCommit = updateApplyExpectedCommit(applyStatus);
  const exact = Boolean(
    requestId
    && submissionId === safeRecord.submissionId
    && expectedCommit === safeRecord.targetCommit
    && admission?.submissionId === safeRecord.submissionId
    && admission.requestId === requestId
    && admission.targetCommit === safeRecord.targetCommit
  );
  if (exact) return { outcome: "accepted", record: safeRecord };
  if (admission?.authority === "active") {
    return { outcome: "conflict", record: safeRecord };
  }
  const observed = boundedFiniteNumber(
    observedAtMs,
    safeRecord.submittedAtMs,
    safeRecord.submittedAtMs,
    Number.MAX_SAFE_INTEGER,
  );
  if (
    admission?.authority === "inactive"
    && observed - safeRecord.submittedAtMs >= UPDATE_APPLY_MODAL_GRACE_MS
  ) {
    return { outcome: "not_accepted", record: safeRecord };
  }
  return { outcome: "pending", record: safeRecord };
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

function maintenancePresentation(flow) {
  return flow?.presentation && typeof flow.presentation === "object" ? flow.presentation : flow || {};
}

function maintenanceBooleanText(value, t) {
  if (value === true) return t.yes || "Yes";
  if (value === false) return t.no || "No";
  return value === null || value === undefined || value === "" ? "-" : String(value);
}

export function maintenanceReadinessRows(overview, t) {
  const flows = overview?.flows || {};
  const titleLabels = t.maintenanceReadinessTitles || {};
  const summaries = t.maintenanceOperatorSummaries || {};
  const actions = t.maintenanceOperatorActions || {};
  const statusLabels = t.maintenanceOperatorStatuses || {};
  const checkActions = t.maintenanceCheckActions || {};
  const factLabels = t.maintenanceFactLabels || {};

  return ["db_adoption", "migration"].map((key) => {
    const flow = { key, ...(flows[key] || {}) };
    const presentation = maintenancePresentation(flow);
    const userStatus = presentation.user_status || "attention";
    const facts = Array.isArray(presentation.facts) ? presentation.facts : [];
    const canApply = Boolean(key === "db_adoption" && presentation.can_apply && presentation.apply_supported);
    return {
      key,
      flow,
      userStatus,
      statusClass: maintenanceStatusClass(userStatus),
      statusLabel: statusLabels[userStatus] || maintenanceStatusText(userStatus, t),
      title: titleLabels[presentation.title_key] || t.maintenanceFlows?.[key] || key,
      summary: summaries[presentation.summary_key] || formatMaintenanceMessage(flow.reason, t),
      action: actions[presentation.operator_action_key] || actions.check_status || "",
      checkLabel: checkActions[key] || t.maintenanceDryRun,
      canCheck: Boolean(presentation.can_check),
      showCheck: Boolean(presentation.can_check && userStatus !== "ok" && !canApply),
      showApply: canApply,
      applyLabel: t.maintenanceDbAdoptionApply || t.maintenanceApply || "",
      supportReportAvailable: Boolean(presentation.support_report_available),
      facts: facts
        .filter((item) => item && item.value !== null && item.value !== undefined)
        .map((item) => [factLabels[item.key] || item.key, maintenanceBooleanText(item.value, t)])
        .slice(0, 3),
    };
  });
}

function backupDimensionLabel(group, status, t) {
  const labels = t[group] || {};
  const key = String(status || "unknown").trim().toLowerCase();
  return labels[key] || labels.unknown || maintenanceStatusText(key, t);
}

function backupDimensionTone(status) {
  const key = String(status || "").trim().toLowerCase();
  if (["available", "verified", "compatible", "passed", "allowed"].includes(key)) return "ok";
  if (["not_checked", "not_performed", "unknown"].includes(key)) return "neutral";
  if (["stale_evidence", "migration_required", "partial_retryable", "incomplete"].includes(key)) return "attention";
  return "problem";
}

export function maintenanceBackupCheckResultText(status, t) {
  const labels = t.maintenanceBackupCheckStatuses || {};
  const key = String(status || "").trim().toLowerCase();
  if (labels[key]) return labels[key];
  if (["valid", "verified", "available"].includes(key)) return labels.valid || maintenanceStatusText("verified", t);
  if (["no_artifacts", "empty"].includes(key)) return labels.no_artifacts || t.maintenanceBackupNothingToCheck || maintenanceStatusText("no_artifacts", t);
  if (["blocked", "invalid", "check_failed"].includes(key)) return labels[key] || maintenanceStatusText(key, t);
  return labels.fallback || t.maintenanceMessageFallback || maintenanceStatusText(status, t);
}

export function maintenanceBackupOperationResultText(result, t) {
  const kind = String(result?.kind || "check").trim().toLowerCase();
  const operationResult = result?.result && typeof result.result === "object" ? result.result : {};
  const status = String(operationResult.status || result?.status || result?.state || "").trim().toLowerCase();
  const operationLabels = t.maintenanceBackupOperationLabels || {};
  if (kind === "create") {
    const labels = t.maintenanceBackupCreateStatuses || {};
    const created = ["ok", "created", "verified", "valid", "completed", "complete", "satisfied"].includes(status);
    const failed = ["blocked", "failed", "error", "backup_failed"].includes(status);
    const label = operationLabels.create || t.maintenanceBackupCreate || "Create";
    return {
      kind,
      label,
      title: label,
      text: labels[status] || (created ? t.maintenanceBackupCreated : failed ? t.maintenanceBackupCreateFailed : labels.fallback || t.maintenanceMessageFallback || maintenanceStatusText(status, t)),
      successful: created,
      showReason: !created,
    };
  }
  if (kind === "delete") {
    const labels = t.maintenanceBackupDeleteStatuses || {};
    const deleted = ["deleted", "deleted_with_missing_files", "ok", "completed", "complete"].includes(status);
    const failed = ["blocked", "failed", "error", "delete_failed", "not_found"].includes(status);
    const label = operationLabels.delete || t.maintenanceBackupDelete || "Delete";
    return {
      kind,
      label,
      title: label,
      text: labels[status] || (deleted ? t.maintenanceBackupDeleted : failed ? t.maintenanceBackupDeleteFailed : labels.fallback || t.maintenanceMessageFallback || maintenanceStatusText(status, t)),
      successful: deleted,
      showReason: !deleted,
    };
  }
  const integrityStatus = String(operationResult.integrity_status || "").trim().toLowerCase();
  const compatibilityStatus = String(operationResult.compatibility_status || "").trim().toLowerCase();
  const validationStatus = String(operationResult.restore_validation_status || "").trim().toLowerCase();
  const outcomeLabels = t.maintenanceBackupCheckOutcomes || {};
  const label = operationLabels.check || t.maintenanceBackupCheck || "Check";
  const passedTitle = t.maintenanceBackupCheckPassedTitle || maintenanceBackupCheckResultText("valid", t);
  const hasStructuredOutcome = Boolean(integrityStatus || compatibilityStatus || validationStatus);
  const fullyValidated = integrityStatus === "verified"
    && compatibilityStatus === "compatible"
    && validationStatus === "passed";
  const statusValidated = !hasStructuredOutcome
    && ["valid", "verified", "validated", "passed"].includes(status);
  if (fullyValidated || statusValidated) {
    return {
      kind: "check",
      label,
      title: passedTitle,
      text: outcomeLabels.fully_validated
        || t.maintenanceBackupCheckPassedText
        || maintenanceBackupCheckResultText(status, t),
      successful: true,
      showReason: false,
    };
  }
  if (
    integrityStatus === "verified" &&
    compatibilityStatus === "migration_required" &&
    ["", "not_performed", "not_performed_stage5_deferred"].includes(validationStatus)
  ) {
    return {
      kind: "check",
      label,
      title: label,
      text: outcomeLabels.integrity_verified_migration_required || maintenanceBackupCheckResultText(status, t),
      successful: false,
      showReason: false,
    };
  }
  if (integrityStatus === "failed") {
    return {
      kind: "check",
      label,
      title: label,
      text: outcomeLabels.integrity_failed || maintenanceBackupCheckResultText(status, t),
      successful: false,
      showReason: true,
    };
  }
  if (validationStatus === "failed") {
    return {
      kind: "check",
      label,
      title: label,
      text: outcomeLabels.restore_failed || maintenanceBackupCheckResultText(status, t),
      successful: false,
      showReason: true,
    };
  }
  const knownResult = ["valid", "verified", "available", "validated", "passed", "completed"].includes(status);
  return {
    kind: "check",
    label,
    title: label,
    text: maintenanceBackupCheckResultText(status, t),
    successful: false,
    showReason: !knownResult,
  };
}

function maintenanceBackupProjectionModel(details, flowStatus, t, lang = "ru") {
  const source = details && typeof details === "object" ? details : {};
  const artifacts = Array.isArray(source.artifacts)
    ? source.artifacts
    : Array.isArray(source.items)
      ? source.items
      : [];
  const sortedArtifacts = [...artifacts].sort((left, right) => {
    const leftTime = new Date(left?.artifact_created_at || 0).getTime() || 0;
    const rightTime = new Date(right?.artifact_created_at || 0).getTime() || 0;
    return rightTime - leftTime;
  });
  const totalCount = Number(source.total_count ?? source.artifact_count ?? sortedArtifacts.length) || 0;
  const totalBytes = Number(source.total_bytes || 0) || 0;
  const offset = Math.max(0, Number(source.offset || 0) || 0);
  const limit = Math.max(1, Number(source.limit || sortedArtifacts.length || 20) || 20);
  const validCount = Number(source.valid_artifact_count ?? source.verified_compatible_count ?? 0) || 0;
  const latest = sortedArtifacts[0] || null;
  const productionRestoreSupported = Boolean(source.current_product_restore_supported);
  const temporaryValidationSupported = Boolean(source.temporary_validation_restore_supported);
  const copyWord = totalCount === 1 ? t.maintenanceBackupCopyOne : t.maintenanceBackupCopyMany;
  const renderedCopyWord = String(copyWord || "").includes("{count}")
    ? String(copyWord || "").replace("{count}", String(totalCount))
    : `${totalCount} ${copyWord || ""}`.trim();
  const modeledArtifacts = sortedArtifacts.map((item) => {
    const artifactId = String(item.artifact_id || "");
    const productOwnedIdentity = BACKUP_ARTIFACT_ID_PATTERN.test(artifactId);
    const availability = String(item.availability_status || "unsafe");
    const integrity = String(item.integrity_status || "not_checked");
    const compatibility = String(item.compatibility_status || "unknown");
    const validation = String(item.restore_validation_status || "not_performed");
    const deleteStatus = String(item.delete_status || "blocked");
    const restoreIneligibleReason = !productOwnedIdentity
      ? "artifact_invalid"
      : availability !== "available"
        ? "artifact_unavailable"
        : integrity !== "verified"
          ? "artifact_integrity_not_verified"
          : compatibility !== "compatible"
            ? compatibility
            : !productionRestoreSupported
              ? "restore_not_supported"
              : "";
    return {
      id: artifactId,
      createdAt: item.artifact_created_at ? formatAuditTimestamp(item.artifact_created_at, lang) : "-",
      size: formatFileSize(item.file_size),
      availability,
      availabilityLabel: backupDimensionLabel("maintenanceBackupAvailabilityStatuses", availability, t),
      availabilityTone: backupDimensionTone(availability),
      integrity,
      integrityLabel: backupDimensionLabel("maintenanceBackupIntegrityStatuses", integrity, t),
      integrityTone: backupDimensionTone(integrity),
      compatibility,
      compatibilityLabel: backupDimensionLabel("maintenanceBackupCompatibilityStatuses", compatibility, t),
      compatibilityTone: backupDimensionTone(compatibility),
      validation,
      validationLabel: backupDimensionLabel("maintenanceBackupValidationStatuses", validation, t),
      validationTone: backupDimensionTone(validation),
      deleteStatus,
      deletable: Boolean(productOwnedIdentity && item.delete_supported && ["allowed", "partial_retryable"].includes(deleteStatus)),
      canDelete: Boolean(productOwnedIdentity && item.delete_supported && ["allowed", "partial_retryable"].includes(deleteStatus)),
      canCheck: Boolean(productOwnedIdentity && temporaryValidationSupported),
      canRestore: Boolean(!restoreIneligibleReason),
      restoreIneligibleReason,
      schema: item.artifact_schema_version ?? "-",
      backend: item.db_backend || "-",
      sizeText: formatFileSize(item.file_size),
      checkedAt: item.checked_at ? formatAuditTimestamp(item.checked_at, lang) : "",
      validatedAt: item.validated_at ? formatAuditTimestamp(item.validated_at, lang) : "",
      hasProblem: [availability, integrity, compatibility, validation].some((status) => backupDimensionTone(status) === "problem"),
    };
  });
  const latestArtifact = modeledArtifacts[0] || null;
  const rootStatus = String(source.root_status || "unknown");
  const latestTones = latestArtifact
      ? [
        latestArtifact.availabilityTone,
        latestArtifact.integrityTone,
        latestArtifact.compatibilityTone,
      ]
    : [];
  const tone = rootStatus === "unsafe" || String(flowStatus || source.status || "") === "unavailable"
    ? "blocked"
    : !totalCount
      ? "warning"
      : latestTones.includes("problem")
        ? "blocked"
        : validCount < 1 || latestTones.includes("attention") || latestTones.includes("neutral")
          ? "warning"
          : "ok";
  return {
    total: totalCount,
    valid: validCount,
    problem: 0,
    totalCount,
    totalBytes,
    totalBytesText: formatFileSize(totalBytes),
    validCount,
    problemCount: 0,
    countText: totalCount ? renderedCopyWord : t.maintenanceBackupNoCopies,
    latest,
    latestArtifact,
    latestCreatedAt: latest?.artifact_created_at ? formatAuditTimestamp(latest.artifact_created_at, lang) : "-",
    latestStatus: latest
      ? backupDimensionLabel("maintenanceBackupIntegrityStatuses", latest.integrity_status || "not_checked", t)
      : t.maintenanceBackupNoCopies,
    statusText: totalCount
      ? (t.maintenanceBackupStatusReady || "").replace("{count}", String(totalCount)).replace("{copy}", copyWord)
      : t.maintenanceBackupStatusEmpty,
    canCheck: Boolean(totalCount && temporaryValidationSupported),
    canDelete: modeledArtifacts.some((item) => item.canDelete),
    rootStatus,
    flowStatus: String(flowStatus || source.status || "unknown"),
    tone,
    offset,
    limit,
    hasMore: Boolean(source.has_more),
    hasPrevious: offset > 0,
    pageStart: totalCount ? offset + 1 : 0,
    pageEnd: Math.min(totalCount, offset + modeledArtifacts.length),
    artifacts: modeledArtifacts,
  };
}

export function maintenanceBackupOverviewModel(overview, t, lang = "ru") {
  const restore = overview?.flows?.restore || {};
  return maintenanceBackupProjectionModel(
    restore?.details || {},
    restore?.status,
    t,
    lang,
  );
}

export function maintenanceBackupDetailModel(backupStatus, t, lang = "ru") {
  return maintenanceBackupProjectionModel(
    backupStatus || {},
    backupStatus?.status,
    t,
    lang,
  );
}

export function maintenanceBackupValidOffset(totalCount, limit, requestedOffset) {
  const safeTotal = Math.max(0, Number(totalCount || 0) || 0);
  const safeLimit = Math.max(1, Number(limit || 1) || 1);
  const safeRequested = Math.max(0, Number(requestedOffset || 0) || 0);
  if (!safeTotal) return 0;
  return Math.min(
    safeRequested,
    Math.floor((safeTotal - 1) / safeLimit) * safeLimit,
  );
}

export function maintenanceBackupManagerModel(overview, t, lang = "ru", backupStatus = null) {
  return backupStatus && typeof backupStatus === "object"
    ? maintenanceBackupDetailModel(backupStatus, t, lang)
    : maintenanceBackupOverviewModel(overview, t, lang);
}

export function maintenanceDatabaseOverviewModel(overview, t) {
  const rows = maintenanceReadinessRows(overview, t);
  const blocked = rows.find((row) => row.userStatus === "blocked");
  const attention = rows.find((row) => row.userStatus !== "ok");
  const tone = blocked ? "blocked" : attention ? "warning" : rows.length ? "ok" : "neutral";
  const primary = blocked || attention || rows.find((row) => row.key === "migration") || rows[0] || {};
  const facts = rows
    .flatMap((row) => row.facts || [])
    .filter(([label, value], index, all) => (
      all.findIndex(([candidate]) => candidate === label) === index
      && value !== null
      && value !== undefined
      && value !== ""
    ))
    .slice(0, 3);
  return {
    tone,
    statusLabel: primary.statusLabel || maintenanceStatusText("unknown", t),
    summary: primary.summary || t.maintenanceMessageFallback || "",
    action: primary.action || "",
    facts,
    actionableRow: blocked || attention || null,
  };
}

export function maintenanceOverallHealthModel({
  overview,
  updateOperator,
  database,
  backup,
  warnings,
  loading = false,
  loadError = false,
  t,
}) {
  if (
    loading
    || loadError
    || !overview
    || !updateOperator
    || updateOperator.status === "unknown"
    || !database
    || !backup
    || backup.rootStatus === "unknown"
    || warnings?.available !== true
    || warnings?.status !== "complete"
  ) {
    return {
      tone: "neutral",
      icon: "i",
      title: t.maintenanceOverallUnknown,
      summary: t.maintenanceOverallUnknownText,
    };
  }
  const blocked = (
    updateOperator.severity === "blocked"
    || database.tone === "blocked"
    || backup.tone === "blocked"
  );
  if (blocked) {
    return {
      tone: "blocked",
      icon: "!",
      title: t.maintenanceOverallBlocked,
      summary: t.maintenanceOverallBlockedText,
    };
  }
  const warning = (
    updateOperator.severity !== "ok"
    || database.tone !== "ok"
    || backup.tone !== "ok"
    || Number(warnings?.groups?.actionable || 0) > 0
  );
  if (warning) {
    return {
      tone: "warning",
      icon: "!",
      title: t.maintenanceOverallAttention,
      summary: backup.totalCount === 0
        ? t.maintenanceOverallNoBackupText
        : t.maintenanceOverallAttentionText,
    };
  }
  return {
    tone: "ok",
    icon: "✓",
    title: t.maintenanceOverallHealthy,
    summary: t.maintenanceOverallHealthyText,
  };
}

export function maintenanceWarningModel(overview, t) {
  const report = overview?.upgrade_report || {};
  const warnings = Array.isArray(report.warnings) ? report.warnings : [];
  const labels = t.maintenanceWarningLabels || {};
  const fallback = t.maintenanceWarningGeneric || {};
  const commonFallback = t.maintenanceWarningsFallback || {};
  const groups = report.warning_groups || {};
  return {
    available: report.available === true,
    status: String(report.status || "unknown").trim().toLowerCase() || "unknown",
    total: Number(report.warnings_count ?? report.total ?? warnings.length) || 0,
    groups: {
      actionable: Number(groups.actionable || 0),
      informational: Number(groups.informational || 0),
      support: Number(groups.support || 0),
    },
    items: warnings.slice(0, 10).map((item) => {
      const code = String(item?.code || "");
      const copy = labels[code] || fallback[item?.classification] || fallback.informational || commonFallback || {};
      return {
        code,
        classification: item?.classification || "informational",
        severity: item?.severity || "info",
        title: copy.title || commonFallback.title || maintenanceStatusText("unknown", t),
        summary: copy.summary || t.maintenanceMessageFallback,
        action: copy.action || t.maintenanceSupportReportAction,
      };
    }),
  };
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
  if (typeof message !== "string" || !message) return null;
  try {
    return JSON.parse(message);
  } catch {
    return null;
  }
}

const HUMAN_ERROR_INPUT_LIMIT = 4096;
const HUMAN_ERROR_OUTPUT_LIMIT = 240;
const HUMAN_ERROR_MAX_CONTAINER_DEPTH = 2;
const HUMAN_ERROR_MAX_ARRAY_ITEMS = 4;
const HUMAN_ERROR_ALLOWLISTED_FIELDS = ["detail", "summary", "error", "message", "msg"];
const HUMAN_ERROR_INVALID_DATA = Symbol("human-error-invalid-data");

function normalizedHumanErrorFallback(fallback) {
  return String(fallback || "")
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, HUMAN_ERROR_OUTPUT_LIMIT);
}

function safeHumanErrorCandidate(value) {
  const raw = typeof value === "string" ? value : "";
  if (!raw || raw.length > HUMAN_ERROR_OUTPUT_LIMIT) return "";
  const text = raw
    .replace(/[\u0000-\u001f\u007f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!text || text.length > HUMAN_ERROR_OUTPUT_LIMIT) return "";

  const unsafePatterns = [
    /<(?:!doctype|\?xml|\/?[a-z][^>]*)>/i,
    /\b(?:traceback|stack\s+trace|exception\s+in\s+thread)\b/i,
    /\b(?:typeerror|referenceerror|syntaxerror|internalservererror)\s*:/i,
    /\b(?:502\s+bad\s+gateway|503\s+service\s+unavailable|504\s+gateway\s+timeout)\b/i,
    /\bfile\s+"[^"]+"\s*,\s*line\s+\d+/i,
    /\bat\s+[^\s()]+\s*\([^)]*:\d+(?::\d+)?\)/i,
    /\bbearer\s+[a-z0-9._~+\/-]{6,}/i,
    /["']?(?:authorization|password|passwd|access[_ -]?token|refresh[_ -]?token|api[_ -]?key|cookie|secret)["']?\s*[:=]\s*\S+/i,
    /\b[a-z]:\\/i,
    /\\\\[a-z0-9_.-]+\\/i,
    /\bfile:\/\//i,
    /(?:^|\s)\/(?:volume\d+|app|var|etc|proc|sys|root|home|tmp|usr|opt|run|storage|data)(?:\/|\b)/i,
    /\bhttps?:\/\//i,
    /\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b/,
    /\[[0-9a-f:]+\]:\d{1,5}\b/i,
    /\b(?:api|web|nginx|postgres|redis|recorder|update-helper|setup-helper)(?:[-_.][a-z0-9-]+)*:\d{1,5}\b/i,
    /^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+$/i,
  ];
  return unsafePatterns.some((pattern) => pattern.test(text)) ? "" : text;
}

function humanErrorArrayValues(value) {
  try {
    const lengthDescriptor = Object.getOwnPropertyDescriptor(value, "length");
    if (!lengthDescriptor || !Object.prototype.hasOwnProperty.call(lengthDescriptor, "value")) return HUMAN_ERROR_INVALID_DATA;
    const length = lengthDescriptor.value;
    if (!Number.isSafeInteger(length) || length < 0) return HUMAN_ERROR_INVALID_DATA;
    const items = [];
    for (let index = 0; index < Math.min(length, HUMAN_ERROR_MAX_ARRAY_ITEMS); index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, String(index));
      if (!descriptor) continue;
      if (!Object.prototype.hasOwnProperty.call(descriptor, "value")) return HUMAN_ERROR_INVALID_DATA;
      items.push(descriptor.value);
    }
    return items;
  } catch {
    return HUMAN_ERROR_INVALID_DATA;
  }
}

function extractHumanErrorData(value, containerDepth = 0) {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    if (containerDepth > HUMAN_ERROR_MAX_CONTAINER_DEPTH) return HUMAN_ERROR_INVALID_DATA;
    const items = humanErrorArrayValues(value);
    if (items === HUMAN_ERROR_INVALID_DATA) return HUMAN_ERROR_INVALID_DATA;
    const messages = [];
    for (const item of items) {
      const candidate = extractHumanErrorData(item, containerDepth + 1);
      if (candidate === HUMAN_ERROR_INVALID_DATA) return HUMAN_ERROR_INVALID_DATA;
      if (candidate) messages.push(candidate);
    }
    return messages.join("; ");
  }
  if (!isPlainDataRecord(value) || containerDepth > HUMAN_ERROR_MAX_CONTAINER_DEPTH || !isBoundedPlainDataRecord(value)) {
    return HUMAN_ERROR_INVALID_DATA;
  }

  for (const key of HUMAN_ERROR_ALLOWLISTED_FIELDS) {
    const fieldValue = ownDataField(value, key);
    if (fieldValue === UPDATE_APPLY_UNSAFE_DATA_FIELD) return HUMAN_ERROR_INVALID_DATA;
    if (fieldValue === undefined || fieldValue === null || fieldValue === "") continue;
    const candidate = extractHumanErrorData(fieldValue, containerDepth + 1);
    if (candidate === HUMAN_ERROR_INVALID_DATA) return HUMAN_ERROR_INVALID_DATA;
    if (candidate) return candidate;
  }
  return "";
}

function boundedHumanErrorRetrySeconds(value) {
  if (typeof value === "number") {
    return Number.isFinite(value) && value >= 0 && value <= 3600 ? Math.floor(value) : null;
  }
  if (typeof value !== "string" || value.length > 16 || !/^\d+(?:\.\d+)?$/.test(value.trim())) return null;
  const parsed = Number(value.trim());
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 3600 ? Math.floor(parsed) : null;
}

function humanErrorRetryText(value, safeFallback) {
  if (!isBoundedPlainDataRecord(value)) return "";
  const retryValue = ownDataField(value, "retry_after_seconds");
  if (retryValue === UPDATE_APPLY_UNSAFE_DATA_FIELD) return "";
  const retryAfterSeconds = boundedHumanErrorRetrySeconds(retryValue);
  if (retryAfterSeconds === null) return "";
  return safeHumanErrorCandidate(`${safeFallback} (${retryAfterSeconds}s)`);
}

export function humanErrorText(message, fallback) {
  const safeFallback = normalizedHumanErrorFallback(fallback);
  let value = message;
  if (typeof message === "string") {
    if (!message || message.length > HUMAN_ERROR_INPUT_LIMIT) return safeFallback;
    const parsed = parseErrorDetail(message);
    value = parsed ?? message;
  } else if (!Array.isArray(message) && !isPlainDataRecord(message)) {
    return safeFallback;
  }

  const candidate = extractHumanErrorData(value);
  if (candidate === HUMAN_ERROR_INVALID_DATA) return safeFallback;
  if (candidate) return safeHumanErrorCandidate(candidate) || safeFallback;
  const retryText = humanErrorRetryText(value, safeFallback);
  if (retryText) return retryText;
  return safeFallback;
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
