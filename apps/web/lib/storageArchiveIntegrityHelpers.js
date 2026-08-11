import { asNumber, finiteCount, statusLabel } from "./storageOperationsSharedHelpers.js";

export function integrityOperationPresentation(reconciliation = {}) {
  const statusValue = String(reconciliation?.status || "not_run").toLowerCase();
  const stale = reconciliation?.evidence_status === "stale" || reconciliation?.stale === true;
  const problemCount = Math.max(
    0,
    finiteCount(reconciliation?.problem_file_count)
      ?? Object.values(reconciliation?.category_counts || {}).reduce((total, value) => total + Math.max(0, finiteCount(value) ?? 0), 0)
  );
  const active = reconciliation?.active === true || ["queued", "running", "cancel_requested"].includes(statusValue);
  let status = "not_run";
  let tone = "unknown";
  if (active) {
    status = statusValue === "cancel_requested" ? "cancel_requested" : "running";
    tone = "warning";
  } else if (statusValue === "completed" && stale) {
    status = "stale";
    tone = "warning";
  } else if (statusValue === "completed") {
    status = problemCount > 0 ? "findings" : "clean";
    tone = problemCount > 0 ? "warning" : "ok";
  } else if (["partial", "failed", "interrupted", "cancelled"].includes(statusValue)) {
    status = statusValue;
    tone = statusValue === "failed" ? "error" : "warning";
  } else if (statusValue && statusValue !== "not_run" && statusValue !== "never_run") {
    status = "unknown";
  }
  return {
    status,
    tone,
    problemCount,
    checkedCount: Math.max(0, finiteCount(reconciliation?.checked_count) ?? 0),
    failedCount: Math.max(0, finiteCount(reconciliation?.failed_count) ?? 0),
    lastCheckedAt: reconciliation?.last_checked_at || null,
    scanId: reconciliation?.scan_id || null,
    stale,
  };
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
  automatic_reconciliation_pending: "integrityNoActionAutomaticReconciliation",
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
  const stale = Boolean(scan?.stale);
  const planned = Math.max(0, asNumber(progress.planned_count, 0));
  const checked = Math.max(0, asNumber(progress.checked_count, 0));
  const metadataChecked = Math.max(
    0,
    asNumber(progress.metadata_checked_count, Math.min(checked, planned)),
  );
  const filesystemChecked = Math.max(
    0,
    asNumber(progress.filesystem_checked_count, Math.max(checked - planned, 0)),
  );
  const found = stale ? 0 : Math.max(0, asNumber(progress.found_count, 0));
  const failed = Math.max(0, asNumber(progress.failed_count, 0));
  const running = ["queued", "running", "cancel_requested", "interrupted"].includes(status);
  const terminal = ["completed", "partial", "failed", "cancelled"].includes(status);
  const phase = String(scan?.phase || "queued");
  const successfulTerminal = ["completed", "partial"].includes(status);
  const progressIndeterminate = running && phase === "filesystem";
  const percent = successfulTerminal
    ? 100
    : planned > 0
      ? Math.max(0, Math.min(running ? 99 : 100, (metadataChecked / planned) * 100))
      : 0;
  const completedWithFindings = status === "completed" && found > 0;
  const operationCancelAllowed = scan?.operation?.cancel_allowed;
  return {
    status,
    titleKey: completedWithFindings ? "integrityScanCompletedWithProblemsTitle" : presentation.titleKey,
    detailKey: completedWithFindings ? "integrityScanCompletedWithProblemsText" : presentation.detailKey,
    tone: stale && status === "completed" || completedWithFindings ? "warning" : presentation.tone,
    running,
    terminal,
    stale,
    planned,
    checked,
    metadataChecked,
    filesystemChecked,
    found,
    failed,
    percent,
    progressIndeterminate,
    canStart: Boolean(permission.allowed && !running),
    canCancel: Boolean(permission.allowed && running && operationCancelAllowed !== false && status !== "cancel_requested"),
    permissionReason: permission.allowed ? "" : permission.reason,
    phaseKey: `integrityPhase${phase.replace(/(^|_)([a-z])/g, (_, _separator, letter) => letter.toUpperCase())}`,
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
    detailKey: finding?.category === "zero_size_file"
      ? "integrityDetailZeroSize"
      : finding?.category === "partial_file"
        ? "integrityDetailPartial"
        : null,
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
