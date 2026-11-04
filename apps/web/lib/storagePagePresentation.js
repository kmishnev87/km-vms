import { activationProgressModel, humanBlockerReason } from "./storageOperations";

export const ACTIVATION_ACK_KEY = "kmvms.storage.activation.acknowledged";

export function newStorageOperationId(prefix) {
  const randomPart = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 14)}`;
  return `${prefix}-${randomPart}`;
}


export function blockerText(blockers, copy, language) {
  const list = Array.isArray(blockers) ? blockers : [];
  if (!list.length) return copy.noReasons;
  const labels = list.map((item) => humanBlockerReason(item, language));
  return Array.from(new Set(labels)).join(" ");
}


export function errorDetailText(error, fallback, language) {
  const detail = error?.detail || error?.data?.detail || null;
  if (detail && typeof detail === "object") {
    if (Array.isArray(detail.blockers) && detail.blockers.length) return blockerText(detail.blockers, { noReasons: fallback }, language);
    if (detail.reason_code) return humanBlockerReason(detail.reason_code, language);
    if (detail.error) return humanBlockerReason(detail.error, language);
    if (detail.reason) return humanBlockerReason(detail.reason, language);
  }
  const candidate = typeof detail === "string" ? detail : String(error?.message || "");
  const normalized = candidate.trim();
  if (!normalized || normalized.length > 320) return fallback;
  if (/^(http\s+\d+|[a-z0-9]+(?:[_.:-][a-z0-9]+)+)$/i.test(normalized)) return fallback;
  if (/[{}\[\]\\]|\/(?:api|storage|volume|app)\//i.test(normalized)) return fallback;
  return normalized;
}


export function archiveRootPath(root, fallback = "-") {
  return root?.configured_path || root?.root_path || root?.path || root?.archive_host_path || fallback || "-";
}


export function activationProgressTone(status) {
  if (status?.status === "failed" || status?.status === "failed_recovery_required") return "error";
  if (status?.status === "completed") return "ok";
  if (status?.status === "running") return "warning";
  return "neutral";
}


export function activationProgressStatusText(status, copy) {
  if (status?.status === "queued" || status?.status === "running") return copy.activationRunning;
  if (status?.status === "completed") return copy.activationCompleted;
  if (status?.status === "failed_recovery_required") return copy.activationRecoveryRequired;
  if (status?.status === "failed") return copy.activationFailed;
  return copy.activationIdle;
}


export function activationProgressReasonText(status, copy, language) {
  const model = activationProgressModel(status);
  const messages = {
    storage_activation_queued: copy.activationQueued,
    storage_activation_stopping_recordings: copy.activationStopping,
    storage_activation_switching_location: copy.activationSwitching,
    storage_activation_restoring_recordings: copy.activationRestoring,
    storage_activation_checking_archive_access: copy.activationChecking,
    storage_activation_completed: copy.activationCompleted,
    storage_activation_failed_previous_location_preserved: copy.activationFailedPreviousPreserved,
    storage_activation_failed_previous_location_restored: copy.activationFailedPreviousRestored,
    storage_activation_restoring_previous_location: copy.activationRollbackRunning,
    storage_activation_recovery_required: copy.activationRecoveryRequiredDetail,
    storage_activation_completed_with_archive_access_problem: copy.activationArchiveAccessProblem,
  };
  const base = messages[model.presentationKey] || "";
  if (model.recoveryRequired && model.effectiveRootLabel) {
    return `${base} ${copy.activationEffectiveRoot.replace("{root}", model.effectiveRootLabel)}`.trim();
  }
  return base;
}


export function activationProgressItems(status, copy) {
  const labels = {
    recordings_stopped: copy.activationStepStopRecordings,
    runtime_applied: copy.activationStepApplyRuntime,
    cameras_restored: copy.activationStepRestoreCameras,
    archive_access_checked: copy.activationStepCheckAccess,
  };
  const model = activationProgressModel(status);
  const items = model.steps.map((step) => `${step.done ? "✓" : step.active ? "…" : "·"} ${labels[step.key]}`);
  if (model.rollback.status !== "not_required") {
    items.push(`${model.rollback.completed ? "✓" : model.rollback.active ? "…" : model.rollback.failed ? "!" : "·"} ${copy.activationStepRollback}`);
  }
  return items;
}
