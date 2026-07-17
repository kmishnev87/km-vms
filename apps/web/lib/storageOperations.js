export function asNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function formatBytes(value) {
  const bytes = asNumber(value, 0);
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let current = bytes;
  let index = 0;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  const precision = current >= 10 || index === 0 ? 0 : 1;
  return `${current.toFixed(precision)} ${units[index]}`;
}

export function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(number >= 10 ? 1 : 2)}%`;
}

export function activationProgressModel(status = {}) {
  const completed = new Set(Array.isArray(status?.completed_steps) ? status.completed_steps : []);
  const current = String(status?.current_step || "");
  const definitions = [
    { key: "recordings_stopped", active: ["recordings_stopping", "recordings_stopped"] },
    { key: "runtime_applied", active: ["root_preflight_checked", "runtime_activation_requested", "runtime_applied"] },
    { key: "cameras_restored", active: ["cameras_restoring", "cameras_restored"] },
    { key: "archive_access_checked", active: ["archive_access_checking", "archive_access_checked"] },
  ];
  return {
    status: String(status?.status || "idle"),
    operationId: status?.operation_id || null,
    steps: definitions.map((definition) => ({
      key: definition.key,
      done: completed.has(definition.key),
      active: !completed.has(definition.key) && definition.active.includes(current),
    })),
    rollback: {
      status: String(status?.rollback_status || "not_required"),
      active: current.startsWith("rollback_") && current !== "rollback_completed",
      completed: completed.has("rollback_completed"),
      failed: status?.status === "failed_recovery_required" || status?.rollback_status === "failed",
    },
    recoveryRequired: status?.status === "failed_recovery_required",
    effectiveRootLabel: status?.effective_active_root_label || null,
    targetRootLabel: status?.target_root_label || null,
    previousRootLabel: status?.previous_root_label || null,
    presentationKey: status?.presentation_key || null,
  };
}

export function discoveryStateModel(discovery = {}) {
  const freshness = String(discovery?.freshness || discovery?.status || "unavailable");
  const current = freshness === "current" && discovery?.available === true && Boolean(discovery?.snapshot_id);
  return {
    freshness,
    current,
    refreshing: freshness === "refreshing" || discovery?.status === "refreshing" || discovery?.refresh_in_progress === true,
    stale: freshness === "stale",
    unavailable: freshness === "unavailable" || (!current && freshness !== "refreshing" && freshness !== "stale"),
    snapshotId: current ? discovery.snapshot_id : null,
    candidates: current && Array.isArray(discovery?.candidates) ? discovery.candidates : [],
    reasonCode: discovery?.refresh_error || discovery?.reason_code || null,
  };
}

export function discoveryHeaderStatusModel(discovery = null) {
  if (!discovery) {
    return { state: "not_checked", tone: "neutral", needsRefresh: true };
  }
  const model = discoveryStateModel(discovery);
  if (model.refreshing) return { state: "refreshing", tone: "neutral", needsRefresh: false };
  if (model.current) return { state: "current", tone: "ok", needsRefresh: false };
  if (model.stale) return { state: "stale", tone: "warning", needsRefresh: true };
  return { state: "unavailable", tone: "warning", needsRefresh: true };
}

export function archiveRootCleanupCapabilityModel(detail = {}) {
  const expectedActions = {
    immediate: "retry_cleanup",
    after_refresh: "refresh_storage_state",
    after_external_fix: "correct_storage_access",
    none: "close",
  };
  const requestedMode = String(detail?.retry_mode || "none");
  const requestedAction = String(detail?.next_action || "close");
  const valid = Object.prototype.hasOwnProperty.call(expectedActions, requestedMode)
    && expectedActions[requestedMode] === requestedAction;
  const retryMode = valid ? requestedMode : "none";
  const nextAction = valid ? requestedAction : "close";
  return {
    retryMode,
    nextAction,
    canRetryNow: retryMode === "immediate",
    shouldRefresh: retryMode === "after_refresh",
    needsExternalFix: retryMode === "after_external_fix",
    retryAvailable: retryMode === "immediate",
  };
}

export function formatDateTime(value, language = "ru") {
  if (!value) {
    if (language === "en") return "Never";
    if (language === "zh-CN") return "从未运行";
    return "Не запускалось";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(language === "en" ? "en-US" : language === "zh-CN" ? "zh-CN" : "ru-RU");
}

const RECENT_OPERATION_TYPE_KEYS = Object.freeze({
  retention_auto_run: "recentOperationRetentionAutomatic",
  retention_run: "recentOperationRetentionManual",
  retention_auto_free_space: "recentOperationAutoFree",
  archive_root_activation: "recentOperationRootActivation",
  archive_root_delete: "recentOperationRootDelete",
  camera_delete_with_files: "recentOperationCameraDelete",
  manual_bulk_delete: "recentOperationBulkDelete",
  manual_single_delete: "recentOperationSingleDelete",
  integrity_metadata_repair: "recentOperationIntegrityRepair",
  integrity_catalog_retirement: "recentOperationIntegrityRetirement",
  integrity_recording_delete: "recentOperationIntegrityRecordingDelete",
  integrity_scan: "recentOperationIntegrityScan",
  integrity_plan_prepare: "recentOperationIntegrityPlan",
  orphan_file_cleanup: "recentOperationOrphanCleanup",
  archive_migration_apply: "recentOperationMigration",
});

const RECENT_OPERATION_STATUS = Object.freeze({
  queued: { labelKey: "recentOperationStatusQueued", tone: "neutral" },
  running: { labelKey: "recentOperationStatusRunning", tone: "neutral" },
  cancel_requested: { labelKey: "recentOperationStatusCancelRequested", tone: "warning" },
  completed: { labelKey: "recentOperationStatusCompleted", tone: "ok" },
  partial: { labelKey: "recentOperationStatusPartial", tone: "warning" },
  blocked: { labelKey: "recentOperationStatusBlocked", tone: "warning" },
  failed: { labelKey: "recentOperationStatusFailed", tone: "error" },
  cancelled: { labelKey: "recentOperationStatusCancelled", tone: "neutral" },
  interrupted: { labelKey: "recentOperationStatusInterrupted", tone: "warning" },
  unknown: { labelKey: "recentOperationStatusUnknown", tone: "neutral" },
});

const RECENT_OPERATION_NEXT_ACTION_KEYS = Object.freeze({
  check_storage_access: "recentOperationNextCheckStorage",
  resume_after_storage_pressure: "recentOperationNextWaitForSpace",
  retry_operation: "recentOperationNextRetry",
  review_and_confirm_auto_free_terms: "recentOperationNextConfirmAutoFree",
  review_storage_problems: "recentOperationNextReviewProblems",
  create_new_integrity_scan: "recentOperationNextNewIntegrityScan",
  retry_remediation: "recentOperationNextRetryRemediation",
});

const DELETION_OPERATION_TYPES = new Set([
  "retention_auto_run",
  "retention_run",
  "retention_auto_free_space",
  "archive_root_delete",
  "camera_delete_with_files",
  "manual_bulk_delete",
  "manual_single_delete",
]);

function finiteCount(value) {
  if (value == null || value === "") return null;
  const count = Number(value);
  return Number.isFinite(count) && count >= 0 ? count : null;
}

export function recentOperationPresentation(item = {}) {
  const operationType = String(item?.operation_type || "");
  const status = String(item?.status || "unknown");
  const progress = item?.progress && typeof item.progress === "object" ? item.progress : {};
  const facts = [];
  const deletedCount = finiteCount(progress.deleted_count);
  const completedCount = finiteCount(progress.completed_count);
  const failedCount = finiteCount(progress.failed_count);
  const skippedCount = finiteCount(progress.skipped_count);
  const completedBytes = finiteCount(progress.completed_bytes ?? progress.bytes_freed);
  const effectiveDeletedCount = deletedCount ?? (DELETION_OPERATION_TYPES.has(operationType) ? completedCount : null);
  if (effectiveDeletedCount != null) facts.push({ labelKey: "recentOperationDeletedCount", value: effectiveDeletedCount });
  if (completedBytes != null) facts.push({ labelKey: "recentOperationFreedBytes", value: completedBytes, format: "bytes" });
  if (failedCount != null && failedCount > 0) facts.push({ labelKey: "recentOperationFailedCount", value: failedCount });
  if (skippedCount != null && skippedCount > 0) facts.push({ labelKey: "recentOperationSkippedCount", value: skippedCount });
  const statusPresentation = RECENT_OPERATION_STATUS[status] || RECENT_OPERATION_STATUS.unknown;
  return {
    key: String(item?.operation_id || `${operationType || "operation"}:${item?.finished_at || item?.started_at || "unknown"}`),
    typeKey: RECENT_OPERATION_TYPE_KEYS[operationType] || "recentOperationGeneric",
    statusKey: statusPresentation.labelKey,
    tone: statusPresentation.tone,
    timestamp: item?.finished_at || item?.started_at || item?.queued_at || null,
    facts,
    reasonCode: typeof item?.reason_code === "string" ? item.reason_code : null,
    nextActionKey: RECENT_OPERATION_NEXT_ACTION_KEYS[item?.next_action] || null,
  };
}

export function recentOperationPresentations(items, limit = 5) {
  const boundedLimit = Math.max(0, Math.min(Number(limit) || 0, 5));
  return (Array.isArray(items) ? items : []).slice(0, boundedLimit).map(recentOperationPresentation);
}

export function isStorageAccessDeniedError(error) {
  const status = Number(error?.status ?? error?.response?.status);
  if (status === 401 || status === 403) return true;

  const message = String(error?.message || error || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
  if (!message) return false;

  return [
    "http 401",
    "http 403",
    "forbidden",
    "not authenticated",
    "invalid token",
    "insufficient permissions",
    "permission denied",
    "права пользователя ограничены",
    "недостаточно прав",
  ].some((phrase) => message.includes(phrase));
}

export function statusLabel(value, language = "ru") {
  const labels = {
    ru: {
      available: "Доступно",
      blocked: "Заблокировано",
      degraded: "Есть предупреждения",
      unavailable: "Недоступно",
      ok: "Норма",
      warning: "Предупреждение",
      critical: "Критично",
      cleanup_threshold: "Порог автоосвобождения",
      capacity_unknown: "Место неизвестно",
      problems_found: "Найдены проблемы",
      pending: "Ожидает",
      running: "Выполняется",
      never_run: "Не запускалось",
      completed_with_warnings: "Завершено с предупреждениями",
      failed: "Ошибка",
      idle: "Ожидает действия",
      unavailable_due_to_permissions: "Недоступно по правам",
      check_needed: "Нужна проверка",
      preview_completed: "Предпросмотр готов",
      apply_completed: "Применение завершено",
      apply_blocked: "Применение заблокировано",
      apply_failed: "Применение завершилось ошибкой",
      unknown: "Неизвестно",
    },
    en: {
      available: "Available",
      blocked: "Blocked",
      degraded: "Warnings",
      unavailable: "Unavailable",
      ok: "OK",
      warning: "Warning",
      critical: "Critical",
      cleanup_threshold: "Cleanup threshold",
      capacity_unknown: "Capacity unknown",
      problems_found: "Problems found",
      pending: "Pending",
      running: "Running",
      never_run: "Never run",
      completed_with_warnings: "Completed with warnings",
      failed: "Failed",
      idle: "Waiting for action",
      unavailable_due_to_permissions: "Permission limited",
      check_needed: "Check needed",
      preview_completed: "Preview ready",
      apply_completed: "Apply completed",
      apply_blocked: "Apply blocked",
      apply_failed: "Apply failed",
      unknown: "Unknown",
    },
    "zh-CN": {
      available: "可用",
      blocked: "已阻止",
      degraded: "有告警",
      unavailable: "不可用",
      ok: "正常",
      warning: "告警",
      critical: "严重",
      cleanup_threshold: "自动清理阈值",
      capacity_unknown: "容量未知",
      problems_found: "发现问题",
      pending: "等待中",
      running: "运行中",
      never_run: "从未运行",
      completed_with_warnings: "已完成但有告警",
      failed: "失败",
      idle: "等待操作",
      unavailable_due_to_permissions: "权限受限",
      check_needed: "需要检查",
      preview_completed: "预览已就绪",
      apply_completed: "应用已完成",
      apply_blocked: "应用已阻止",
      apply_failed: "应用失败",
      unknown: "未知",
    },
  };
  const table = labels[language] || labels.ru;
  return table[value] || table.unknown;
}

export function boolLabel(value, language = "ru") {
  if (language === "en") return value ? "Yes" : "No";
  if (language === "zh-CN") return value ? "是" : "否";
  return value ? "Да" : "Нет";
}

export function factLabel(value, language = "ru") {
  if (value === true) return boolLabel(true, language);
  if (value === false) return boolLabel(false, language);
  if (value === "permission_limited" || value === "limited") {
    if (language === "en") return "Limited";
    if (language === "zh-CN") return "受限";
    return "Ограничено";
  }
  if (value === "unavailable") {
    if (language === "en") return "Unavailable";
    if (language === "zh-CN") return "不可用";
    return "Недоступно";
  }
  if (language === "en") return "Unknown";
  if (language === "zh-CN") return "未知";
  return "Не проверено";
}

export function factTone(value) {
  if (value === true) return "ok";
  if (value === false || value === "unavailable") return "error";
  if (value === "permission_limited" || value === "limited") return "warning";
  return "unknown";
}

export function accessRightsModel(pathHealth = {}, language = "ru") {
  const readable = pathHealth?.readable;
  const writable = pathHealth?.writable;
  const labels = {
    ru: {
      ok: "Права на чтение и запись: есть",
      writeLimited: "Чтение есть, запись недоступна",
      readLimited: "Чтение недоступно, запись есть",
      none: "Права на чтение и запись: нет",
      unknown: "Права на чтение и запись: не проверены",
    },
    en: {
      ok: "Read and write access: available",
      writeLimited: "Read is available, write is unavailable",
      readLimited: "Read is unavailable, write is available",
      none: "Read and write access: unavailable",
      unknown: "Read and write access: not checked",
    },
    "zh-CN": {
      ok: "读写权限：可用",
      writeLimited: "可读取，无法写入",
      readLimited: "无法读取，可写入",
      none: "读写权限：不可用",
      unknown: "读写权限：未检查",
    },
  };
  const text = labels[language] || labels.ru;
  if (readable === true && writable === true) return { status: "ok", tone: "ok", label: text.ok };
  if (readable === true && writable === false) return { status: "write_limited", tone: "error", label: text.writeLimited };
  if (readable === false && writable === true) return { status: "read_limited", tone: "error", label: text.readLimited };
  if (readable === false && writable === false) return { status: "none", tone: "error", label: text.none };
  return { status: "unknown", tone: "unknown", label: text.unknown };
}

export function freeSpaceTone(capacity = {}, policy = {}) {
  if (policy?.recording_suspended_by_low_disk || policy?.state === "critical") return "error";
  if (policy?.state === "cleanup_threshold" || policy?.state === "warning") return "warning";
  const freePercent = Number(capacity?.free_percent);
  const warning = Number(policy?.warning_threshold_percent ?? 10);
  const critical = Number(policy?.critical_threshold_percent ?? 1);
  if (Number.isFinite(freePercent) && Number.isFinite(critical) && freePercent <= critical) return "error";
  if (Number.isFinite(freePercent) && Number.isFinite(warning) && freePercent <= warning) return "warning";
  return "neutral";
}

export function policyStateLabel(policy, language = "ru") {
  const enabled = Boolean(policy?.auto_free_space_cleanup_effective ?? policy?.auto_free_space_cleanup_enabled);
  if (language === "en") return enabled ? "ON" : "OFF";
  if (language === "zh-CN") return enabled ? "开启" : "关闭";
  return enabled ? "Включено" : "Выключено";
}

export function lowDiskPolicyText(policy, language = "ru") {
  const cleanupEnabled = Boolean(policy?.auto_free_space_cleanup_effective ?? policy?.auto_free_space_cleanup_enabled);
  const warning = policy?.warning_threshold_percent ?? 10;
  const cleanup = policy?.cleanup_threshold_percent ?? 5;
  const critical = policy?.critical_threshold_percent ?? 1;
  if (language === "en") {
    return cleanupEnabled
      ? `Warning below ${warning}% free. Below ${cleanup}% the system may delete only KM VMS-owned recordings confirmed by metadata safety checks. Below ${critical}% recording may be suspended.`
      : `Warning below ${warning}% free. Automatic deletion is OFF; below ${critical}% recording may still be suspended to protect the disk.`;
  }
  if (language === "zh-CN") {
    return cleanupEnabled
      ? `可用空间低于 ${warning}% 时显示告警。低于 ${cleanup}% 时，系统只能删除属于 KM VMS 且已通过元数据安全检查的旧录像。低于 ${critical}% 时可暂停录像以保护磁盘。`
      : `可用空间低于 ${warning}% 时仅显示告警。自动删除已关闭；低于 ${critical}% 时仍可暂停录像以保护磁盘。`;
  }
  return cleanupEnabled
    ? `Ниже ${warning}% система предупреждает о нехватке места. Ниже ${cleanup}% она может автоматически удалить только старые записи, принадлежащие KM VMS и безопасно подтвержденные метаданными. Ниже ${critical}% запись может быть приостановлена для защиты диска.`
    : `Ниже ${warning}% система только предупреждает о нехватке места. Автоосвобождение выключено; ниже ${critical}% запись может быть приостановлена для защиты диска, но это не разрешает удаление без явного включения.`;
}

export function topReasonEntries(summary, limit = 5) {
  const counts = summary?.reason_counts || summary?.skipped_reason_counts || summary?.item_reason_counts || {};
  return Object.entries(counts)
    .filter(([, value]) => Number(value) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, limit);
}

const REVIEW_ONLY_RECONCILIATION_CLASSES = new Set(["orphan_file", "pre_metadata_km_vms_file", "legacy_archive_file"]);
const SAFE_METADATA_RECONCILIATION_CLASSES = new Set([
  "missing_file",
  "orphan_metadata",
  "zero_size_file",
  "partial_file",
  "corrupted_file",
  "stale_writing_segment",
  "invalid_path",
  "path_outside_storage",
  "unreadable_file",
]);

export function reconciliationClassLabel(code, labels = {}, language = "ru") {
  const known = {
    missing_file: { ru: "Записи без файлов", en: "Records without files", "zh-CN": "无文件的录像记录" },
    orphan_metadata: { ru: "Записи без файлов", en: "Metadata records without files", "zh-CN": "无文件的元数据记录" },
    orphan_file: { ru: "Файлы без метаданных", en: "Files without metadata", "zh-CN": "无元数据的文件" },
    pre_metadata_km_vms_file: { ru: "Файлы без метаданных", en: "Pre-metadata KM VMS files", "zh-CN": "无新元数据的 KM VMS 文件" },
    legacy_archive_file: { ru: "Старые архивные файлы", en: "Legacy archive files", "zh-CN": "旧归档文件" },
    invalid_path: { ru: "Некорректные пути", en: "Invalid paths", "zh-CN": "无效路径" },
    path_outside_storage: { ru: "Пути вне хранилища", en: "Paths outside storage", "zh-CN": "存储之外的路径" },
    zero_size_file: { ru: "Файлы нулевого размера", en: "Zero-size files", "zh-CN": "零大小文件" },
    partial_file: { ru: "Частичные файлы", en: "Partial files", "zh-CN": "部分文件" },
    corrupted_file: { ru: "Поврежденные файлы", en: "Corrupted files", "zh-CN": "损坏文件" },
    stale_writing_segment: { ru: "Зависшие записи", en: "Stale writing records", "zh-CN": "卡住的录像记录" },
    unreadable_file: { ru: "Файлы недоступны для чтения", en: "Unreadable files", "zh-CN": "无法读取的文件" },
    storage_unavailable: { ru: "Хранилище недоступно", en: "Storage unavailable", "zh-CN": "存储不可用" },
    root_unavailable: { ru: "Корень архива недоступен", en: "Archive root unavailable", "zh-CN": "归档根目录不可用" },
    active_root_not_writable: { ru: "Нет записи в активный корень", en: "Active archive root is not writable", "zh-CN": "活动归档根目录不可写" },
    root_unresolved: { ru: "Расположение записи не определено", en: "Recording location unresolved", "zh-CN": "无法确定录像位置" },
    skipped: { ru: "Пропущено", en: "Skipped", "zh-CN": "已跳过" },
  };
  const backendLabel = typeof labels?.[code] === "string" ? labels[code] : "";
  if (known[code]) return known[code][language] || known[code].ru;
  return backendLabel || statusLabel("unknown", language);
}

export function normalizeReconciliationSummary(source = {}, language = "ru") {
  const counts = {
    ...(source?.counts || {}),
    ...(source?.classification_counts || {}),
    ...(source?.category_counts || {}),
  };
  if (!Object.keys(counts).length) {
    if (source?.missing_file_count != null) counts.missing_file = Number(source.missing_file_count || 0);
    if (source?.root_unavailable_count != null) counts.root_unavailable = Number(source.root_unavailable_count || 0);
    if (source?.active_root_write_problem_count != null) counts.active_root_not_writable = Number(source.active_root_write_problem_count || 0);
    if (source?.root_unresolved_count != null) counts.root_unresolved = Number(source.root_unresolved_count || 0);
    if (source?.orphan_file_count != null) counts.orphan_file = Number(source.orphan_file_count || 0);
    if (source?.invalid_path_count != null) counts.invalid_path = Number(source.invalid_path_count || 0);
    if (source?.path_outside_storage_count != null) counts.path_outside_storage = Number(source.path_outside_storage_count || 0);
  }
  const labels = source?.classification_labels_ru || {};
  const cleanup = source?.cleanup_candidates || source?.cleanup_candidates_summary || {};
  const cleanupCounts = cleanup?.classification_counts || {};
  const reviewOnlyCount = Number(cleanup.count ?? source?.cleanup_candidate_count ?? source?.cleanup_candidates_count ?? 0);
  const safeReasonCounts = source?.apply_safe_summary?.reason_counts || {};
  const safeFixCount = Number(source?.apply_safe_summary?.updated_metadata_count ?? source?.updated_metadata_count ?? 0);
  const safeProblemCount = Object.entries(counts)
    .filter(([key]) => SAFE_METADATA_RECONCILIATION_CLASSES.has(key))
    .reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const manualProblemCount = Object.entries(counts)
    .filter(([key]) => REVIEW_ONLY_RECONCILIATION_CLASSES.has(key))
    .reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const problemCount = Object.entries(counts)
    .filter(([key]) => key !== "ok_owned_finalized" && key !== "skipped")
    .reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const categories = Object.entries({ ...counts, ...cleanupCounts, ...safeReasonCounts })
    .map(([code, value]) => ({ code, count: Number(value || 0), label: reconciliationClassLabel(code, labels, language) }))
    .filter((item) => item.count > 0 && item.code !== "ok_owned_finalized")
    .sort((a, b) => b.count - a.count)
    .slice(0, 6);
  return {
    status: source?.status || (problemCount ? "problems_found" : "ok"),
    problemCount,
    safeFixCount: safeFixCount || safeProblemCount,
    reviewOnlyCount,
    manualProblemCount,
    totalRows: Number(source?.total_metadata_rows_checked ?? source?.checked_count ?? 0),
    categories,
    counts,
    canApplySafe: Boolean((safeFixCount || safeProblemCount) > 0),
    hasReviewOnly: reviewOnlyCount > 0 || manualProblemCount > 0,
  };
}

const ARCHIVE_INTEGRITY_CATEGORY_KEYS = Object.freeze({
  missing_file: "integrityCategoryMissing",
  zero_size_file: "integrityCategoryZeroSize",
  corrupted_file: "integrityCategoryCorrupted",
  stale_writing_segment: "integrityCategoryStaleWriting",
  partial_file: "integrityCategoryActivePartial",
  orphan_file: "integrityCategoryProvenOrphan",
  pre_metadata_km_vms_file: "integrityCategoryLegacyUnproven",
  legacy_archive_file: "integrityCategoryLegacyUnproven",
  foreign_file: "integrityCategoryForeign",
  unknown_file: "integrityCategoryUnknownFile",
  invalid_path: "integrityCategoryInvalidPath",
  path_outside_storage: "integrityCategoryOutsideStorage",
  unreadable_file: "integrityCategoryUnreadable",
  storage_unavailable: "integrityCategoryStorageUnavailable",
  root_unresolved: "integrityCategoryRootUnresolved",
  probe_unavailable: "integrityCategoryProbeUnavailable",
  ownership_untrusted: "integrityCategoryOwnershipUntrusted",
});

const ARCHIVE_INTEGRITY_IMPACT_KEYS = Object.freeze({
  recording_unavailable: "integrityImpactRecordingUnavailable",
  recording_unplayable: "integrityImpactRecordingUnplayable",
  recording_in_progress: "integrityImpactRecordingInProgress",
  recording_incomplete: "integrityImpactRecordingIncomplete",
  unindexed_storage_usage: "integrityImpactUnindexedSpace",
  outside_product_ownership: "integrityImpactOutsideOwnership",
  ownership_unknown: "integrityImpactOwnershipUnknown",
  archive_root_unavailable: "integrityImpactRootUnavailable",
  recording_location_unknown: "integrityImpactLocationUnknown",
  integrity_not_fully_checked: "integrityImpactNotFullyChecked",
});

const ARCHIVE_INTEGRITY_ACTION_KEYS = Object.freeze({
  retire_missing_recording: "integrityActionRetireMissing",
  mark_stale_recording: "integrityActionMarkStale",
  delete_unusable_recording: "integrityActionDeleteUnusable",
  delete_proven_orphan: "integrityActionDeleteOrphan",
});

const ARCHIVE_INTEGRITY_NO_ACTION_KEYS = Object.freeze({
  orphan_observation_grace_required: "integrityNoActionOrphanGrace",
  wait_for_recording_completion: "integrityNoActionRecordingActive",
  incomplete_recording_review_required: "integrityNoActionIncompleteReview",
  legacy_file_review_required: "integrityNoActionLegacyReview",
  foreign_file_not_managed: "integrityNoActionForeign",
  unknown_file_review_required: "integrityNoActionOwnershipUnknown",
  contact_support_with_diagnostics: "integrityNoActionSupport",
  restore_archive_access: "integrityNoActionRestoreAccess",
  restore_archive_root_mapping: "integrityNoActionRestoreMapping",
  retry_integrity_check: "integrityNoActionRetryCheck",
});

const ARCHIVE_INTEGRITY_SCAN_STATUS_KEYS = Object.freeze({
  not_run: { titleKey: "integrityScanNotRunTitle", detailKey: "integrityScanNotRunText", tone: "unknown" },
  queued: { titleKey: "integrityScanQueuedTitle", detailKey: "integrityScanQueuedText", tone: "neutral" },
  running: { titleKey: "integrityScanRunningTitle", detailKey: "integrityScanRunningText", tone: "neutral" },
  cancel_requested: { titleKey: "integrityScanCancelRequestedTitle", detailKey: "integrityScanCancelRequestedText", tone: "warning" },
  completed: { titleKey: "integrityScanCompletedTitle", detailKey: "integrityScanCompletedText", tone: "ok" },
  partial: { titleKey: "integrityScanPartialTitle", detailKey: "integrityScanPartialText", tone: "warning" },
  failed: { titleKey: "integrityScanFailedTitle", detailKey: "integrityScanFailedText", tone: "error" },
  cancelled: { titleKey: "integrityScanCancelledTitle", detailKey: "integrityScanCancelledText", tone: "neutral" },
  interrupted: { titleKey: "integrityScanInterruptedTitle", detailKey: "integrityScanInterruptedText", tone: "warning" },
});

export function archiveIntegrityScanModel(scan = {}, permission = { allowed: true, reason: "" }) {
  const status = String(scan?.status || "not_run");
  const presentation = ARCHIVE_INTEGRITY_SCAN_STATUS_KEYS[status] || ARCHIVE_INTEGRITY_SCAN_STATUS_KEYS.failed;
  const progress = scan?.progress && typeof scan.progress === "object" ? scan.progress : {};
  const planned = Math.max(0, asNumber(progress.planned_count, 0));
  const checked = Math.max(0, asNumber(progress.checked_count, 0));
  const found = Math.max(0, asNumber(progress.found_count, 0));
  const failed = Math.max(0, asNumber(progress.failed_count, 0));
  const running = ["queued", "running", "cancel_requested", "interrupted"].includes(status);
  const stale = Boolean(scan?.stale);
  const terminal = ["completed", "partial", "failed", "cancelled"].includes(status);
  const operationCancelAllowed = scan?.operation?.cancel_allowed;
  return {
    status,
    titleKey: presentation.titleKey,
    detailKey: presentation.detailKey,
    tone: stale && status === "completed" ? "warning" : presentation.tone,
    running,
    terminal,
    stale,
    planned,
    checked,
    found,
    failed,
    percent: planned > 0 ? Math.max(0, Math.min(100, (checked / planned) * 100)) : 0,
    canStart: Boolean(permission.allowed && !running),
    canCancel: Boolean(permission.allowed && running && operationCancelAllowed !== false && status !== "cancel_requested"),
    permissionReason: permission.allowed ? "" : permission.reason,
    phaseKey: `integrityPhase${String(scan?.phase || "queued").replace(/(^|_)([a-z])/g, (_, _separator, letter) => letter.toUpperCase())}`,
  };
}

function safeIntegrityReference(value) {
  const normalized = String(value || "").replaceAll("\\", "/");
  const basename = normalized.split("/").filter(Boolean).at(-1) || "";
  return basename.slice(0, 160);
}

export function archiveIntegrityFindingPresentation(finding = {}) {
  const actionKey = String(finding?.action_key || "");
  const noActionReason = String(finding?.no_action_reason || "");
  const permissionDenied = finding?.action_allowed !== true && Boolean(finding?.required_permission) && !noActionReason;
  return {
    key: String(finding?.finding_id || ""),
    categoryKey: ARCHIVE_INTEGRITY_CATEGORY_KEYS[finding?.category] || "integrityCategoryUnknown",
    impactKey: ARCHIVE_INTEGRITY_IMPACT_KEYS[finding?.impact_key] || "integrityImpactUnknown",
    actionLabelKey: ARCHIVE_INTEGRITY_ACTION_KEYS[actionKey] || null,
    noActionLabelKey: permissionDenied
      ? "integrityNoActionPermission"
      : ARCHIVE_INTEGRITY_NO_ACTION_KEYS[noActionReason] || "integrityNoActionUnavailable",
    actionKey: actionKey || null,
    actionAllowed: finding?.action_allowed === true && Boolean(ARCHIVE_INTEGRITY_ACTION_KEYS[actionKey]),
    permissionDenied,
    destructive: String(finding?.confirmation_level || "").startsWith("destructive"),
    tone: finding?.severity === "error" ? "error" : finding?.severity === "warning" ? "warning" : "neutral",
    cameraName: String(finding?.camera_name || "").slice(0, 120),
    rootLabel: String(finding?.root_label || "").slice(0, 120),
    displayName: safeIntegrityReference(finding?.display_name),
    stale: finding?.stale === true || finding?.state !== "active",
  };
}

export function archiveIntegrityCategoryPresentations(counts = {}) {
  return Object.entries(counts && typeof counts === "object" ? counts : {})
    .map(([category, value]) => ({
      category,
      labelKey: ARCHIVE_INTEGRITY_CATEGORY_KEYS[category] || "integrityCategoryUnknown",
      count: Math.max(0, asNumber(value, 0)),
    }))
    .filter((item) => item.count > 0)
    .sort((left, right) => right.count - left.count || left.category.localeCompare(right.category))
    .slice(0, 16);
}

export function archiveIntegrityActionContract(actionKey) {
  const key = String(actionKey || "");
  if (key === "mark_stale_recording") {
    return { planKind: "metadata", confirmationKey: "integrityConfirmMarkStale", destructive: false };
  }
  if (key === "retire_missing_recording") {
    return { planKind: "deletion", confirmationKey: "integrityConfirmRetireMissing", destructive: true };
  }
  if (key === "delete_unusable_recording") {
    return { planKind: "deletion", confirmationKey: "integrityConfirmDeleteUnusable", destructive: true };
  }
  if (key === "delete_proven_orphan") {
    return { planKind: "deletion", confirmationKey: "integrityConfirmDeleteOrphan", destructive: true };
  }
  return { planKind: null, confirmationKey: "integrityNoActionUnavailable", destructive: false };
}

export const STORAGE_ACTION_PERMISSIONS = Object.freeze({
  openStorage: "manage_settings",
  autoFreeSpace: "manage_settings",
  archiveRootDiscovery: "manage_settings",
  archiveRootCreate: "manage_settings",
  archiveRootActivate: "manage_settings",
  migrationPreview: "manage_settings",
  migrationApply: "manage_settings",
  reconciliationSummary: "run_diagnostics",
  reconciliationApply: "manage_settings",
  retentionPreview: "delete_recordings",
  retentionApply: "delete_recordings",
  settingsRead: "manage_settings",
});

export function userHasPermission(user, permission) {
  return Array.isArray(user?.permissions) && user.permissions.includes(permission);
}

export function storagePermissionReason(permission, language = "ru") {
  const labels = {
    manage_settings: {
      ru: "Недостаточно прав для управления настройками хранилища.",
      en: "Not enough permissions to manage storage settings.",
      "zh-CN": "没有足够权限管理存储设置。",
    },
    delete_recordings: {
      ru: "Недостаточно прав для удаления записей.",
      en: "Not enough permissions to delete recordings.",
      "zh-CN": "没有足够权限删除录像。",
    },
    run_diagnostics: {
      ru: "Недостаточно прав для запуска диагностики.",
      en: "Not enough permissions to run diagnostics.",
      "zh-CN": "没有足够权限运行诊断。",
    },
  };
  return labels[permission]?.[language] || labels[permission]?.ru || labels.manage_settings.ru;
}

export function actionPermissionState(user, permission, language = "ru") {
  const allowed = userHasPermission(user, permission);
  return {
    allowed,
    permission,
    reason: allowed ? "" : storagePermissionReason(permission, language),
  };
}

function reasonCode(reason) {
  return typeof reason === "string" ? reason : reason?.reason_code || reason?.reason || reason?.code || reason?.error || "";
}

export function humanBlockerReason(reason, language = "ru") {
  const rawCode = reasonCode(reason);
  const aliases = {
    archive_root_identity_changed: "physical_volume_identity_changed",
    archive_root_not_readable: "archive_root_unavailable",
    archive_root_open_failed: "archive_root_unavailable",
    archive_root_physical_identity_unknown: "physical_volume_identity_unknown",
    archive_root_runtime_not_directory: "archive_root_unavailable",
    migration_final_checksum_mismatch: "migration_target_checksum_mismatch",
    migration_final_provenance_mismatch: "migration_target_collision",
    migration_final_provenance_unknown: "migration_target_collision",
    migration_manifest_item_tampered: "migration_plan_stale_or_tampered",
    migration_operation_identity_invalid: "migration_plan_stale_or_tampered",
    migration_plan_idempotency_mismatch: "migration_plan_stale_or_tampered",
    migration_quarantine_provenance_mismatch: "migration_source_cleanup_truth_unknown",
    migration_segment_missing: "migration_segment_changed_after_plan",
    migration_source_changed_after_copy: "migration_source_changed",
    migration_source_provenance_mismatch: "migration_source_changed",
    migration_source_short_read: "migration_source_changed",
    migration_target_residue_provenance_mismatch: "migration_target_collision",
    migration_target_residue_provenance_unknown: "migration_target_collision",
    migration_temp_pending_object_ambiguous: "migration_temp_collision",
    migration_temp_provenance_mismatch: "migration_temp_collision",
    migration_temp_provenance_unknown: "migration_temp_collision",
    migration_item_terminal_truth_incomplete: "migration_manifest_incomplete",
    migration_not_processed_after_partial: "migration_manifest_incomplete",
  };
  const code = aliases[rawCode] || rawCode;
  const labels = {
    auto_free_space_acknowledgement_required: {
      ru: "Перед включением подтвердите условия автоматического удаления старых записей.",
      en: "Review and confirm the automatic deletion terms before enabling auto-free-space.",
      "zh-CN": "启用自动释放空间前，请查看并确认自动删除条款。",
    },
    auto_free_space_acknowledgement_stale: {
      ru: "Условия автоосвобождения изменились. Ознакомьтесь с актуальной версией и подтвердите её.",
      en: "The auto-free-space terms have changed. Review and confirm the current version.",
      "zh-CN": "自动释放空间条款已更新。请查看并确认当前版本。",
    },
    auto_free_space_policy_not_effective: {
      ru: "Автоосвобождение не включено или его условия ещё не подтверждены.",
      en: "Auto-free-space is disabled or its terms have not been confirmed.",
      "zh-CN": "自动释放空间未启用，或其条款尚未确认。",
    },
    retention_size_unknown: {
      ru: "Размер части записей не подтверждён. Проверьте состояние архива, чтобы система могла точно применить лимит камеры.",
      en: "The size of some recordings is unknown. Check archive health so the camera quota can be enforced accurately.",
      "zh-CN": "部分录像大小尚未确认。请检查归档状态，以便系统准确执行摄像机配额。",
    },
    retention_no_safe_candidates: {
      ru: "Правило хранения нарушено, но сейчас нет записей, которые можно безопасно удалить. Откройте проблемы архива.",
      en: "A retention rule is exceeded, but no recording can currently be deleted safely. Review archive problems.",
      "zh-CN": "保留规则已超限，但当前没有可安全删除的录像。请查看归档问题。",
    },
    retention_no_progress: {
      ru: "Автоматическое применение правил не смогло удалить выбранные записи. Обновите состояние архива и проверьте найденные проблемы.",
      en: "Automatic retention could not delete the selected recordings. Refresh archive status and review detected problems.",
      "zh-CN": "自动保留策略无法删除所选录像。请刷新归档状态并检查发现的问题。",
    },
    retention_preempted_by_storage_pressure: {
      ru: "Применение правил хранения временно уступило освобождению критически занятого тома и продолжится автоматически.",
      en: "Retention temporarily yielded to cleanup of a pressured volume and will continue automatically.",
      "zh-CN": "保留处理暂时让位于空间紧张卷的清理，并会自动继续。",
    },
    automatic_retention_failed: {
      ru: "Автоматическое применение правил хранения завершилось ошибкой. Обновите состояние и повторите проверку.",
      en: "Automatic retention failed. Refresh storage status and retry the check.",
      "zh-CN": "自动保留处理失败。请刷新存储状态并重试检查。",
    },
    physical_volume_identity_unknown: {
      ru: "Не удалось однозначно определить физический том. Проверьте доступность корней архива и обновите состояние.",
      en: "The physical volume could not be identified reliably. Check archive-root access and refresh status.",
      "zh-CN": "无法可靠识别物理卷。请检查归档根目录访问并刷新状态。",
    },
    physical_volume_identity_changed: {
      ru: "Подключение тома изменилось после проверки. Обновите состояние хранилища перед повтором.",
      en: "The volume mapping changed after validation. Refresh storage status before retrying.",
      "zh-CN": "验证后卷映射发生变化。请刷新存储状态后重试。",
    },
    physical_volume_runtime_identity_changed: {
      ru: "Фактическое подключение тома изменилось. Автоосвобождение остановлено до новой проверки.",
      en: "The runtime volume mount changed. Auto-free-space stopped until the volume is checked again.",
      "zh-CN": "运行时卷挂载已更改。自动释放空间已停止，等待重新检查。",
    },
    physical_volume_runtime_identity_ambiguous: {
      ru: "Корни, указанные как один том, фактически подключены к разным устройствам. Проверьте расположения архива.",
      en: "Roots configured as one volume are mounted on different devices. Review archive locations.",
      "zh-CN": "配置为同一卷的归档根目录实际挂载在不同设备上。请检查归档位置。",
    },
    capacity_unknown: {
      ru: "Не удалось измерить свободное место на томе. Проверьте его подключение и права чтения.",
      en: "Free space on the volume could not be measured. Check its mount and read access.",
      "zh-CN": "无法测量卷的可用空间。请检查挂载和读取权限。",
    },
    auto_free_no_safe_candidates: {
      ru: "На заполненном томе нет записей, которые сейчас можно безопасно удалить. Откройте проблемы архива.",
      en: "The pressured volume has no recording that can currently be deleted safely. Review archive problems.",
      "zh-CN": "空间紧张的卷上当前没有可安全删除的录像。请查看归档问题。",
    },
    auto_free_no_progress: {
      ru: "Освободить место не удалось: выбранные записи не были удалены. Обновите состояние и проверьте проблемы архива.",
      en: "No space was recovered because the selected recordings were not deleted. Refresh status and review archive problems.",
      "zh-CN": "未能释放空间：所选录像未被删除。请刷新状态并检查归档问题。",
    },
    auto_free_retry_scheduled: {
      ru: "Повтор временно отложен, потому что состояние тома не изменилось. Система проверит его снова автоматически.",
      en: "Retry is temporarily delayed because the volume state has not changed. The system will check it again automatically.",
      "zh-CN": "由于卷状态未变化，重试暂时延后。系统会自动再次检查。",
    },
    archive_root_runtime_unavailable: {
      ru: "Один из корней тома сейчас недоступен. Восстановите подключение и обновите состояние хранилища.",
      en: "One of the volume roots is unavailable. Restore its mount and refresh storage status.",
      "zh-CN": "该卷的一个归档根目录当前不可用。请恢复挂载并刷新存储状态。",
    },
    storage_operation_interrupted: {
      ru: "Операция прервалась до подтверждённого завершения. Обновите состояние; система безопасно продолжит или предложит повтор.",
      en: "The operation stopped before terminal confirmation. Refresh status; the system will safely resume or offer a retry.",
      "zh-CN": "操作在确认完成前中断。请刷新状态；系统将安全继续或提供重试。",
    },
    storage_operation_lease_lost: {
      ru: "Операция потеряла право на продолжение и была безопасно остановлена. Обновите состояние перед повтором.",
      en: "The operation lost its execution lease and stopped safely. Refresh status before retrying.",
      "zh-CN": "操作失去执行租约并已安全停止。重试前请刷新状态。",
    },
    storage_operation_conflict: {
      ru: "С этим архивом уже выполняется несовместимая операция. Дождитесь её завершения и обновите состояние.",
      en: "A conflicting archive operation is already running. Wait for it to finish and refresh status.",
      "zh-CN": "正在执行冲突的归档操作。请等待其完成并刷新状态。",
    },
    auto_free_root_set_changed: {
      ru: "Состав корней тома изменился во время операции. Автоосвобождение остановлено; обновите состояние хранилища.",
      en: "The volume root set changed during cleanup. Auto-free-space stopped; refresh storage status.",
      "zh-CN": "清理期间卷的归档根目录集合发生变化。自动释放空间已停止；请刷新存储状态。",
    },
    root_set_changed: {
      ru: "Состав корней архива изменился после проверки. Обновите состояние перед повтором.",
      en: "The archive root set changed after validation. Refresh status before retrying.",
      "zh-CN": "验证后归档根目录集合发生变化。重试前请刷新状态。",
    },
    automatic_auto_free_failed: {
      ru: "Автоматическое освобождение места завершилось ошибкой. Обновите состояние и проверьте доступность тома.",
      en: "Automatic space cleanup failed. Refresh status and check volume access.",
      "zh-CN": "自动空间清理失败。请刷新状态并检查卷访问。",
    },
    operation_terminal_persistence_failed: {
      ru: "Не удалось подтвердить итог операции. Не повторяйте действие до обновления состояния.",
      en: "The terminal operation result could not be confirmed. Do not retry until status is refreshed.",
      "zh-CN": "无法确认操作最终结果。刷新状态前请勿重试。",
    },
    active_recording_jobs: {
      ru: "Перенос заблокирован: сейчас идет запись. Остановите запись на всех камерах или выполните перенос позже.",
      en: "Migration is blocked because recording is active. Stop recording on all cameras or run migration later.",
      "zh-CN": "迁移已阻止：当前正在录像。请停止所有摄像机录像或稍后再迁移。",
    },
    active_recording_jobs_block_root_switch: {
      ru: "Переключение корня архива заблокировано активной записью. Остановите запись или дождитесь завершения текущей операции.",
      en: "Archive root switch is blocked by active recording. Stop recording or wait for the current operation to finish.",
      "zh-CN": "活动录像阻止切换归档根目录。请停止录像或等待当前操作完成。",
    },
    archive_root_not_writable_or_namespace_missing: {
      ru: "Корень архива не готов: нет записи или пространства kmvms/recordings. Проверьте путь и права NAS.",
      en: "Archive root is not ready: write access or kmvms/recordings namespace is missing. Check NAS path and permissions.",
      "zh-CN": "归档根目录未就绪：缺少写入权限或 kmvms/recordings 命名空间。请检查 NAS 路径和权限。",
    },
    archive_migration_apply_requires_confirm: {
      ru: "Применение переноса требует явного подтверждения.",
      en: "Migration apply requires explicit confirmation.",
      "zh-CN": "应用迁移需要明确确认。",
    },
    recording_format_change_blocked_active_recordings: {
      ru: "Изменение формата записи заблокировано активной записью.",
      en: "Recording format change is blocked by active recording.",
      "zh-CN": "活动录像阻止更改录像格式。",
    },
    missing_file: {
      ru: "Файл отсутствует",
      en: "Missing file",
      "zh-CN": "文件缺失",
    },
    orphan_file: {
      ru: "Файл без метаданных KM VMS",
      en: "File without KM VMS metadata",
      "zh-CN": "文件没有 KM VMS 元数据",
    },
    invalid_path: {
      ru: "Некорректный путь",
      en: "Invalid path",
      "zh-CN": "路径无效",
    },
    path_outside_storage: {
      ru: "Путь вне безопасного хранилища",
      en: "Path outside safe storage",
      "zh-CN": "路径在安全存储之外",
    },
    namespace_missing: {
      ru: "Не создана папка пространства KM VMS: kmvms/recordings",
      en: "KM VMS namespace folder is missing: kmvms/recordings",
      "zh-CN": "缺少 KM VMS 命名空间文件夹：kmvms/recordings",
    },
    archive_root_namespace_mismatch: {
      ru: "Корень архива относится к другому пространству хранения",
      en: "Archive root belongs to a different storage namespace",
      "zh-CN": "归档根目录属于不同的存储命名空间",
    },
    archive_root_unavailable: {
      ru: "Корень архива недоступен",
      en: "Archive root is unavailable",
      "zh-CN": "归档根目录不可用",
    },
    archive_root_activation_in_progress: {
      ru: "Переключение расположения архива уже выполняется. Дождитесь его завершения.",
      en: "An archive location switch is already running. Wait for it to finish.",
      "zh-CN": "归档位置切换正在进行。请等待其完成。",
    },
    archive_root_mutation_in_progress: {
      ru: "Сейчас выполняется другая операция с расположением архива. Дождитесь её завершения.",
      en: "Another archive-location operation is running. Wait for it to finish.",
      "zh-CN": "另一个归档位置操作正在进行。请等待其完成。",
    },
    archive_root_recovery_required: {
      ru: "Предыдущее переключение не завершилось безопасно. Выполните восстановление предыдущего расположения.",
      en: "The previous switch did not finish safely. Recover the previous archive location.",
      "zh-CN": "上次切换未安全完成。请恢复之前的归档位置。",
    },
    archive_root_retired: {
      ru: "Это расположение архива уже удалено и недоступно для переключения.",
      en: "This archive location has been removed and cannot be activated.",
      "zh-CN": "此归档位置已删除，无法激活。",
    },
    storage_candidate_identity_unavailable: {
      ru: "Не удалось подтвердить физический том. Обновите список томов и повторите действие.",
      en: "The physical volume could not be verified. Refresh the volume list and retry.",
      "zh-CN": "无法确认物理卷。请刷新卷列表后重试。",
    },
    archive_root_not_writable: {
      ru: "Корень архива недоступен для записи",
      en: "Archive root is not writable",
      "zh-CN": "归档根目录不可写",
    },
    archive_root_overlap: {
      ru: "Корни архива пересекаются. Выберите отдельную папку архива.",
      en: "Archive roots overlap. Choose a separate archive folder.",
      "zh-CN": "归档根目录重叠。请选择独立的归档文件夹。",
    },
    archive_root_missing: {
      ru: "Корень архива не найден",
      en: "Archive root is missing",
      "zh-CN": "找不到归档根目录",
    },
    archive_root_outside_approved_storage_base: {
      ru: "Путь корня архива вне разрешенной области хранения.",
      en: "Archive root path is outside the approved storage base.",
      "zh-CN": "归档根路径位于允许的存储范围之外。",
    },
    target_root_missing: {
      ru: "Целевой корень архива не выбран или недоступен.",
      en: "Target archive root is missing or unavailable.",
      "zh-CN": "目标归档根目录缺失或不可用。",
    },
    path_outside_archive_root: {
      ru: "Путь записи вне корня архива. Нужна ручная проверка метаданных.",
      en: "Recording path is outside the archive root. Manual metadata review is required.",
      "zh-CN": "录像路径位于归档根目录之外。需要手动检查元数据。",
    },
    root_missing: {
      ru: "Путь архива не найден",
      en: "Archive path is missing",
      "zh-CN": "找不到归档路径",
    },
    root_not_directory: {
      ru: "Путь архива не является папкой",
      en: "Archive path is not a directory",
      "zh-CN": "归档路径不是目录",
    },
    root_not_readable: {
      ru: "Путь архива недоступен для чтения",
      en: "Archive path is not readable",
      "zh-CN": "归档路径不可读",
    },
    storage_root_missing: {
      ru: "Корень хранилища отсутствует. Проверьте подключение и путь NAS.",
      en: "Storage root is missing. Check NAS mount and path.",
      "zh-CN": "存储根目录缺失。请检查 NAS 挂载和路径。",
    },
    storage_root_not_directory: {
      ru: "Корень хранилища не является папкой.",
      en: "Storage root is not a directory.",
      "zh-CN": "存储根目录不是文件夹。",
    },
    storage_root_not_readable: {
      ru: "Корень хранилища недоступен для чтения.",
      en: "Storage root is not readable.",
      "zh-CN": "存储根目录不可读。",
    },
    storage_root_not_writable: {
      ru: "Корень хранилища недоступен для записи.",
      en: "Storage root is not writable.",
      "zh-CN": "存储根目录不可写。",
    },
    insufficient_target_free_space: {
      ru: "На целевом корне недостаточно свободного места для переноса.",
      en: "Target root does not have enough free space for migration.",
      "zh-CN": "目标根目录没有足够可用空间执行迁移。",
    },
    stale_or_tampered_plan: {
      ru: "План переноса устарел или изменен. Обновите проверку и повторите подтверждение.",
      en: "Migration plan is stale or changed. Refresh the check and confirm again.",
      "zh-CN": "迁移计划已过期或已更改。请刷新检查并重新确认。",
    },
    source_file_changed_after_plan: {
      ru: "Исходный файл изменился после предпросмотра. Обновите проверку перед повтором.",
      en: "Source file changed after preview. Refresh the check before retrying.",
      "zh-CN": "源文件在预览后发生变化。重试前请刷新检查。",
    },
    source_changed_during_copy: {
      ru: "Исходный файл изменился во время копирования. Исходник сохранен, нужна ручная проверка.",
      en: "Source file changed during copy. Source is preserved; manual review is required.",
      "zh-CN": "源文件在复制期间发生变化。源文件已保留，需要手动检查。",
    },
    target_collision: {
      ru: "В целевом корне уже есть файл с таким путем. Нужна ручная проверка.",
      en: "Target root already has a file at that path. Manual review is required.",
      "zh-CN": "目标根目录中已有同路径文件。需要手动检查。",
    },
    temp_target_collision: {
      ru: "Не удалось создать временный файл переноса. Повторите после проверки целевого корня.",
      en: "Could not create migration temporary file. Retry after checking the target root.",
      "zh-CN": "无法创建迁移临时文件。检查目标根后重试。",
    },
    plan_item_missing_after_validation: {
      ru: "Элемент плана исчез после проверки. Обновите проверку и повторите.",
      en: "Plan item disappeared after validation. Refresh the check and retry.",
      "zh-CN": "计划项在验证后消失。请刷新检查并重试。",
    },
    migration_source_target_required: {
      ru: "Выберите исходное и целевое расположения архива.",
      en: "Select both the source and target archive locations.",
      "zh-CN": "请选择源归档位置和目标归档位置。",
    },
    migration_source_equals_target: {
      ru: "Исходное и целевое расположения должны отличаться.",
      en: "Source and target archive locations must be different.",
      "zh-CN": "源归档位置和目标归档位置必须不同。",
    },
    migration_plan_not_ready: {
      ru: "План переноса ещё не готов. Дождитесь завершения проверки.",
      en: "The migration plan is not ready yet. Wait for validation to finish.",
      "zh-CN": "迁移计划尚未就绪。请等待验证完成。",
    },
    migration_plan_not_found: {
      ru: "План переноса не найден или больше недоступен. Обновите состояние и подготовьте новый план.",
      en: "The migration plan was not found or is no longer available. Refresh status and prepare a new plan.",
      "zh-CN": "找不到迁移计划或该计划已不可用。请刷新状态并准备新计划。",
    },
    migration_operation_not_found: {
      ru: "Операция переноса не найдена. Обновите состояние, чтобы получить её актуальный результат.",
      en: "The migration operation was not found. Refresh status to retrieve its current result.",
      "zh-CN": "找不到迁移操作。请刷新状态以获取当前结果。",
    },
    migration_plan_expired: {
      ru: "План переноса устарел. Подготовьте новый план.",
      en: "The migration plan has expired. Prepare a new plan.",
      "zh-CN": "迁移计划已过期。请准备新计划。",
    },
    migration_plan_stale_or_tampered: {
      ru: "План больше не соответствует подтверждённому состоянию. Подготовьте его заново.",
      en: "The plan no longer matches the confirmed state. Prepare it again.",
      "zh-CN": "计划不再与已确认状态匹配。请重新准备计划。",
    },
    migration_insufficient_target_space: {
      ru: "На целевом томе недостаточно свободного места с учётом безопасного резерва.",
      en: "The target volume lacks free space after the required safety reserve.",
      "zh-CN": "目标卷在保留所需安全空间后可用空间不足。",
    },
    migration_no_eligible_recordings: {
      ru: "В выбранном исходном расположении нет завершённых записей, которые сейчас можно безопасно перенести.",
      en: "The selected source has no finalized recordings that can be moved safely now.",
      "zh-CN": "所选源位置中当前没有可安全移动的已完成录像。",
    },
    migration_permission_required: {
      ru: "Для запуска переноса нужны права управления хранилищем и удаления записей.",
      en: "Starting a migration requires both storage-management and recording-deletion permissions.",
      "zh-CN": "启动迁移需要存储管理权限和录像删除权限。",
    },
    migration_plan_actor_mismatch: {
      ru: "Этот план подготовлен другим пользователем. Первоначальный перенос может запустить только автор плана; администратор может принять только явно доступное завершение очистки.",
      en: "This plan was prepared by another user. Only its author can start the initial migration; an administrator may take over only an explicitly available cleanup recovery.",
      "zh-CN": "此计划由其他用户准备。只有计划创建者可以启动首次迁移；管理员只能接管明确可用的清理恢复。",
    },
    migration_cleanup_takeover_not_allowed: {
      ru: "Административное завершение сейчас недоступно: нет подтверждённой незавершённой очистки или операция уже изменилась. Обновите состояние.",
      en: "Administrative cleanup is not available because no exact pending cleanup is proven or the operation has changed. Refresh the state.",
      "zh-CN": "管理员清理当前不可用：未确认存在准确的待清理工作，或操作状态已更改。请刷新状态。",
    },
    migration_cleanup_takeover_requires_confirm: {
      ru: "Административное завершение очистки требует явного подтверждения.",
      en: "Administrative cleanup recovery requires explicit confirmation.",
      "zh-CN": "管理员清理恢复需要明确确认。",
    },
    migration_recovery_permission_revoked: {
      ru: "Администратор потерял необходимые права во время очистки. Изменение остановлено до восстановления прав.",
      en: "The recovery administrator lost required permissions during cleanup. Mutation stopped until permissions are restored.",
      "zh-CN": "恢复管理员在清理期间失去所需权限。更改已停止，等待权限恢复。",
    },
    migration_plan_preparation_failed: {
      ru: "Не удалось безопасно подготовить план переноса. Обновите состояние хранилища и повторите подготовку.",
      en: "The migration plan could not be prepared safely. Refresh storage status and prepare it again.",
      "zh-CN": "无法安全准备迁移计划。请刷新存储状态后重新准备。",
    },
    migration_root_revalidation_failed: {
      ru: "Расположение архива изменилось или стало недоступно после подготовки плана. Обновите состояние и подготовьте новый план.",
      en: "An archive location changed or became unavailable after plan preparation. Refresh status and prepare a new plan.",
      "zh-CN": "准备计划后归档位置已更改或不可用。请刷新状态并准备新计划。",
    },
    migration_temp_missing: {
      ru: "Подтверждённый временный файл переноса отсутствует. Система остановилась без повторного копирования; обновите состояние и используйте предложенное действие.",
      en: "The proven migration temporary file is missing. The system stopped without recopying; refresh status and use the offered action.",
      "zh-CN": "已确认的迁移临时文件缺失。系统已停止且未重复复制；请刷新状态并执行建议操作。",
    },
    migration_operation_failed: {
      ru: "Операция переноса остановилась без раскрытия внутренних технических данных. Обновите состояние и используйте предложенное безопасное действие.",
      en: "Migration stopped without exposing internal technical details. Refresh status and use the offered safe action.",
      "zh-CN": "迁移已停止，未公开内部技术信息。请刷新状态并执行建议的安全操作。",
    },
    migration_permission_revoked: {
      ru: "Во время переноса необходимые права были отозваны. Уже завершённые файлы не откатывались.",
      en: "Required permissions were revoked during migration. Completed files were not rolled back.",
      "zh-CN": "迁移期间所需权限被撤销。已完成的文件不会回滚。",
    },
    migration_segment_became_active: {
      ru: "Одна из записей снова используется камерой. Подготовьте новый план после завершения записи.",
      en: "A recording became active again. Prepare a new plan after recording finishes.",
      "zh-CN": "某个录像再次处于活动状态。请在录像结束后准备新计划。",
    },
    migration_segment_changed_after_plan: {
      ru: "Данные записи изменились после подготовки плана. Подготовьте новый план.",
      en: "Recording data changed after the plan was prepared. Prepare a new plan.",
      "zh-CN": "计划准备后录像数据发生变化。请准备新计划。",
    },
    migration_source_changed: {
      ru: "Исходный файл изменился после подготовки плана. Он не был удалён.",
      en: "A source file changed after the plan was prepared. It was not deleted.",
      "zh-CN": "计划准备后源文件发生变化。该文件未被删除。",
    },
    migration_source_checksum_changed: {
      ru: "Контрольная сумма исходного файла изменилась. Файл оставлен на месте для безопасной проверки.",
      en: "A source checksum changed. The file was left in place for safe review.",
      "zh-CN": "源文件校验和发生变化。文件保留在原处以便安全检查。",
    },
    migration_target_collision: {
      ru: "В целевом расположении найден чужой или неподтверждённый файл с тем же именем. Система его не изменяла.",
      en: "A foreign or unverified file already occupies the target name. KM VMS did not modify it.",
      "zh-CN": "目标名称已被外部或未验证文件占用。KM VMS 未修改该文件。",
    },
    migration_temp_collision: {
      ru: "В служебном месте переноса обнаружен файл без подтверждённой принадлежности этой операции. Он не изменён.",
      en: "The migration workspace contains an object not proven to belong to this operation. It was not modified.",
      "zh-CN": "迁移工作区中存在无法证明属于本次操作的对象。该对象未被修改。",
    },
    migration_target_checksum_mismatch: {
      ru: "Проверка целевой копии не совпала с исходником. Исходный файл сохранён.",
      en: "Target verification did not match the source. The source file was preserved.",
      "zh-CN": "目标文件验证结果与源文件不一致。源文件已保留。",
    },
    migration_final_target_missing: {
      ru: "Подтверждённый целевой файл исчез. Переключение или очистка остановлены; восстановите доступ к целевому расположению и обновите состояние.",
      en: "A proven target file disappeared. Switching or cleanup stopped; restore target access and refresh status.",
      "zh-CN": "已确认的目标文件消失。切换或清理已停止；请恢复目标位置访问并刷新状态。",
    },
    migration_final_not_readable: {
      ru: "Целевой файл создан, но не прошёл проверку обычного чтения. Исходный файл не удалён.",
      en: "The target file was created but failed the normal read check. The source was not deleted.",
      "zh-CN": "目标文件已创建，但未通过正常读取检查。源文件未被删除。",
    },
    migration_metadata_changed_before_switch: {
      ru: "Карточка записи изменилась до переключения на новый файл. Система остановилась без удаления исходника.",
      en: "Recording metadata changed before placement could switch. The operation stopped without deleting the source.",
      "zh-CN": "切换到新文件前录像元数据发生变化。操作已停止，源文件未删除。",
    },
    migration_source_cleanup_incomplete: {
      ru: "Запись уже переключена на целевой файл, но удаление исходной копии не завершено. Доступен безопасный повтор очистки.",
      en: "The recording already uses the target file, but source cleanup is incomplete. A safe cleanup retry is available.",
      "zh-CN": "录像已切换到目标文件，但源文件清理尚未完成。可以安全重试清理。",
    },
    migration_source_cleanup_truth_unknown: {
      ru: "Система не смогла доказать итог удаления исходной копии. Не запускайте новый перенос до безопасного повтора очистки.",
      en: "The source-cleanup result could not be proven. Do not start another migration until cleanup is safely retried.",
      "zh-CN": "无法证明源文件清理结果。在安全重试清理前，请勿启动新的迁移。",
    },
    migration_manifest_incomplete: {
      ru: "Не все элементы подтверждённого плана завершены. Итог сохранён как частичный.",
      en: "Not every item in the confirmed plan completed. The result was stored as partial.",
      "zh-CN": "已确认计划中的部分项目未完成。结果已保存为部分完成。",
    },
    migration_cancel_cleanup_pending: {
      ru: "Отмена принята, но сначала система должна безопасно завершить очистку уже обработанного файла.",
      en: "Cancellation was accepted, but cleanup of an already processed file must finish safely first.",
      "zh-CN": "取消请求已接受，但必须先安全完成已处理文件的清理。",
    },
    migration_filesystem_failure: {
      ru: "Операция с файлами завершилась ошибкой. Уже подтверждённые изменения сохранены; доступный повтор указан ниже.",
      en: "A filesystem operation failed. Proven completed changes were kept; the available retry is shown below.",
      "zh-CN": "文件系统操作失败。已确认完成的更改已保留；下方显示可用的重试方式。",
    },
    migration_worker_failure: {
      ru: "Фоновый перенос остановился с ошибкой. Обновите состояние и используйте предложенный безопасный повтор.",
      en: "The background migration stopped with an error. Refresh status and use the offered safe retry.",
      "zh-CN": "后台迁移因错误停止。请刷新状态并使用提供的安全重试。",
    },
    migration_retry_not_allowed: {
      ru: "Для этого результата безопасный повтор недоступен. Обновите состояние и подготовьте новый план.",
      en: "A safe retry is not available for this result. Refresh status and prepare a new plan.",
      "zh-CN": "此结果无法安全重试。请刷新状态并准备新计划。",
    },
    permission_denied: {
      ru: "Недостаточно прав для действия.",
      en: "Not enough permissions for this action.",
      "zh-CN": "没有足够权限执行此操作。",
    },
    unavailable: {
      ru: "Состояние недоступно. Обновите проверку или откройте диагностику.",
      en: "State is unavailable. Refresh the check or open diagnostics.",
      "zh-CN": "状态不可用。请刷新检查或打开诊断。",
    },
  };
  return labels[code]?.[language] || labels[code]?.ru || statusLabel("unknown", language);
}

export function storageTopHealthModel({ operations = {}, pathHealth = {}, capacity = {}, policy = {}, reconciliation = {}, retention = {} } = {}, language = "ru") {
  const labels = {
    ru: {
      unavailable: "Корень архива недоступен. Проверьте подключение NAS, путь и права доступа.",
      unwritable: "Проверьте права записи в архив.",
      unreadable: "Проверьте права чтения архива.",
      ambiguousAvailability: "Корень архива требует проверки: чтение и запись доступны, но общая проверка архива не подтверждена. Проверьте путь, служебную папку архива и права.",
      lowDisk: "Освободите место или проверьте регламент хранения.",
      integrity: "Запустите проверку целостности и разберите найденные проблемы.",
      retention: "Проверьте правила хранения камер и причины, по которым автоматическое применение заблокировано.",
      migration: "Перенос заблокирован активной записью; выполните его позже.",
      interruptedOperation: "Предыдущая операция с архивом прервалась. Обновите состояние и повторяйте действие только после проверки результата.",
      stale: "Обновите состояние хранилища перед действиями.",
      unknown: "Дождитесь проверки состояния: не хватает фактов для безопасного действия.",
      ok: "Немедленных действий не требуется.",
    },
    en: {
      unavailable: "Archive root is unavailable. Check NAS mount, path, and permissions.",
      unwritable: "Check archive write permissions.",
      unreadable: "Check archive read permissions.",
      ambiguousAvailability: "Archive root needs verification: read and write checks pass, but the overall archive check is not confirmed. Check the path, archive service folder, and permissions.",
      lowDisk: "Free space or review retention policy.",
      integrity: "Run integrity check and review detected problems.",
      retention: "Check camera retention rules and the reason automatic enforcement is blocked.",
      migration: "Migration is blocked by active recording; run it later.",
      interruptedOperation: "A previous archive operation was interrupted. Refresh the state and retry only after checking its result.",
      stale: "Refresh storage state before actions.",
      unknown: "Wait for storage checks: facts are incomplete.",
      ok: "No immediate action is required.",
    },
    "zh-CN": {
      unavailable: "归档根目录不可用。请检查 NAS 挂载、路径和权限。",
      unwritable: "请检查归档写入权限。",
      unreadable: "请检查归档读取权限。",
      ambiguousAvailability: "归档根目录需要验证：读写检查通过，但整体归档检查未确认。请检查路径、归档服务文件夹和权限。",
      lowDisk: "请释放空间或检查保留策略。",
      integrity: "请运行完整性检查并处理发现的问题。",
      retention: "请检查摄像机保留规则以及自动应用被阻止的原因。",
      migration: "迁移被活动录像阻止；请稍后执行。",
      interruptedOperation: "上一个归档操作已中断。请刷新状态并确认结果后再重试。",
      stale: "操作前请刷新存储状态。",
      unknown: "等待存储检查完成：安全操作所需事实不完整。",
      ok: "当前无需立即操作。",
    },
  };
  const text = labels[language] || labels.ru;
  let status = "ok";
  let tone = "ok";
  let reason = text.ok;
  let nextStep = text.ok;
  if (pathHealth?.available == null || pathHealth?.readable == null || pathHealth?.writable == null) {
    status = "unknown";
    tone = "unknown";
    reason = text.unknown;
    nextStep = text.unknown;
  } else if (pathHealth?.readable === true && pathHealth?.writable === true && pathHealth?.available === false) {
    status = "availability_unconfirmed";
    tone = "warning";
    reason = text.ambiguousAvailability;
    nextStep = text.ambiguousAvailability;
  } else if (operations?.status === "unavailable" || pathHealth?.available === false) {
    status = "unavailable";
    tone = "error";
    reason = text.unavailable;
    nextStep = text.unavailable;
  } else if (pathHealth?.readable === false) {
    status = "unreadable";
    tone = "error";
    reason = text.unreadable;
    nextStep = text.unreadable;
  } else if (pathHealth?.writable === false) {
    status = "unwritable";
    tone = "error";
    reason = text.unwritable;
    nextStep = text.unwritable;
  } else if (policy?.recording_suspended_by_low_disk || policy?.state === "critical" || policy?.state === "cleanup_threshold" || Number(capacity?.free_percent) <= Number(policy?.warning_threshold_percent ?? 10)) {
    status = "low_disk";
    tone = policy?.state === "critical" || policy?.recording_suspended_by_low_disk ? "error" : "warning";
    reason = text.lowDisk;
    nextStep = text.lowDisk;
  } else if (Array.isArray(operations?.interrupted_operations) && operations.interrupted_operations.length > 0) {
    status = "operation_interrupted";
    tone = "warning";
    reason = text.interruptedOperation;
    nextStep = text.interruptedOperation;
  } else if (Number(reconciliation?.problem_file_count || 0) > 0 || Number(reconciliation?.cleanup_candidate_count || 0) > 0) {
    status = "reconciliation";
    tone = "warning";
    reason = text.integrity;
    nextStep = text.integrity;
  } else if (["missing", "unknown", "not_checked", "stale", "metadata_only"].includes(String(reconciliation?.evidence_status || "missing"))) {
    status = "unknown";
    tone = "unknown";
    reason = text.unknown;
    nextStep = text.unknown;
  } else if (retention?.last_status === "failed") {
    status = "retention_failed";
    tone = "warning";
    reason = text.retention;
    nextStep = text.retention;
  } else if (operations?.stale || operations?.status === "stale") {
    status = "stale";
    tone = "unknown";
    reason = text.stale;
    nextStep = text.stale;
  } else if (!operations?.status || !capacity?.total_bytes) {
    status = "unknown";
    tone = "unknown";
    reason = text.unknown;
    nextStep = text.unknown;
  }
  return { status, tone, reason, nextStep };
}

export function primaryStorageActionText(args = {}, language = "ru") {
  return storageTopHealthModel(args, language).nextStep;
}

export function retentionScenarioModel({ preview = null, result = null, retention = {}, permission = { allowed: true }, running = false } = {}, language = "ru") {
  const source = preview || result || retention?.last_summary || null;
  const count = Number(source?.planned_count ?? source?.deleted_count ?? 0);
  const bytes = Number(source?.estimated_freed_bytes ?? source?.bytes_freed ?? 0);
  const durableState = String(retention?.state || retention?.last_status || "idle").toLowerCase();
  const failed = ["failed", "blocked", "partial", "interrupted", "no_safe_candidate", "no_progress"].includes(durableState);
  return {
    status: !permission.allowed
      ? "unavailable_due_to_permissions"
      : running || durableState === "running"
      ? "running"
      : durableState === "pending"
      ? "pending"
      : result
      ? "apply_completed"
      : preview
      ? "preview_completed"
      : failed
      ? "apply_failed"
      : "idle",
    permissionReason: permission.allowed ? "" : permission.reason,
    hasPreview: Boolean(preview),
    canPreview: Boolean(permission.allowed && !running),
    canApply: Boolean(permission.allowed && preview && count > 0 && !running),
    plannedCount: count,
    plannedBytes: bytes,
    failedReason: failed ? humanBlockerReason(retention?.last_error || "unavailable", language) : "",
  };
}

export function reconciliationScenarioModel({ preview = null, result = null, reconciliation = {}, canCheck = { allowed: true }, canApply = { allowed: true }, running = false } = {}, language = "ru") {
  const source = preview || result || reconciliation || {};
  const normalized = normalizeReconciliationSummary(source, language);
  return {
    status: !canCheck.allowed ? "unavailable_due_to_permissions" : running ? "running" : result ? "apply_completed" : preview ? "preview_completed" : normalized.problemCount || normalized.reviewOnlyCount ? "check_needed" : "idle",
    checkPermissionReason: canCheck.allowed ? "" : canCheck.reason,
    applyPermissionReason: canApply.allowed ? "" : canApply.reason,
    canCheck: Boolean(canCheck.allowed && !running),
    canApply: Boolean(canApply.allowed && preview && normalized.canApplySafe && !running),
    problemCount: normalized.problemCount,
    cleanupCount: normalized.reviewOnlyCount,
    safeFixCount: normalized.safeFixCount,
    manualProblemCount: normalized.manualProblemCount,
    categories: normalized.categories,
    totalRows: normalized.totalRows,
    noAutoFixReason: preview && !normalized.canApplySafe && normalized.problemCount ? "review_only" : "",
    scope: "metadata_status_safe_repair",
    failedReason: result?.status === "failed" ? humanBlockerReason(result?.reason || "unavailable", language) : "",
  };
}

const MIGRATION_ACTIVE_STATUSES = new Set(["building", "queued", "running", "cancel_requested", "interrupted"]);
const MIGRATION_TERMINAL_STATUSES = new Set(["completed", "partial", "failed", "blocked", "cancelled", "expired"]);

export function migrationScenarioModel({
  plan = null,
  operation = null,
  preview = null,
  result = null,
  permission = { allowed: true, reason: "" },
  preparePermission = null,
  applyPermission = null,
  running = false,
} = {}, language = "ru") {
  const currentPlan = plan || preview || result?.plan || {};
  const currentOperation = operation || result?.operation || null;
  const prepareGate = preparePermission || permission;
  const applyGate = applyPermission || permission;
  const operationStatus = String(currentOperation?.status || "");
  const planStatus = String(currentPlan?.status || "");
  let status = operationStatus || planStatus || (running ? "running" : "idle");
  if (!MIGRATION_ACTIVE_STATUSES.has(status) && !MIGRATION_TERMINAL_STATUSES.has(status) && !["ready", "ready_with_exclusions", "idle"].includes(status)) {
    status = status ? "unknown" : "idle";
  }

  const progress = currentOperation?.progress || {};
  const itemCount = Number(currentPlan?.item_count ?? progress?.item_count);
  const completedCount = Number(currentPlan?.completed_count ?? progress?.completed_count);
  const totalBytes = Number(currentPlan?.total_bytes ?? progress?.total_bytes);
  const completedBytes = Number(currentPlan?.completed_bytes ?? progress?.completed_bytes);
  const currentItemBytes = Number(progress?.current_item_bytes);
  const countsKnown = Number.isFinite(itemCount) && Number.isFinite(completedCount);
  const bytesKnown = Number.isFinite(totalBytes) && Number.isFinite(completedBytes);
  const cleanupPending = currentPlan?.cleanup_pending === true;
  const completedProof = status === "completed"
    && !cleanupPending
    && countsKnown
    && completedCount === itemCount;
  let percent = null;
  if (completedProof) {
    percent = 100;
  } else if (bytesKnown && totalBytes > 0) {
    const transferred = completedBytes + (Number.isFinite(currentItemBytes) ? Math.max(0, currentItemBytes) : 0);
    percent = Math.min(99, Math.max(0, Math.floor((transferred / totalBytes) * 100)));
  } else if (countsKnown && itemCount > 0) {
    percent = Math.min(99, Math.max(0, Math.floor((completedCount / itemCount) * 100)));
  }

  const phase = String(progress?.phase || currentPlan?.phase || status || "unknown");
  const optionalBoundedNumber = (value, { minimum = 0, maximum = Number.MAX_SAFE_INTEGER } = {}) => {
    if (value === null || value === undefined || value === "") return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) && numeric >= minimum && numeric <= maximum ? numeric : null;
  };
  const speedBytesPerSecond = phase === "copying"
    ? optionalBoundedNumber(progress?.speed_bytes_per_second, { minimum: 1 })
    : null;
  const etaSeconds = phase === "copying"
    ? optionalBoundedNumber(progress?.eta_seconds)
    : null;
  const transferMetricsWarming = phase === "copying"
    && (speedBytesPerSecond === null || etaSeconds === null);
  const reasonCode = currentOperation?.reason_code || currentPlan?.reason_code || null;
  const ready = ["ready", "ready_with_exclusions"].includes(status);
  const active = MIGRATION_ACTIVE_STATUSES.has(status);
  const terminal = MIGRATION_TERMINAL_STATUSES.has(status);
  const retryAllowed = currentOperation?.retry_allowed === true;
  const capabilities = currentOperation?.capabilities || {};
  const ownerRetryAllowed = Object.prototype.hasOwnProperty.call(capabilities, "owner_retry_allowed")
    ? capabilities.owner_retry_allowed === true
    : true;
  const cancelAllowed = currentOperation
    ? currentOperation.cancel_allowed === true && active
    : status === "building";

  return {
    status,
    phase,
    ready,
    active,
    terminal,
    completedProof,
    percent,
    itemCount: Number.isFinite(itemCount) ? Math.max(0, itemCount) : null,
    completedCount: Number.isFinite(completedCount) ? Math.max(0, completedCount) : null,
    totalBytes: Number.isFinite(totalBytes) ? Math.max(0, totalBytes) : null,
    completedBytes: Number.isFinite(completedBytes) ? Math.max(0, completedBytes) : null,
    speedBytesPerSecond,
    etaSeconds,
    transferMetricsWarming,
    excludedCount: Number.isFinite(Number(currentPlan?.excluded_count)) ? Math.max(0, Number(currentPlan.excluded_count)) : null,
    newAfterHighWatermarkCount: Number.isFinite(Number(currentPlan?.new_after_high_watermark_count)) ? Math.max(0, Number(currentPlan.new_after_high_watermark_count)) : null,
    retainedSourceCount: Number.isFinite(Number(currentPlan?.retained_source_count)) ? Math.max(0, Number(currentPlan.retained_source_count)) : null,
    cleanupPending,
    reasonCode,
    reason: reasonCode ? humanBlockerReason(reasonCode, language) : "",
    nextAction: currentOperation?.next_action || currentPlan?.next_action || null,
    retryMode: currentOperation?.retry_mode || currentPlan?.retry_mode || null,
    canPrepare: Boolean(prepareGate.allowed && !active),
    canPreview: Boolean(prepareGate.allowed && !active),
    canApply: Boolean(applyGate.allowed && ready && currentPlan?.canonical_hash && !active),
    canCancel: Boolean(prepareGate.allowed && cancelAllowed),
    canRetry: Boolean(applyGate.allowed && terminal && retryAllowed && ownerRetryAllowed),
    canCleanupTakeover: Boolean(
      applyGate.allowed
      && terminal
      && capabilities.cleanup_takeover_allowed === true
    ),
    preparePermissionReason: prepareGate.allowed ? "" : prepareGate.reason,
    applyPermissionReason: applyGate.allowed ? "" : applyGate.reason,
    permissionReason: prepareGate.allowed ? "" : prepareGate.reason,
    sourceRootId: currentPlan?.source_root_id || null,
    sourceLabel: currentPlan?.source_label || "",
    targetRootId: currentPlan?.target_root_id || null,
    targetLabel: currentPlan?.target_label || "",
    samePhysicalVolume: typeof currentPlan?.same_physical_volume === "boolean" ? currentPlan.same_physical_volume : null,
    expiresAt: currentPlan?.expires_at || null,
    operationId: currentOperation?.operation_id || currentPlan?.operation_id || null,
    planId: currentPlan?.plan_id || null,
    planHash: currentPlan?.canonical_hash || null,
    manualReviewRequired: ["partial", "failed", "blocked", "interrupted"].includes(status) || cleanupPending,
  };
}

export function archiveRootScenarioModel({ root = null, permission = { allowed: true }, running = false } = {}, language = "ru") {
  const hasPath = Boolean(root?.configured_path || root?.path || root?.root_path || root?.display_path || root?.label);
  const problem = root?.problem || "";
  const canRuntimeActivate = Boolean(root?.requires_activation && !root?.is_active && problem === "root_missing");
  return {
    status: !permission.allowed ? "unavailable_due_to_permissions" : running ? "running" : root?.is_active ? "active" : !hasPath ? "blocked" : root?.is_available === false ? "check_needed" : "idle",
    permissionReason: permission.allowed ? "" : permission.reason,
    canActivate: Boolean(permission.allowed && root && !root.is_active && hasPath && !running),
    reason: problem ? humanBlockerReason(problem, language) : !hasPath ? humanBlockerReason("archive_root_missing", language) : "",
  };
}

export function cameraStorageRows(rows, limit = 12) {
  return (Array.isArray(rows) ? rows : [])
    .map((row) => ({
      camera_id: row?.camera_id,
      camera_name: row?.camera_name || `Camera ${row?.camera_id || "-"}`,
      size_bytes: asNumber(row?.size_bytes, 0),
      segment_count: asNumber(row?.segment_count, 0),
      existing_file_count: asNumber(row?.existing_file_count, 0),
      missing_file_count: asNumber(row?.missing_file_count, 0),
      problem_file_count: asNumber(row?.problem_file_count, 0),
      problem_counts: Object.fromEntries(
        Object.entries(row?.problem_counts || {})
          .map(([reason, count]) => [reason, asNumber(count, 0)])
          .filter(([, count]) => count > 0)
      ),
      oldest_recording_at: row?.oldest_recording_at || null,
      newest_recording_at: row?.newest_recording_at || null,
    }))
    .sort((a, b) => b.size_bytes - a.size_bytes)
    .slice(0, limit);
}
