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

export function updateApplyEffectiveStatus(updateStatus, applyStatus, transientError = "") {
  const status = applyStatus?.effective_status || applyStatus?.status || updateStatus?.status || "unknown";
  if (applyStatus?.is_stale || status === "stalled") return "stalled";
  if (transientError && updateApplyIsRunning(status)) return "reconnecting";
  if (status === "completed" && applyStatus?.expected_commit && applyStatus?.commit_verified === false) return "failed";
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

export function updateApplyOperatorModel(updateStatus, applyStatus, t, lang = "ru", transientError = "") {
  const effective = updateApplyEffectiveStatus(updateStatus, applyStatus, transientError);
  const comparison = updateStatus?.comparison || {};
  const status = comparison.status || updateStatus?.status || effective || "unknown";
  const normalizedEffective = normalizedUpdateApplyState(effective);
  const normalizedStatus = normalizedUpdateApplyState(status);
  const normalizedUpdateStatus = normalizedUpdateApplyState(updateStatus?.status);
  const running = updateApplyIsRunning(applyStatus?.status || "");
  const trustedCandidate = updateStatus?.trusted_apply_candidate || {};
  const freshTrustedCandidateAvailable = Boolean(trustedCandidate.fresh && trustedCandidate.can_apply_from_ui && trustedCandidate.latest);
  const liveCheckFailedWithCandidate = normalizedUpdateStatus === "check_failed" && freshTrustedCandidateAvailable;
  const canApply = Boolean((updateStatus?.can_apply_from_ui || freshTrustedCandidateAvailable) && !running && !applyStatus?.is_stale);
  const lastSummary = applyStatus?.last_apply_summary || null;
  const currentVersion = updateApplyReleaseValue(updateStatus, "currentVersion");
  const availableVersion = updateApplyReleaseValue(updateStatus, "availableVersion");
  const targetCommit = updateApplyReleaseValue(updateStatus, "targetCommit") || applyStatus?.expected_commit || applyStatus?.source?.commit || "";
  const installedCommit = applyStatus?.installed_commit || updateApplyReleaseValue(updateStatus, "installedCommit");
  const commitVerified = Boolean(
    applyStatus?.commit_verified ||
    lastSummary?.commit_verified ||
    (installedCommit && targetCommit && installedCommit === targetCommit)
  );
  const terminalSuccess = effective === "completed" && commitVerified;
  const current = status === "current" && !canApply && !running;
  const available = status === "update_available" && canApply;
  const failed = !liveCheckFailedWithCandidate && (isUpdateApplyAttentionState(normalizedEffective) ||
    isUpdateApplyAttentionState(normalizedStatus) ||
    isUpdateApplyAttentionState(normalizedUpdateStatus) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedEffective) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedStatus) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedUpdateStatus));
  const severity = failed ? "blocked" : running || available || liveCheckFailedWithCandidate || transientError ? "warning" : "ok";
  const headlineKey = terminalSuccess ? "completed" : running ? "running" : failed ? "blocked" : available || liveCheckFailedWithCandidate ? "available" : current ? "current" : "current";
  const headline = t.updateApplyHeadlines?.[headlineKey] || maintenanceStatusText(terminalSuccess ? "completed" : status, t);
  const recoveryStatus = isUpdateApplyAttentionState(normalizedEffective) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedEffective)
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
  const summary = failed || liveCheckFailedWithCandidate ? recoverySummary : (t.updateApplySummaries?.[headlineKey] || recoverySummary);
  const updateResult = terminalSuccess
    ? (t.updateApplyResults?.completedVerified || headline)
    : t.updateApplyResults?.[headlineKey] || headline;
  const finishedAt = lastSummary?.finished_at || (terminalSuccess ? applyStatus?.updated_at : "") || updateApplyReleaseValue(updateStatus, "installedAt");
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
    showHeroSummary: !terminalSuccess,
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
    metadataStatus: releaseConfirmedText(updateApplyReleaseValue(updateStatus, "metadataStatus") || applyStatus?.release_identity?.metadata_status, t),
    canApply,
    canCheck: true,
    showApplyButton: canApply || running,
    timeline: safeTimeline.slice(0, 5),
    detailUnavailable,
    diagnosticsRows: updateApplyTechnicalRows(updateStatus, applyStatus, t),
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
    if (typeof value.summary === "string") return value.summary;
    if (typeof value.error === "string") return value.error;
    if (typeof value.message === "string") return value.message;
    if (typeof value.error_category === "string") return value.error_category;
    if (value.retry_after_seconds !== undefined) return `${fallback} (${value.retry_after_seconds}s)`;
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
