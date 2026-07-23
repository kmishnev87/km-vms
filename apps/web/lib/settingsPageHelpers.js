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
export const UPDATE_APPLY_RUNNING_STATUSES = ["queued", "starting_helper", "preflight", "acquire_source", "downloading", "extracting", "validating_source", "overlay", "applying", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification"];
export const UPDATE_APPLY_POLL_INTERVAL_MS = 5000;
export const UPDATE_APPLY_MODAL_GRACE_MS = 10000;
export const UPDATE_APPLY_ABSENCE_SETTLE_MS = 360000;
export const UPDATE_APPLY_RECONCILIATION_STORAGE_KEY = "km_vms_update_apply_reconciliation_v1";
const UPDATE_APPLY_STALE_DEFAULT_SECONDS = 180;
const UPDATE_APPLY_RECONCILIATION_SCHEMA = 3;
const UPDATE_APPLY_RECONCILIATION_STATES = new Set([
  "unresolved",
  "conflict",
  "recheck_required",
  "reconciliation_corrupt",
  "legacy_uncorrelated",
]);
const UPDATE_APPLY_PRESERVED_TERMINAL_STATUSES = new Set([
  "failed",
  "blocked",
  "stalled",
  "cancelled",
  "canceled",
]);
const UPDATE_APPLY_AUTHORITATIVE_TERMINAL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "canceled",
]);
const UPDATE_APPLY_CANCELLATION_ERROR_PATTERN = /^(?:cancelled|canceled)(?:_|$)/;
const UPDATE_APPLY_FULL_COMMIT_PATTERN = /^[0-9a-f]{40}$/i;
const UPDATE_APPLY_SUBMISSION_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const UPDATE_APPLY_SUBMISSION_PROOF_PATTERN = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/;
const UPDATE_APPLY_SUBMISSION_PROOF_LIMIT = 2048;
const UPDATE_APPLY_SYNTHETIC_PHASE_PATTERN = /^status_/;
const UPDATE_APPLY_UNSAFE_DATA_FIELD = Symbol("update-apply-unsafe-data-field");
// Persisted reconciliation is bounded before parsing; generated safe records are much smaller.
const UPDATE_APPLY_RECONCILIATION_STORAGE_LIMIT = 8192;
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

export function maintenanceStatusText(status, t) {
  const key = status || "unknown";
  const labels = t.maintenanceStatuses || {};
  return labels[key] || labels.unknown || t.maintenanceStatusUnknown || "Unknown";
}

export function maintenanceStatusClass(status) {
  if (["ok", "current", "available", "adopted", "already_adopted", "complete", "completed", "valid", "verified", "drift_known_safe", "draft_known_safe", "update_available"].includes(status)) return "ok";
  if (["blocked", "no_artifacts", "not_configured", "failed", "cancelled", "stalled"].includes(status)) return "blocked";
  if (["adoptable", "action_available", "attention", "limited", "unavailable", "queued", "starting_helper", "preflight", "acquire_source", "downloading", "extracting", "validating_source", "overlay", "applying", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification", "reconnecting", "checking"].includes(status)) return "warning";
  return "neutral";
}

export function updateApplyIsRunning(status) {
  return UPDATE_APPLY_RUNNING_STATUSES.includes(status || "");
}

function boundedFiniteNumber(value, fallback, min, max) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

function boundedContractText(value, maxLength = 160) {
  return String(value || "").trim().slice(0, maxLength);
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

function dataFieldIsAbsent(value, maxLength) {
  if (value === undefined || value === null) return true;
  return typeof value === "string" && value.length <= maxLength && value.trim() === "";
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

function updateApplyCancellationErrorIsCompatible(errorValue) {
  if (errorValue === undefined || errorValue === null) return true;
  if (!isBoundedPlainDataRecord(errorValue)) return false;
  const categoryValue = ownDataField(errorValue, "category");
  const category = exactBoundedDataText(categoryValue, 80).toLowerCase();
  return Boolean(category && !UPDATE_APPLY_SYNTHETIC_PHASE_PATTERN.test(category) && UPDATE_APPLY_CANCELLATION_ERROR_PATTERN.test(category));
}

export function updateApplyIsAuthoritativeInactiveSnapshot(applyStatus) {
  if (!isPlainDataRecord(applyStatus)) return false;

  const schemaVersion = ownDataField(applyStatus, "schema_version");
  const statusValue = ownDataField(applyStatus, "status");
  const effectiveStatusValue = ownDataField(applyStatus, "effective_status");
  const phaseValue = ownDataField(applyStatus, "phase");
  const currentStepValue = ownDataField(applyStatus, "current_step");
  const requestIdValue = ownDataField(applyStatus, "request_id");
  const expectedCommitValue = ownDataField(applyStatus, "expected_commit");
  const installedCommitValue = ownDataField(applyStatus, "installed_commit");
  const commitVerified = ownDataField(applyStatus, "commit_verified");
  const errorValue = ownDataField(applyStatus, "error");
  const isStale = ownDataField(applyStatus, "is_stale");
  const fields = [
    schemaVersion,
    statusValue,
    effectiveStatusValue,
    phaseValue,
    currentStepValue,
    requestIdValue,
    expectedCommitValue,
    installedCommitValue,
    commitVerified,
    errorValue,
    isStale,
  ];
  if (fields.includes(UPDATE_APPLY_UNSAFE_DATA_FIELD)) return false;
  if (schemaVersion !== 1 || isStale === true || (isStale !== undefined && typeof isStale !== "boolean")) return false;

  const status = exactBoundedDataText(statusValue, 80).toLowerCase();
  const effectiveStatus = exactBoundedDataText(effectiveStatusValue, 80).toLowerCase();
  const phase = exactBoundedDataText(phaseValue, 80).toLowerCase();
  const currentStep = currentStepValue === undefined || currentStepValue === null
    ? ""
    : exactBoundedDataText(currentStepValue, 80).toLowerCase();
  if (!status || status !== effectiveStatus || !phase) return false;
  if ((currentStepValue !== undefined && currentStepValue !== null && !currentStep) ||
      [phase, currentStep].some((value) => value === "unknown" || UPDATE_APPLY_SYNTHETIC_PHASE_PATTERN.test(value))) {
    return false;
  }

  if (status === "idle") {
    return phase === "idle" &&
      (!currentStep || currentStep === "idle") &&
      dataFieldIsAbsent(requestIdValue, 160) &&
      dataFieldIsAbsent(expectedCommitValue, 80) &&
      dataFieldIsAbsent(installedCommitValue, 80) &&
      (errorValue === undefined || errorValue === null) &&
      commitVerified !== true &&
      (commitVerified === undefined || typeof commitVerified === "boolean");
  }

  if (!UPDATE_APPLY_AUTHORITATIVE_TERMINAL_STATUSES.has(status)) return false;
  const requestId = exactBoundedDataText(requestIdValue, 160);
  const expectedCommit = exactBoundedDataText(expectedCommitValue, 40).toLowerCase();
  if (!requestId || !UPDATE_APPLY_FULL_COMMIT_PATTERN.test(expectedCommit)) return false;
  if (commitVerified !== undefined && typeof commitVerified !== "boolean") return false;

  if (status === "completed") {
    const installedCommit = exactBoundedDataText(installedCommitValue, 40).toLowerCase();
    return phase === "completed" &&
      commitVerified === true &&
      UPDATE_APPLY_FULL_COMMIT_PATTERN.test(installedCommit) &&
      installedCommit === expectedCommit &&
      (errorValue === undefined || errorValue === null);
  }

  if (status === "failed") {
    if (["idle", "completed", "cancelled", "canceled"].includes(phase)) return false;
    if (!isBoundedPlainDataRecord(errorValue)) return false;
    const category = exactBoundedDataText(ownDataField(errorValue, "category"), 80).toLowerCase();
    return Boolean(category && !UPDATE_APPLY_SYNTHETIC_PHASE_PATTERN.test(category) && commitVerified !== true);
  }

  return ["cancelled", "canceled"].includes(phase) &&
    commitVerified !== true &&
    updateApplyCancellationErrorIsCompatible(errorValue);
}

function updateApplyRequestId(applyStatus) {
  return boundedContractText(applyStatus?.request_id || applyStatus?.operation_id, 160);
}

function updateApplyExpectedCommit(applyStatus) {
  return boundedContractText(applyStatus?.expected_commit || applyStatus?.source?.commit, 80).toLowerCase();
}

export function updateApplyReconnectTiming(applyStatus, receivedAtMs) {
  const received = boundedFiniteNumber(receivedAtMs, 0, 0, Number.MAX_SAFE_INTEGER);
  const staleAfterSeconds = boundedFiniteNumber(
    applyStatus?.stale_after_seconds,
    UPDATE_APPLY_STALE_DEFAULT_SECONDS,
    1,
    3600,
  );
  const lastProgressAgeSeconds = boundedFiniteNumber(
    applyStatus?.last_progress_age_seconds,
    0,
    0,
    staleAfterSeconds,
  );
  const allowanceMs = UPDATE_APPLY_POLL_INTERVAL_MS * 2;
  const remainingMs = Math.max(0, staleAfterSeconds - lastProgressAgeSeconds) * 1000;
  const hardDeadlineMs = received + (staleAfterSeconds * 1000) + allowanceMs;
  return {
    receivedAtMs: received,
    staleAfterSeconds,
    lastProgressAgeSeconds,
    deadlineMs: Math.min(received + remainingMs + allowanceMs, hardDeadlineMs),
    hardDeadlineMs,
  };
}

export function updateApplyTransportPhase(applyStatus, transportError, timing, nowMs) {
  if (!transportError) return "connected";
  if (!updateApplyIsRunning(applyStatus?.status || applyStatus?.effective_status || "")) return "unknown";
  const now = boundedFiniteNumber(nowMs, Number.MAX_SAFE_INTEGER, 0, Number.MAX_SAFE_INTEGER);
  const deadline = boundedFiniteNumber(timing?.deadlineMs, -1, 0, Number.MAX_SAFE_INTEGER);
  return deadline >= 0 && now <= deadline ? "reconnecting" : "unknown";
}

export function updateApplyCandidateSnapshot(updateStatus) {
  const trustedCandidate = updateApplyTrustedCandidateRelease(updateStatus);
  const latest = trustedCandidate.version && (trustedCandidate.commit || trustedCandidate.commit_sha)
    ? trustedCandidate
    : updateStatus?.latest || updateStatus?.latest_release || {};
  return Object.freeze({
    version: boundedContractText(latest.version || latest.latest_version, 80),
    commit: boundedContractText(latest.commit || latest.commit_sha || latest.build_id, 80).toLowerCase(),
    title: boundedContractText(latest.title, 240),
  });
}

export function createUpdateApplyReconciliation(ticket, candidate, preSubmitStatus, submittedAtMs) {
  const submitted = boundedFiniteNumber(submittedAtMs, 0, 0, Number.MAX_SAFE_INTEGER);
  const version = boundedContractText(candidate?.version, 80);
  const commit = boundedContractText(candidate?.commit, 80).toLowerCase();
  const submissionId = boundedContractText(ticket?.submission_id, 80).toLowerCase();
  const rawProof = typeof ticket?.submission_proof === "string" ? ticket.submission_proof : "";
  const proof = boundedContractText(rawProof, UPDATE_APPLY_SUBMISSION_PROOF_LIMIT);
  const proofExpiresAt = boundedContractText(ticket?.expires_at, 80);
  const ticketVersion = boundedContractText(ticket?.target_version, 80);
  const ticketCommit = boundedContractText(ticket?.target_commit, 40).toLowerCase();
  if (
    !version
    || !UPDATE_APPLY_FULL_COMMIT_PATTERN.test(commit)
    || !submitted
    || !UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)
    || rawProof.length > UPDATE_APPLY_SUBMISSION_PROOF_LIMIT
    || !UPDATE_APPLY_SUBMISSION_PROOF_PATTERN.test(proof)
    || !proofExpiresAt
    || Number.isNaN(Date.parse(proofExpiresAt))
    || ticketVersion !== version
    || ticketCommit !== commit
  ) return null;
  return {
    schema: UPDATE_APPLY_RECONCILIATION_SCHEMA,
    state: "unresolved",
    submissionId,
    submissionProof: proof,
    proofExpiresAt,
    targetVersion: version,
    targetCommit: commit,
    preRequestId: updateApplyRequestId(preSubmitStatus),
    preStatus: boundedContractText(preSubmitStatus?.status || preSubmitStatus?.effective_status, 80),
    preExpectedCommit: updateApplyExpectedCommit(preSubmitStatus),
    preUpdatedAt: boundedContractText(preSubmitStatus?.updated_at, 80),
    submittedAtMs: submitted,
    modalDeadlineAtMs: submitted + UPDATE_APPLY_MODAL_GRACE_MS,
    settleWindowMs: UPDATE_APPLY_ABSENCE_SETTLE_MS,
    successfulUnchangedReads: 0,
    firstUnchangedAtMs: null,
    lastUnchangedAtMs: null,
  };
}

export function createCorruptUpdateApplyReconciliation(createdAtMs = Date.now()) {
  const created = boundedFiniteNumber(createdAtMs, Date.now(), 1, Number.MAX_SAFE_INTEGER);
  return {
    schema: UPDATE_APPLY_RECONCILIATION_SCHEMA,
    state: "reconciliation_corrupt",
    targetVersion: "",
    targetCommit: "",
    submissionProof: "",
    proofExpiresAt: "",
    preRequestId: "",
    preStatus: "",
    preExpectedCommit: "",
    preUpdatedAt: "",
    submittedAtMs: created,
    modalDeadlineAtMs: created + UPDATE_APPLY_MODAL_GRACE_MS,
    settleWindowMs: UPDATE_APPLY_ABSENCE_SETTLE_MS,
    successfulUnchangedReads: 0,
    firstUnchangedAtMs: null,
    lastUnchangedAtMs: null,
    conflictRequestId: "",
    conflictSubmissionId: "",
    conflictCommit: "",
    conflictUpdatedAt: "",
  };
}

export function sanitizeUpdateApplyReconciliation(value, nowMs = Date.now()) {
  if (!value || typeof value !== "object" || Number(value.schema) !== UPDATE_APPLY_RECONCILIATION_SCHEMA) return null;
  const state = boundedContractText(value.state, 48);
  if (!UPDATE_APPLY_RECONCILIATION_STATES.has(state)) return null;
  const targetVersion = boundedContractText(value.targetVersion, 80);
  const targetCommit = boundedContractText(value.targetCommit, 80).toLowerCase();
  const submissionId = boundedContractText(value.submissionId, 80).toLowerCase();
  const rawSubmissionProof = typeof value.submissionProof === "string" ? value.submissionProof : "";
  const submissionProof = boundedContractText(rawSubmissionProof, UPDATE_APPLY_SUBMISSION_PROOF_LIMIT);
  const proofExpiresAt = boundedContractText(value.proofExpiresAt, 80);
  const submittedAtMs = boundedFiniteNumber(value.submittedAtMs, 0, 0, Number.MAX_SAFE_INTEGER);
  const requiresCorrelation = state === "unresolved" || state === "conflict" || state === "recheck_required";
  if (
    (requiresCorrelation && (
      !targetVersion
      || !UPDATE_APPLY_FULL_COMMIT_PATTERN.test(targetCommit)
      || !UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)
      || rawSubmissionProof.length > UPDATE_APPLY_SUBMISSION_PROOF_LIMIT
      || !UPDATE_APPLY_SUBMISSION_PROOF_PATTERN.test(submissionProof)
      || !proofExpiresAt
      || Number.isNaN(Date.parse(proofExpiresAt))
    ))
    || !submittedAtMs
  ) return null;
  const now = boundedFiniteNumber(nowMs, submittedAtMs, 0, Number.MAX_SAFE_INTEGER);
  const safeSubmittedAtMs = Math.min(submittedAtMs, now);
  const successfulUnchangedReads = Math.floor(boundedFiniteNumber(value.successfulUnchangedReads, 0, 0, 1000));
  const firstUnchangedAtMs = value.firstUnchangedAtMs === null || value.firstUnchangedAtMs === undefined
    ? null
    : boundedFiniteNumber(value.firstUnchangedAtMs, null, safeSubmittedAtMs, now);
  const lastUnchangedAtMs = value.lastUnchangedAtMs === null || value.lastUnchangedAtMs === undefined
    ? null
    : boundedFiniteNumber(value.lastUnchangedAtMs, null, safeSubmittedAtMs, now);
  return {
    schema: UPDATE_APPLY_RECONCILIATION_SCHEMA,
    state,
    submissionId: requiresCorrelation ? submissionId : "",
    submissionProof: requiresCorrelation ? submissionProof : "",
    proofExpiresAt: requiresCorrelation ? proofExpiresAt : "",
    targetVersion,
    targetCommit,
    preRequestId: boundedContractText(value.preRequestId, 160),
    preStatus: boundedContractText(value.preStatus, 80),
    preExpectedCommit: boundedContractText(value.preExpectedCommit, 80).toLowerCase(),
    preUpdatedAt: boundedContractText(value.preUpdatedAt, 80),
    submittedAtMs: safeSubmittedAtMs,
    modalDeadlineAtMs: safeSubmittedAtMs + UPDATE_APPLY_MODAL_GRACE_MS,
    settleWindowMs: UPDATE_APPLY_ABSENCE_SETTLE_MS,
    successfulUnchangedReads,
    firstUnchangedAtMs,
    lastUnchangedAtMs,
    conflictRequestId: boundedContractText(value.conflictRequestId, 160),
    conflictSubmissionId: boundedContractText(value.conflictSubmissionId, 80).toLowerCase(),
    conflictCommit: boundedContractText(value.conflictCommit, 80).toLowerCase(),
    conflictUpdatedAt: boundedContractText(value.conflictUpdatedAt, 80),
  };
}

function createLegacyUpdateApplyReconciliation(value, nowMs) {
  if (!value || typeof value !== "object" || Array.isArray(value) || ![1, 2].includes(Number(value.schema))) return null;
  const legacyState = boundedContractText(value.state, 48);
  if (!["unresolved", "conflict", "recheck_required", "reconciliation_corrupt"].includes(legacyState)) return null;
  const submittedAtMs = boundedFiniteNumber(value.submittedAtMs, 0, 1, Number.MAX_SAFE_INTEGER);
  if (!submittedAtMs) return null;
  if (legacyState === "reconciliation_corrupt") return createCorruptUpdateApplyReconciliation(nowMs);
  return sanitizeUpdateApplyReconciliation({
    schema: UPDATE_APPLY_RECONCILIATION_SCHEMA,
    state: "legacy_uncorrelated",
    submissionId: "",
    targetVersion: "",
    targetCommit: "",
    submittedAtMs: Math.min(submittedAtMs, nowMs),
  }, nowMs);
}


export function updateApplyReconciliationExactMatch(left, right, nowMs = Date.now()) {
  const first = sanitizeUpdateApplyReconciliation(left, nowMs);
  const second = sanitizeUpdateApplyReconciliation(right, nowMs);
  if (!first || !second) return false;
  return JSON.stringify(first) === JSON.stringify(second);
}

export function restoreUpdateApplyReconciliation(rawValue, nowMs = Date.now()) {
  if (rawValue === null || rawValue === undefined) return null;
  if (typeof rawValue !== "string" || rawValue.length > UPDATE_APPLY_RECONCILIATION_STORAGE_LIMIT) {
    return createCorruptUpdateApplyReconciliation(nowMs);
  }
  try {
    const parsed = JSON.parse(rawValue);
    return sanitizeUpdateApplyReconciliation(parsed, nowMs)
      || createLegacyUpdateApplyReconciliation(parsed, nowMs)
      || createCorruptUpdateApplyReconciliation(nowMs);
  } catch {
    return createCorruptUpdateApplyReconciliation(nowMs);
  }
}

function updateApplyRecheckRecord(record, observedAtMs) {
  return sanitizeUpdateApplyReconciliation({
    ...record,
    state: "recheck_required",
    successfulUnchangedReads: 0,
    firstUnchangedAtMs: null,
    lastUnchangedAtMs: null,
  }, observedAtMs);
}

export function updateApplyRecheckCanClear(record, applyStatus) {
  const safeRecord = sanitizeUpdateApplyReconciliation(record, Date.now());
  if (!safeRecord || !["recheck_required", "reconciliation_corrupt", "legacy_uncorrelated"].includes(safeRecord.state)) return false;
  return updateApplyHasAuthoritativeNoActiveAdmission(applyStatus);
}

export function resetUpdateApplyAbsenceEvidence(record) {
  if (!record) return null;
  return {
    ...record,
    successfulUnchangedReads: 0,
    firstUnchangedAtMs: null,
    lastUnchangedAtMs: null,
  };
}

function updateApplyAdmissionContract(applyStatus) {
  if (!isPlainDataRecord(applyStatus)) return null;
  const admission = ownDataField(applyStatus, "admission");
  if (!isBoundedPlainDataRecord(admission, 16)) return null;
  const schemaVersion = ownDataField(admission, "schema_version");
  const authority = exactBoundedDataText(ownDataField(admission, "authority"), 20).toLowerCase();
  const state = exactBoundedDataText(ownDataField(admission, "state"), 80).toLowerCase();
  const linearizable = ownDataField(admission, "linearizable");
  const active = ownDataField(admission, "active");
  const submissionId = exactBoundedDataText(ownDataField(admission, "submission_id"), 80).toLowerCase();
  const requestId = exactBoundedDataText(ownDataField(admission, "request_id"), 160);
  const targetCommit = exactBoundedDataText(ownDataField(admission, "target_commit"), 40).toLowerCase();
  if (schemaVersion !== 2 || !["active", "inactive", "unknown"].includes(authority) || !state) return null;
  if (authority === "active" && (linearizable !== true || active !== true)) return null;
  if (authority === "inactive" && (linearizable !== true || active !== false)) return null;
  if (authority === "unknown" && (linearizable === true || active !== null)) return null;
  if (submissionId && !UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)) return null;
  if (targetCommit && !UPDATE_APPLY_FULL_COMMIT_PATTERN.test(targetCommit)) return null;
  return { authority, state, submissionId, requestId, targetCommit };
}

function updateApplyHasAuthoritativeNoActiveAdmission(applyStatus) {
  const admission = updateApplyAdmissionContract(applyStatus);
  return Boolean(
    admission?.authority === "inactive"
    && updateApplyIsAuthoritativeInactiveSnapshot(applyStatus),
  );
}

function updateApplyIsAuthoritativeActiveSnapshot(applyStatus) {
  if (!isPlainDataRecord(applyStatus) || applyStatus.schema_version !== 1) return false;
  const admission = updateApplyAdmissionContract(applyStatus);
  const requestId = updateApplyRequestId(applyStatus);
  const submissionId = boundedContractText(applyStatus?.submission_id, 80).toLowerCase();
  const expectedCommit = updateApplyExpectedCommit(applyStatus);
  const status = boundedContractText(applyStatus?.status, 80).toLowerCase();
  const effective = boundedContractText(applyStatus?.effective_status || status, 80).toLowerCase();
  const phase = boundedContractText(applyStatus?.phase || applyStatus?.current_step, 80).toLowerCase();
  const running = updateApplyIsRunning(status);
  const stalled = effective === "stalled" && applyStatus?.is_stale === true && running;
  const current = effective === status && applyStatus?.is_stale === false && running;
  return Boolean(
    admission?.authority === "active"
    && requestId
    && submissionId
    && UPDATE_APPLY_SUBMISSION_ID_PATTERN.test(submissionId)
    && UPDATE_APPLY_FULL_COMMIT_PATTERN.test(expectedCommit)
    && admission.requestId === requestId
    && admission.submissionId === submissionId
    && admission.targetCommit === expectedCommit
    && (current || stalled)
    && phase
    && phase !== "unknown"
    && !UPDATE_APPLY_SYNTHETIC_PHASE_PATTERN.test(phase),
  );
}

function classifyUpdateApplyOperation(record, applyStatus) {
  const admission = updateApplyAdmissionContract(applyStatus);
  const requestId = updateApplyRequestId(applyStatus);
  const submissionId = boundedContractText(applyStatus?.submission_id, 80).toLowerCase();
  const expectedCommit = updateApplyExpectedCommit(applyStatus);
  const active = updateApplyIsAuthoritativeActiveSnapshot(applyStatus);
  const status = boundedContractText(applyStatus?.status, 80).toLowerCase();
  const authoritativeTerminal = updateApplyIsAuthoritativeInactiveSnapshot(applyStatus) && status !== "idle";
  const terminal = authoritativeTerminal && admission?.authority === "inactive";
  const exactIdentity = Boolean(
    record.submissionId
    && submissionId === record.submissionId
    && admission?.submissionId === record.submissionId
    && requestId
    && requestId !== record.preRequestId
    && admission.requestId === requestId,
  );
  const exactTarget = Boolean(
    expectedCommit === record.targetCommit
    && admission?.targetCommit === record.targetCommit,
  );
  const immutableForeignMutation = Boolean(
    record.state === "conflict"
    && record.conflictRequestId
    && record.conflictRequestId === requestId
    && record.conflictCommit
    && record.conflictCommit !== expectedCommit,
  );
  const invalidMatchingIdentity = Boolean(
    record.submissionId
    && submissionId === record.submissionId
    && expectedCommit === record.targetCommit
    && (!requestId || requestId === record.preRequestId || admission?.requestId !== requestId),
  );
  return {
    admission,
    requestId,
    submissionId,
    expectedCommit,
    active,
    terminal,
    foreignTerminal: authoritativeTerminal,
    exact: exactIdentity && exactTarget && !immutableForeignMutation && (active || terminal),
    noActive: updateApplyHasAuthoritativeNoActiveAdmission(applyStatus),
    immutableForeignMutation,
    invalidMatchingIdentity,
  };
}

export function reconcileUpdateApplySubmission(record, applyStatus, observedAtMs) {
  const safeRecord = sanitizeUpdateApplyReconciliation(record, observedAtMs);
  if (!safeRecord) return { outcome: "none", record: null };

  const observed = boundedFiniteNumber(observedAtMs, safeRecord.submittedAtMs, safeRecord.submittedAtMs, Number.MAX_SAFE_INTEGER);
  const operation = classifyUpdateApplyOperation(safeRecord, applyStatus);

  if (["reconciliation_corrupt", "legacy_uncorrelated"].includes(safeRecord.state)) {
    return { outcome: safeRecord.state, record: safeRecord };
  }

  if (safeRecord.state === "recheck_required") {
    return { outcome: "recheck_required", record: safeRecord };
  }

  if (safeRecord.state === "conflict") {
    if (operation.exact) {
      return { outcome: "accepted", record: safeRecord };
    }
    if (operation.immutableForeignMutation) {
      return { outcome: "conflict", record: safeRecord };
    }
    if (operation.invalidMatchingIdentity) {
      return { outcome: "conflict", record: safeRecord };
    }
    if (operation.active) {
      return {
        outcome: "conflict",
        record: sanitizeUpdateApplyReconciliation({
          ...safeRecord,
          conflictRequestId: operation.requestId,
          conflictSubmissionId: operation.submissionId,
          conflictCommit: operation.expectedCommit,
          conflictUpdatedAt: boundedContractText(applyStatus?.updated_at, 80),
        }, observed),
      };
    }
    if (operation.foreignTerminal || operation.noActive) {
      return { outcome: "recheck_required", record: updateApplyRecheckRecord(safeRecord, observed) };
    }
    return { outcome: "conflict", record: safeRecord };
  }

  if (operation.exact) return { outcome: "accepted", record: safeRecord };
  if (operation.invalidMatchingIdentity) return { outcome: "unresolved", record: safeRecord };
  if (operation.noActive) return { outcome: "not_accepted", record: safeRecord };
  if (operation.active) {
    const conflictRecord = sanitizeUpdateApplyReconciliation({
      ...safeRecord,
      state: "conflict",
      conflictRequestId: operation.requestId,
      conflictSubmissionId: operation.submissionId,
      conflictCommit: operation.expectedCommit,
      conflictUpdatedAt: boundedContractText(applyStatus?.updated_at, 80),
    }, observed);
    return { outcome: "conflict", record: conflictRecord };
  }
  if (operation.foreignTerminal) {
    return { outcome: "recheck_required", record: updateApplyRecheckRecord(safeRecord, observed) };
  }
  return { outcome: "unresolved", record: resetUpdateApplyAbsenceEvidence(safeRecord) };
}

export function updateApplyEffectiveStatus(updateStatus, applyStatus, transportContext = "") {
  const status = boundedContractText(applyStatus?.effective_status || applyStatus?.status || updateStatus?.status || "unknown", 80).toLowerCase();
  if (applyStatus?.is_stale || status === "stalled") return "stalled";
  if (status === "completed" && applyStatus?.expected_commit && applyStatus?.commit_verified === false) return "failed";
  const context = transportContext && typeof transportContext === "object"
    ? transportContext
    : { applyError: transportContext };
  if (context.applyError) {
    if (UPDATE_APPLY_PRESERVED_TERMINAL_STATUSES.has(status) || status === "completed") return status;
    if (transportContext && typeof transportContext !== "object" && updateApplyIsRunning(status)) return "reconnecting";
    return updateApplyTransportPhase(applyStatus, context.applyError, context.reconnectTiming, context.nowMs);
  }
  return status;
}

export function formatDurationSeconds(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "-";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}

export function shortCommit(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.length > 12 ? `${text.slice(0, 12)}...` : text;
}

export function updateApplyTrustedCandidateRelease(updateStatus) {
  const candidate = updateStatus?.trusted_apply_candidate;
  if (!candidate?.fresh || !candidate?.latest) return {};
  const latest = candidate.latest || {};
  const available = candidate.available_release || {};
  return {
    version: available.version || latest.version,
    title: available.title || latest.title,
    summary: available.summary || latest.summary,
    changelog: available.changelog || latest.breaking_changes || [],
    published_at: available.published_at || latest.published_at,
    tag: available.tag || latest.source_ref || latest.git_ref,
    commit: available.commit_sha || latest.commit,
    commit_sha: available.commit_sha || latest.commit,
    commit_short: available.commit_short || shortCommit(latest.commit),
    provider: available.provider || candidate.source || "trusted_snapshot",
  };
}

export function updateApplyFactRows(updateStatus, applyStatus, t) {
  const installedRelease = updateStatus?.installed_release || {};
  const trustedRelease = updateApplyTrustedCandidateRelease(updateStatus);
  const availableRelease = updateStatus?.available_release || trustedRelease;
  const latest = updateStatus?.latest || updateStatus?.latest_release || trustedRelease;
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
    [labels.currentStep || "Current step", maintenanceStatusText(applyStatus?.current_step || applyStatus?.phase || status, t)],
    [labels.lastProgress || "Last progress", formatDurationSeconds(applyStatus?.last_progress_age_seconds)],
    [labels.elapsed || "Elapsed", formatDurationSeconds(applyStatus?.elapsed_seconds)],
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
    [labels.releaseIdentity || "Release identity", applyStatus?.release_identity?.metadata_status || installedRelease.metadata_status || "-"],
    [labels.provider || "Provider", availableRelease.provider || updateStatus?.source_channel?.trusted_source_type || "-"],
  ].filter(([, value]) => value !== "-");
}

function updateApplyReleaseValue(updateStatus, key) {
  const installedRelease = updateStatus?.installed_release || {};
  const trustedRelease = updateApplyTrustedCandidateRelease(updateStatus);
  const availableRelease = updateStatus?.available_release || trustedRelease;
  const latest = updateStatus?.latest || updateStatus?.latest_release || trustedRelease;
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  if (key === "currentVersion") return installedRelease.version || installed.app_version || updateStatus?.installed?.installed_version || "-";
  if (key === "availableVersion") return availableRelease.version || latest.version || latest.latest_version || "-";
  if (key === "title") return availableRelease.title || latest.title || installedRelease.title || latest.release_notes_summary || "-";
  if (key === "summary") return availableRelease.summary || latest.release_notes_summary || installedRelease.summary || "";
  if (key === "installedAt") return installedRelease.installed_at || installed.installed_at || "";
  if (key === "targetCommit") return availableRelease.commit || latest.commit || latest.build_id || "";
  if (key === "installedCommit") return installedRelease.commit || installedRelease.commit_sha || installed.git_commit || installed.installed_commit || "";
  if (key === "metadataStatus") return installedRelease.metadata_status || installedRelease.identity_validity || "";
  return "";
}

function localizedReleaseValue(updateStatus, key, t, lang) {
  const value = updateApplyReleaseValue(updateStatus, key);
  if (!value || value === "-") {
    return key === "title"
      ? t.updateApplyReleaseTitleFallback || "-"
      : t.updateApplyReleaseSummaryFallback || "";
  }
  return value;
}

function releaseConfirmedText(value, t) {
  const key = String(value || "").trim().toLowerCase();
  if (["adopted", "already_adopted", "official_update", "valid"].includes(key)) return t.yes || "Yes";
  if (!key) return "-";
  return maintenanceStatusText(key, t);
}

function formatApplyDate(value, lang = "ru") {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 80);
  return new Intl.DateTimeFormat(lang === "en" ? "en-US" : lang === "zh-CN" ? "zh-CN" : "ru-RU", {
    year: "2-digit",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function applyStepIcon(status) {
  if (status === "completed") return "check";
  if (status === "failed") return "alert";
  if (status === "running") return "pulse";
  if (status === "idle") return "idle";
  return "dot";
}

function defaultUpdateApplyTimeline(t) {
  return ["request", "preflight", "applying", "health_check", "commit_verification"].map((name) => ({
    name,
    label: maintenanceStatusText(name, t),
    status: "idle",
    statusLabel: "",
    icon: "idle",
    timeLabel: "",
  }));
}

const UPDATE_APPLY_ATTENTION_STATES = new Set([
  "failed",
  "check_failed",
  "stalled",
  "reconnecting",
  "blocked",
  "cancelled",
  "canceled",
]);

const UPDATE_CHECK_BLOCKING_STATES = new Set([
  "check_failed",
  "identity_incomplete",
  "installed_identity_drift",
  "metadata_stale",
  "provider_unavailable",
  "no_release_published",
  "installed_newer_than_available",
]);

function normalizedUpdateApplyState(value) {
  return String(value || "").trim().toLowerCase();
}

function isUpdateApplyAttentionState(value) {
  return UPDATE_APPLY_ATTENTION_STATES.has(normalizedUpdateApplyState(value));
}

export function updateApplyOperatorModel(updateStatus, applyStatus, t, lang = "ru", transportContext = "") {
  const context = transportContext && typeof transportContext === "object"
    ? transportContext
    : { applyError: transportContext };
  const effective = updateApplyEffectiveStatus(updateStatus, applyStatus, context);
  const comparison = updateStatus?.comparison || {};
  const status = comparison.status || updateStatus?.status || effective || "unknown";
  const normalizedEffective = normalizedUpdateApplyState(effective);
  const normalizedStatus = normalizedUpdateApplyState(status);
  const normalizedUpdateStatus = normalizedUpdateApplyState(updateStatus?.status);
  const lastKnownRunning = updateApplyIsRunning(applyStatus?.status || "");
  const running = lastKnownRunning && normalizedEffective !== "unknown";
  const stateUnknown = Boolean(context.unresolvedSubmission) || (normalizedEffective === "unknown" && Boolean(context.applyError));
  const trustedCandidate = updateStatus?.trusted_apply_candidate || {};
  const freshTrustedCandidateAvailable = Boolean(trustedCandidate.fresh && trustedCandidate.can_apply_from_ui && trustedCandidate.latest);
  const liveCheckFailedWithCandidate = normalizedUpdateStatus === "check_failed" && freshTrustedCandidateAvailable;
  const canApply = Boolean((updateStatus?.can_apply_from_ui || freshTrustedCandidateAvailable) && !lastKnownRunning && !applyStatus?.is_stale && !stateUnknown && !context.unresolvedSubmission);
  const lastSummary = applyStatus?.last_apply_summary || null;
  const currentVersion = updateApplyReleaseValue(updateStatus, "currentVersion");
  const availableVersion = updateApplyReleaseValue(updateStatus, "availableVersion");
  const targetCommit = updateApplyReleaseValue(updateStatus, "targetCommit") || applyStatus?.expected_commit || applyStatus?.source?.commit || "";
  const installedCommit = applyStatus?.installed_commit || updateApplyReleaseValue(updateStatus, "installedCommit");
  const operationExpectedCommit = applyStatus?.expected_commit || lastSummary?.expected_commit || "";
  const operationInstalledCommit = applyStatus?.installed_commit || lastSummary?.installed_commit || installedCommit;
  const operationCommitVerified = Boolean(
    operationExpectedCommit &&
    operationInstalledCommit &&
    operationExpectedCommit === operationInstalledCommit &&
    applyStatus?.commit_verified !== false &&
    lastSummary?.commit_verified !== false
  );
  const comparisonCommitVerified = Boolean(installedCommit && targetCommit && installedCommit === targetCommit);
  const commitVerified = effective === "completed"
    ? operationCommitVerified
    : Boolean(applyStatus?.commit_verified || lastSummary?.commit_verified || comparisonCommitVerified);
  const metadataStatusValue = updateApplyReleaseValue(updateStatus, "metadataStatus") || applyStatus?.release_identity?.metadata_status;
  const identityComplete = normalizedUpdateApplyState(metadataStatusValue) === "complete";
  const terminalSuccess = effective === "completed" && commitVerified && identityComplete;
  const presentedTerminalSuccess = terminalSuccess && !stateUnknown;
  const terminalVerificationIncomplete = effective === "completed" && !terminalSuccess;
  const current = status === "current" && !canApply && !running;
  const available = status === "update_available" && canApply;
  const suppressUpdateCheckBlocker = lastKnownRunning && Boolean(context.applyError || running);
  const helperFailure = isUpdateApplyAttentionState(normalizedEffective) && normalizedEffective !== "reconnecting";
  const updateCheckFailure = !suppressUpdateCheckBlocker && !stateUnknown && (
    isUpdateApplyAttentionState(normalizedStatus) ||
    isUpdateApplyAttentionState(normalizedUpdateStatus) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedEffective) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedStatus) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedUpdateStatus)
  );
  const failed = terminalVerificationIncomplete || helperFailure || (!liveCheckFailedWithCandidate && updateCheckFailure);
  const severity = failed ? "blocked" : running || stateUnknown || available || liveCheckFailedWithCandidate || context.updateError ? "warning" : "ok";
  const headlineKey = stateUnknown
    ? "unknown"
    : presentedTerminalSuccess
    ? "completed"
    : running
      ? "running"
      : failed
          ? "blocked"
          : available || liveCheckFailedWithCandidate
            ? "available"
            : current
              ? "current"
              : "unknown";
  const headline = t.updateApplyHeadlines?.[headlineKey] || maintenanceStatusText(presentedTerminalSuccess ? "completed" : status, t);
  const recoveryStatus = terminalVerificationIncomplete
    ? (commitVerified ? "identity_incomplete" : "failed")
    : stateUnknown
      ? "unknown"
      : isUpdateApplyAttentionState(normalizedEffective) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedEffective)
    ? normalizedEffective
    : isUpdateApplyAttentionState(normalizedUpdateStatus) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedUpdateStatus)
      ? normalizedUpdateStatus
    : isUpdateApplyAttentionState(normalizedStatus) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedStatus)
      ? normalizedStatus
      : failed && normalizedStatus && normalizedStatus !== "unknown"
        ? normalizedStatus
        : effective;
  const recoverySummary = liveCheckFailedWithCandidate
    ? (t.updateApplyRecoveryLiveCheckFailedWithSnapshot || updateApplyRecoveryText("provider_unavailable", applyStatus, t))
    : updateApplyRecoveryText(recoveryStatus, applyStatus, t);
  const summary = failed || liveCheckFailedWithCandidate || stateUnknown ? recoverySummary : (t.updateApplySummaries?.[headlineKey] || recoverySummary);
  const updateResult = presentedTerminalSuccess
    ? (t.updateApplyResults?.completedVerified || headline)
    : t.updateApplyResults?.[headlineKey] || headline;
  const finishedAt = lastSummary?.finished_at || (presentedTerminalSuccess ? applyStatus?.updated_at : "") || updateApplyReleaseValue(updateStatus, "installedAt");
  const elapsed = running
    ? formatDurationSeconds(applyStatus?.elapsed_seconds)
    : lastSummary?.elapsed_seconds
      ? formatDurationSeconds(lastSummary.elapsed_seconds)
      : "-";
  const liveStepsAvailable = Array.isArray(applyStatus?.steps) && applyStatus.steps.length;
  const historyStepsAvailable = Array.isArray(lastSummary?.steps) && lastSummary.steps.length;
  const stepsSource = liveStepsAvailable
    ? applyStatus
    : historyStepsAvailable
      ? lastSummary
      : null;
  const inactiveTimeline = !running;
  const timeline = updateApplyStepRows(stepsSource || {}, t).map((step) => ({
    ...step,
    status: inactiveTimeline ? "idle" : step.status,
    icon: applyStepIcon(inactiveTimeline ? "idle" : step.status),
    timeLabel: inactiveTimeline ? "" : (step.time_label || (step.status === "running" ? step.statusLabel : "")),
  }));
  const detailUnavailable = Boolean((lastSummary?.history_detail_status || applyStatus?.apply_history?.state === "missing") && !timeline.some((step) => step.timeLabel && /:/.test(step.timeLabel)));
  const safeTimeline = timeline.length ? timeline : defaultUpdateApplyTimeline(t);
  return {
    status: effective,
    severity,
    headline,
    summary,
    showHeroSummary: !presentedTerminalSuccess,
    updateResult,
    currentVersion,
    availableVersion,
    releaseTitle: localizedReleaseValue(updateStatus, "title", t, lang),
    releaseSummary: localizedReleaseValue(updateStatus, "summary", t, lang),
    installedAt: formatApplyDate(updateApplyReleaseValue(updateStatus, "installedAt"), lang),
    finishedAt: formatApplyDate(finishedAt, lang),
    elapsed,
    lastProgress: running ? formatDurationSeconds(applyStatus?.last_progress_age_seconds) : "",
    commitStatus: commitVerified ? t.updateCommitVerified : targetCommit ? t.updateCommitPending : t.updateCommitUnavailable,
    commitVerified,
    installedCommitShort: shortCommit(installedCommit) || "-",
    targetCommitShort: shortCommit(targetCommit) || "-",
    metadataStatus: releaseConfirmedText(metadataStatusValue, t),
    canApply,
    canCheck: true,
    showApplyButton: canApply || lastKnownRunning || Boolean(context.unresolvedSubmission),
    timeline: safeTimeline.slice(0, 5),
    detailUnavailable,
    diagnosticsRows: updateApplyTechnicalRows(updateStatus, applyStatus, t),
    stateUnknown,
    reconnecting: normalizedEffective === "reconnecting",
  };
}

export function updateApplyRecoveryText(status, applyStatus, t) {
  const effective = status || "unknown";
  if (effective === "stalled") return applyStatus?.error?.operator_action || t.updateApplyRecoveryStalled;
  if (effective === "reconnecting") return t.updateApplyRecoveryReconnecting;
  if (effective === "completed" && applyStatus?.commit_verified) return t.updateApplyRecoveryCompleted;
  if (effective === "completed" && applyStatus?.expected_commit && applyStatus?.commit_verified === false) return t.updateApplyRecoveryCommitMismatch;
  if (effective === "failed") return t.updateApplyRecoveryFailed;
  if (effective === "check_failed") return t.updateApplyRecoveryCheckFailed || t.updateApplyRecoveryFailed;
  if (["update_check_required", "trusted_snapshot_stale", "trusted_snapshot_invalidated", "manifest_version_changed", "manifest_commit_changed"].includes(effective)) {
    return t.updateApplyRecoveryRefreshRequired || t.updateApplyRecoveryCheckFailed || t.updateApplyRecoveryBlocked;
  }
  if (effective === "trusted_commit_missing") return t.updateApplyRecoveryMissingCommit || t.updateApplyRecoveryBlocked;
  if (effective === "blocked" || effective === "not_configured") return t.updateApplyRecoveryBlocked;
  if (updateApplyIsRunning(effective)) return t.updateApplyRecoveryRunning;
  if (effective === "current") return t.updateApplyRecoveryCurrent;
  if (effective === "update_available") return t.updateApplyRecoveryAvailable;
  if (effective === "identity_incomplete" || effective === "installed_identity_drift" || effective === "metadata_stale") return t.updateApplyRecoveryIdentity;
  if (effective === "provider_unavailable" || effective === "no_release_published") return t.updateApplyRecoveryProvider;
  if (effective === "installed_newer_than_available") return t.updateApplyRecoveryInstalledNewer;
  return t.updateApplyRecoveryUnknown;
}

export function updateApplyStepRows(applyStatus, t) {
  const steps = Array.isArray(applyStatus?.steps) ? applyStatus.steps : [];
  const stageNames = ["request", "preflight", "applying", "health_check", "commit_verification"];
  const stageFor = (name) => {
    if (name === "queued" || name === "request" || name === "starting_helper") return "request";
    if (name === "preflight") return "preflight";
    if (name === "health_check") return "health_check";
    if (name === "commit_verification" || name === "completed") return "commit_verification";
    if ([
      "acquire_source",
      "downloading",
      "extracting",
      "validating_source",
      "overlay",
      "applying",
      "compose_config",
      "rebuilding",
      "restarting",
    ].includes(name)) return "applying";
    return "";
  };
  const rank = { failed: 5, running: 4, completed: 3, pending: 2, idle: 1 };
  const normalizeStatus = (value) => {
    const status = String(value || "pending").trim().toLowerCase();
    if (["failed", "error", "blocked", "cancelled", "canceled", "stalled"].includes(status)) return "failed";
    if (["running", "in_progress", "starting", "active"].includes(status)) return "running";
    if (["completed", "complete", "ok", "done", "verified"].includes(status)) return "completed";
    if (status === "idle") return "idle";
    return "pending";
  };
  const grouped = new Map(stageNames.map((name) => [name, {
    name,
    label: maintenanceStatusText(name, t),
    status: "pending",
    statusLabel: maintenanceStatusText("pending", t),
  }]));
  for (const step of steps) {
    const name = String(step?.name || "").trim();
    const stage = stageFor(name);
    if (!stage || !grouped.has(stage)) continue;
    const status = normalizeStatus(step?.status);
    const current = grouped.get(stage);
    if (rank[status] >= rank[current.status]) {
      grouped.set(stage, {
        name: stage,
        label: maintenanceStatusText(stage, t),
        status,
        statusLabel: maintenanceStatusText(status, t),
        ...((step?.time_label || step?.completed_at || step?.updated_at) ? { time_label: step?.time_label || step?.completed_at || step?.updated_at } : {}),
      });
    }
  }
  return stageNames.map((name) => grouped.get(name));
}

export function updateApplyButtonText(applyStatus, t) {
  const step = applyStatus?.current_step || applyStatus?.phase || applyStatus?.status;
  if (!updateApplyIsRunning(applyStatus?.status || "")) return t.updateApplyStart;
  if (step === "rebuilding") return t.updateApplyButtonRebuilding || maintenanceStatusText("rebuilding", t);
  if (step === "health_check") return t.updateApplyButtonHealth || maintenanceStatusText("health_check", t);
  if (step === "commit_verification") return t.updateApplyButtonVerification || maintenanceStatusText("commit_verification", t);
  return t.updateApplyButtonRunning || maintenanceStatusText(step, t);
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
  if ((code === "update_check_already_running" || code === "manual_update_check_rate_limited") && labels[code]) return labels[code];
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

export function updateApplyErrorMessages(error, t, lang = "ru") {
  if (!error || typeof error !== "object") return [];
  const messages = [
    error.message ? formatMaintenanceMessage(error.message, t, lang, "error") : "",
    error.operator_action ? formatMaintenanceMessage(error.operator_action, t, lang, "action") : "",
  ].filter(Boolean);
  return [...new Set(messages)];
}

export function buildUpdateApplyConfirmation(t, updateStatus) {
  const trustedRelease = updateApplyTrustedCandidateRelease(updateStatus);
  const latest = updateStatus?.latest || updateStatus?.latest_release || trustedRelease;
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  const lines = [t.updateApplyConfirm];
  if (installed.app_version || updateStatus?.installed?.installed_version) lines.push(`${t.updateCurrent}: ${installed.app_version || updateStatus?.installed?.installed_version}`);
  if (latest.version || latest.latest_version) lines.push(`${t.updateLatest}: ${latest.version || latest.latest_version}`);
  if (latest.commit || latest.commit_sha || latest.build_id) lines.push(`${t.maintenanceLabels?.targetCommit}: ${shortCommit(latest.commit || latest.commit_sha || latest.build_id)}`);
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
      showCheck: Boolean(presentation.can_check && userStatus !== "ok"),
      supportReportAvailable: Boolean(presentation.support_report_available),
      facts: facts
        .filter((item) => item && item.value !== null && item.value !== undefined)
        .map((item) => [factLabels[item.key] || item.key, maintenanceBooleanText(item.value, t)])
        .slice(0, 3),
    };
  });
}

function backupArtifactStatusLabel(artifact, t) {
  const labels = t.maintenanceBackupStatuses || {};
  if (artifact?.valid) return labels.valid || maintenanceStatusText("verified", t);
  const status = artifact?.validation_status || "problem";
  return labels[status] || labels.problem || maintenanceStatusText(status, t);
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
  const status = String(result?.status || "").trim().toLowerCase();
  const operationLabels = t.maintenanceBackupOperationLabels || {};
  if (kind === "create") {
    const labels = t.maintenanceBackupCreateStatuses || {};
    const created = ["ok", "created", "verified", "valid", "completed", "complete", "satisfied"].includes(status);
    const failed = ["blocked", "failed", "error", "backup_failed"].includes(status);
    return {
      kind,
      label: operationLabels.create || t.maintenanceBackupCreate || "Create",
      text: labels[status] || (created ? t.maintenanceBackupCreated : failed ? t.maintenanceBackupCreateFailed : labels.fallback || t.maintenanceMessageFallback || maintenanceStatusText(status, t)),
      showReason: !created,
    };
  }
  if (kind === "delete") {
    const labels = t.maintenanceBackupDeleteStatuses || {};
    const deleted = ["deleted", "deleted_with_missing_files", "ok", "completed", "complete"].includes(status);
    const failed = ["blocked", "failed", "error", "delete_failed", "not_found"].includes(status);
    return {
      kind,
      label: operationLabels.delete || t.maintenanceBackupDelete || "Delete",
      text: labels[status] || (deleted ? t.maintenanceBackupDeleted : failed ? t.maintenanceBackupDeleteFailed : labels.fallback || t.maintenanceMessageFallback || maintenanceStatusText(status, t)),
      showReason: !deleted,
    };
  }
  const checked = ["valid", "verified", "available"].includes(status);
  return {
    kind: "check",
    label: operationLabels.check || t.maintenanceBackupCheck || "Check",
    text: maintenanceBackupCheckResultText(status, t),
    showReason: !checked,
  };
}

export function maintenanceBackupManagerModel(overview, t, lang = "ru") {
  const restore = overview?.flows?.restore || {};
  const details = restore?.details || {};
  const artifacts = Array.isArray(details.artifacts) ? details.artifacts : [];
  const sortedArtifacts = [...artifacts].sort((left, right) => {
    const leftTime = new Date(left?.artifact_created_at || 0).getTime() || 0;
    const rightTime = new Date(right?.artifact_created_at || 0).getTime() || 0;
    return rightTime - leftTime;
  });
  const validCount = sortedArtifacts.filter((item) => item?.valid).length;
  const problemCount = Math.max(0, sortedArtifacts.length - validCount);
  const latest = sortedArtifacts[0] || null;
  const productionRestoreSupported = Boolean(details.current_product_restore_supported);
  const temporaryValidationSupported = Boolean(details.temporary_validation_restore_supported);
  const copyWord = sortedArtifacts.length === 1 ? t.maintenanceBackupCopyOne : t.maintenanceBackupCopyMany;
  const renderedCopyWord = String(copyWord || "").includes("{count}")
    ? String(copyWord || "").replace("{count}", String(sortedArtifacts.length))
    : `${sortedArtifacts.length} ${copyWord || ""}`.trim();
  return {
    total: sortedArtifacts.length,
    valid: validCount,
    problem: problemCount,
    totalCount: sortedArtifacts.length,
    validCount,
    problemCount,
    countText: sortedArtifacts.length ? renderedCopyWord : t.maintenanceBackupNoCopies,
    latest,
    latestCreatedAt: latest?.artifact_created_at ? formatAuditTimestamp(latest.artifact_created_at, lang) : "-",
    latestStatus: latest ? backupArtifactStatusLabel(latest, t) : t.maintenanceBackupNoCopies,
    statusText: sortedArtifacts.length
      ? (t.maintenanceBackupStatusReady || "").replace("{count}", String(sortedArtifacts.length)).replace("{copy}", copyWord)
      : t.maintenanceBackupStatusEmpty,
    canCheck: Boolean(validCount && temporaryValidationSupported),
    canDelete: sortedArtifacts.some((item) => item?.deletable),
    restoreSupported: productionRestoreSupported,
    restoreText: productionRestoreSupported ? t.maintenanceBackupRestoreAvailable : t.maintenanceBackupRestoreUnavailable,
    restoreReason: productionRestoreSupported ? "" : t.maintenanceBackupRestoreUnavailableReason,
    artifacts: sortedArtifacts.slice(0, 6).map((item) => ({
      id: item.artifact_id,
      createdAt: item.artifact_created_at ? formatAuditTimestamp(item.artifact_created_at, lang) : "-",
      size: formatFileSize(item.file_size),
      status: backupArtifactStatusLabel(item, t),
      valid: Boolean(item.valid),
      deletable: Boolean(item.deletable),
      canDelete: Boolean(item.deletable),
      canCheck: Boolean(item.valid && temporaryValidationSupported),
      schema: item.artifact_schema_version ?? "-",
      backend: item.db_backend || "-",
      sizeText: formatFileSize(item.file_size),
    })),
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
