"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch, canAccessPath, forbiddenMessage } from "../../lib/api";
import { useCurrentUser } from "../../lib/currentUser";
import { useI18n, useLocaleText } from "../../lib/i18n";
import {
  boolLabel,
  activationProgressModel,
  cameraStorageRows,
  formatBytes,
  formatDateTime,
  formatPercent,
  discoveryHeaderStatusModel,
  discoveryStateModel,
  humanBlockerReason,
  isStorageAccessDeniedError,
  actionPermissionState,
  accessRightsModel,
  archiveRootScenarioModel,
  freeSpaceTone,
  migrationScenarioModel,
  normalizeReconciliationSummary,
  reconciliationScenarioModel,
  retentionScenarioModel,
  statusLabel,
  storageTopHealthModel,
  topReasonEntries,
} from "../../lib/storageOperations";

const REFRESH_MS = 30000;

function Stat({ label, value, tone = "neutral" }) {
  return (
    <div className={`storageOpsStat storageOpsStat-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Badge({ label, tone = "neutral" }) {
  return <span className={`storageOpsBadge storageOpsBadge-${tone}`}>{label}</span>;
}

function SummaryRow({ label, value, tone = "neutral" }) {
  return (
    <div className={`storageOpsSummaryRow storageOpsSummaryRow-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function TopMetric({ label, value, detail = "", tone = "neutral" }) {
  return (
    <div className={`storageOpsTopMetric storageOpsTopMetric-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {detail ? <small>{detail}</small> : null}
    </div>
  );
}

function MiniFact({ label, value, tone = "neutral" }) {
  return (
    <div className={`storageOpsMiniFact storageOpsMiniFact-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function StorageDialog({ dialog, onClose }) {
  if (!dialog) return null;
  const hasConfirm = typeof dialog.onConfirm === "function";
  return (
    <div className="storageOpsDialogOverlay" role="presentation">
      <div className={`storageOpsDialog storageOpsDialog-${dialog.tone || "warning"}`} role="dialog" aria-modal="true" aria-labelledby="storage-dialog-title">
        <div className="storageOpsDialogHead">
          <strong id="storage-dialog-title">{dialog.title}</strong>
          <button className={`button secondary small ${hasConfirm ? "storageOpsDialogCloseIconButton" : ""}`} type="button" onClick={onClose} aria-label={dialog.closeLabel || dialog.cancelLabel}>
            {hasConfirm ? "×" : (dialog.closeLabel || dialog.cancelLabel)}
          </button>
        </div>
        <p>{dialog.message}</p>
        {Array.isArray(dialog.items) && dialog.items.length ? (
          <ul className="storageOpsDialogList">
            {dialog.items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}
          </ul>
        ) : null}
        {dialog.action ? <div className="storageOpsDialogAction">{dialog.action}</div> : null}
        {hasConfirm ? (
          <div className="storageOpsDialogFooter">
            <button className="button secondary small" type="button" onClick={onClose}>{dialog.cancelLabel}</button>
            <button className={`button small ${dialog.confirmTone === "danger" ? "dangerButton" : ""}`} type="button" onClick={dialog.onConfirm}>{dialog.confirmLabel}</button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function CheckIcon() {
  return (
    <span aria-hidden="true" className="storageOpsCheckIcon">
      <span />
    </span>
  );
}

function TrashIcon() {
  return (
    <svg className="recordingsUiIcon recordingsTrashIcon recordingsRowSvgIcon storageOpsTrashIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4.2 6.8h15.6"></path>
      <path d="M8.9 6.8V4.5h6.2v2.3"></path>
      <path d="M6.7 7.2 7.6 19c.1 1.05 1 1.9 2.05 1.9h4.7c1.05 0 1.95-.85 2.05-1.9l.9-11.8"></path>
      <path d="M10.1 10.7v6.6"></path>
      <path d="M13.9 10.7v6.6"></path>
    </svg>
  );
}

function OperationRow({ title, status, tone = "neutral", description, meta = null, actions = null, children = null }) {
  return (
    <div className={`storageOpsOperationRow storageOpsOperationRow-${tone}`}>
      <div className="storageOpsOperationMain">
        <div className="storageOpsOperationTitle">
          <strong>{title}</strong>
          <span className={`storageOpsStatusPill storageOpsStatusPill-${tone}`}>{status}</span>
        </div>
        <p>{description}</p>
        {meta}
      </div>
      {actions ? <div className="storageOpsOperationActions">{actions}</div> : null}
      {children ? <div className="storageOpsOperationDetails">{children}</div> : null}
    </div>
  );
}

function Section({ title, children, action = null, className = "" }) {
  return (
    <section className={`storageOpsSection ${className}`}>
      <div className="storageOpsSectionHead">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function reasonText(summary, copy, language) {
  const entries = topReasonEntries(summary);
  if (!entries.length) return copy.noReasons;
  return entries.map(([key, value]) => `${humanBlockerReason(key, language)}: ${value}`).join(", ");
}

function blockerText(blockers, copy, language) {
  const list = Array.isArray(blockers) ? blockers : [];
  if (!list.length) return copy.noReasons;
  const labels = list.map((item) => humanBlockerReason(item, language));
  return Array.from(new Set(labels)).join(" ");
}

function errorDetailText(error, fallback, language) {
  const detail = error?.detail || error?.data?.detail || null;
  if (detail && typeof detail === "object") {
    if (Array.isArray(detail.blockers) && detail.blockers.length) return blockerText(detail.blockers, { noReasons: fallback }, language);
    if (detail.error) return humanBlockerReason(detail.error, language);
    if (detail.reason) return humanBlockerReason(detail.reason, language);
  }
  return error?.message || fallback;
}

function archiveRootDialogText(error, copy, language = "ru") {
  const dict = copy.archiveRootAddDialog || {};
  const detail = error?.data?.detail || error?.detail || null;
  let code = "";
  if (Array.isArray(detail)) code = "invalidRequest";
  else if (detail && typeof detail === "object") {
    const blocker = Array.isArray(detail.blockers) ? detail.blockers[0] : null;
    code = blocker?.reason || blocker || detail.error || detail.reason || detail.message || "";
  } else if (typeof detail === "string") {
    code = detail.split(",").map((item) => item.trim()).filter(Boolean)[0] || detail;
  } else if (error?.message) {
    code = error.message.split(",").map((item) => item.trim()).filter(Boolean)[0] || error.message;
  }
  const pair = dict[code] || dict.generic || [copy.archiveRootAddProblemTitle, copy.archiveRootPermissionAction];
  return {
    title: dict.title || copy.archiveRootAddProblemTitle,
    message: pair[0],
    action: pair[1],
    closeLabel: dict.close || copy.close,
    tone: "warning",
  };
}

function retentionSummaryText(source, copy) {
  if (!source) return copy.retentionNoPreview;
  return copy.retentionPreviewReady
    .replace("{count}", String(source.planned_count || source.deleted_count || 0))
    .replace("{bytes}", formatBytes(source.estimated_freed_bytes ?? source.bytes_freed ?? 0));
}

function reconciliationSummaryText(source, copy) {
  if (!source) return copy.reconciliationNoPreview;
  const normalized = normalizeReconciliationSummary(source);
  return copy.reconciliationPreviewReady
    .replace("{problems}", String(normalized.problemCount))
    .replace("{safe}", String(normalized.safeFixCount))
    .replace("{manual}", String(normalized.reviewOnlyCount || normalized.manualProblemCount))
    .replace("{rows}", String(normalized.totalRows));
}

function healthReasonText(topHealth, recording, copy) {
  if (topHealth?.status === "availability_unconfirmed") return copy.healthReasonAvailability;
  if (topHealth?.status === "unknown") return copy.healthReasonUnknown;
  if (topHealth?.status === "low_disk") return copy.healthReasonLowDisk;
  if (topHealth?.status === "reconciliation") return copy.healthReasonIntegrity;
  if (topHealth?.status === "unreadable") return copy.healthReasonRead;
  if (topHealth?.status === "unwritable" || recording?.tone === "error") return copy.healthReasonWrite;
  if (topHealth?.status === "migration_blocked") return copy.healthReasonMigration;
  if (topHealth?.tone === "ok") return copy.healthReasonOk;
  return copy.healthReasonCheck;
}

function healthActionText(topHealth, copy) {
  if (topHealth?.status === "availability_unconfirmed" || topHealth?.status === "unknown") return copy.actionCheckArchive;
  if (topHealth?.status === "low_disk") return copy.actionReviewSpace;
  if (topHealth?.status === "reconciliation") return copy.actionCheckArchive;
  if (topHealth?.status === "unreadable" || topHealth?.status === "unwritable" || topHealth?.status === "unavailable") return copy.actionCheckAccess;
  if (topHealth?.status === "migration_blocked") return copy.actionRetryLater;
  if (topHealth?.tone === "ok") return copy.noActionNeeded;
  return copy.actionCheckArchive;
}

function rootProblemTone(root) {
  if (!root) return "unknown";
  if (root.is_available === false || root.problem) return "error";
  return "ok";
}

function rootProblemLabel(root, copy, language) {
  return rootHasProblems(root) ? copy.yes : copy.no;
}

function rootHasProblems(root) {
  if (!root) return false;
  if (root.requires_activation && !root.is_active) return false;
  return Boolean(root.problem || root.is_available === false || root.is_readable === false || (root.is_active && root.is_writable === false) || root.namespace_exists === false);
}

function rootProblemItems(root, copy, language) {
  const items = [];
  if (!root) return [copy.no];
  if (root.requires_activation && !root.is_active) return [copy.no];
  if (root.problem) items.push(humanBlockerReason(root.problem, language));
  if (root.is_available === false && !root.problem) items.push(copy.archiveRootUnavailableDetail);
  if (root.is_readable === false) items.push(copy.archiveRootUnreadableDetail);
  if (root.is_active && root.is_writable === false) items.push(copy.archiveRootUnwritableDetail);
  if (root.namespace_exists === false) items.push(copy.archiveRootNamespaceMissingDetail);
  return Array.from(new Set(items.length ? items : [copy.no]));
}

function retentionPolicyText(retention, copy) {
  const gb = retention?.per_camera_gb_limit ?? retention?.camera_gb_limit ?? retention?.max_gb_per_camera;
  const days = retention?.per_camera_days_limit ?? retention?.camera_days_limit ?? retention?.max_days_per_camera;
  const parts = [];
  if (gb != null) parts.push(copy.retentionLimitGb.replace("{value}", String(gb)));
  if (days != null) parts.push(copy.retentionLimitDays.replace("{value}", String(days)));
  return parts.length ? `${copy.retentionPolicyActive} ${parts.join("; ")}.` : copy.retentionPolicyGeneric;
}

function autoFreePolicyText(enabled, copy) {
  return enabled ? copy.autoFreePrimaryOn : copy.autoFreePrimaryOff;
}

function compactAccessLabel(accessRights, copy) {
  if (accessRights.status === "ok") return copy.accessOkShort;
  if (accessRights.status === "unknown") return copy.accessUnknownShort;
  return accessRights.label;
}

function integrityStatusText(scenario, normalized, copy) {
  if (scenario.status === "running") return copy.integrityRunning;
  if (scenario.status === "apply_completed") return copy.integrityFixed;
  if (normalized.problemCount > 0) {
    if (normalized.safeFixCount > 0) return copy.integrityProblemsSafe.replace("{count}", String(normalized.problemCount));
    return copy.integrityProblemsManual.replace("{count}", String(normalized.problemCount));
  }
  if (scenario.status === "preview_completed") return copy.integrityNoProblems;
  return copy.integrityNotChecked;
}

function retentionStatusText(scenario, copy) {
  if (scenario.status === "running") return copy.running;
  if (scenario.status === "apply_failed") return copy.retentionFailedStatus;
  if (scenario.status === "unavailable_due_to_permissions") return copy.unavailable;
  return copy.retentionAutomaticStatus;
}

function archiveProblemsStatusText(normalized, copy) {
  const count = Number(normalized.problemCount || 0);
  if (!count) return copy.archiveProblemsNone;
  return copy.archiveProblemsFound.replace("{count}", String(count));
}

function archiveMigrationStatusText(scenario, archiveRoots, copy) {
  const inactiveRoots = archiveRoots.filter((root) => !root.is_active);
  if (!inactiveRoots.length) return copy.migrationNeedsTargetStatus;
  if (scenario.status === "running") return copy.running;
  if (scenario.status === "apply_blocked") return copy.applyBlocked;
  if (scenario.status === "apply_completed") return copy.applyCompleted;
  if (scenario.canApply) return copy.migrationPlanReadyStatus;
  return copy.migrationChooseTargetStatus;
}

function problemActionStatusText(item, copy) {
  if (item?.safe_action_status === "future_safe_cleanup_possible") return copy.problemFutureCleanup;
  if (item?.safe_action_status === "none") return copy.problemNoAction;
  return copy.problemManualReview;
}

function problemCategoryLabel(item, language) {
  if (language === "en") return item?.label_en || item?.label || item?.label_ru || "";
  if (language === "zh-CN") return item?.label_zh_cn || item?.label || item?.label_ru || "";
  return item?.label_ru || item?.label || "";
}

function problemCategoryReason(item, language) {
  if (language === "en") return item?.reason_no_action_available_en || "";
  if (language === "zh-CN") return item?.reason_no_action_available_zh_cn || "";
  return item?.reason_no_action_available || "";
}

function migrationStatusText(scenario, archiveRoots, copy) {
  const inactiveRoots = archiveRoots.filter((root) => !root.is_active);
  if (!inactiveRoots.length) return copy.migrationNeedsTarget;
  if (scenario.blockerReason) return scenario.blockerReason;
  if (!scenario.targetRootId) return copy.migrationChooseTargetFirst;
  if (scenario.canApply) return copy.migrationPlanReady;
  return copy.migrationScenarioText;
}

function healthTone(operations, pathHealth, capacity, policy, reconciliation) {
  if (!operations?.status && !capacity?.total_bytes) return "unknown";
  if (pathHealth?.readable == null || pathHealth?.writable == null || pathHealth?.available == null) return "unknown";
  if (operations?.status === "critical" || operations?.status === "unavailable" || policy?.recording_suspended_by_low_disk || pathHealth?.writable === false) {
    return "error";
  }
  if (
    operations?.status === "warning" ||
    operations?.status === "degraded" ||
    policy?.state === "warning" ||
    policy?.state === "cleanup_threshold" ||
    pathHealth?.available === false ||
    reconciliation?.problem_file_count > 0
  ) {
    return "warning";
  }
  return "ok";
}

function healthTitle(copy, tone) {
  if (tone === "ok") return copy.healthOkTitle;
  if (tone === "warning") return copy.healthWarningTitle;
  if (tone === "error") return copy.healthCriticalTitle;
  return copy.healthUnknownTitle;
}

function archiveRootLabel(root, copy) {
  const label = String(root?.label || root?.name || root?.id || "");
  if (/^default archive$/i.test(label) || /^default$/i.test(label)) return copy.defaultArchive;
  return label || copy.defaultArchive;
}

function archiveRootPath(root, fallback = "-") {
  return root?.configured_path || root?.root_path || root?.path || root?.archive_host_path || fallback || "-";
}

function archiveRootStateText(root, copy) {
  if (root?.is_active) return copy.activeRoot;
  return copy.inactiveRoot || copy.oldInactive;
}

function availabilityState(pathHealth, copy) {
  if (pathHealth?.available === true) return { label: copy.availabilityConfirmed, tone: "ok" };
  if (pathHealth?.available === false) return { label: copy.availabilityNeedsCheck, tone: "warning" };
  return { label: copy.availabilityUnknown, tone: "unknown" };
}

function recordingState(operations, pathHealth, policy, copy) {
  if (policy?.recording_suspended_by_low_disk) {
    return { label: copy.recordingUnavailable, detail: copy.recordingSuspended, tone: "error" };
  }
  if (operations?.status === "unavailable" || pathHealth?.readable === false || pathHealth?.writable === false) {
    return { label: copy.recordingUnavailable, detail: copy.recordingAccessBlocked, tone: "error" };
  }
  if (pathHealth?.readable === true && pathHealth?.writable === true && pathHealth?.available === true) {
    return { label: copy.recordingPossible, detail: copy.recordingPossibleDetail, tone: "ok" };
  }
  if (pathHealth?.readable === true && pathHealth?.writable === true && pathHealth?.available === false) {
    return { label: copy.recordingNeedsCheck, detail: copy.recordingNeedsCheckDetail, tone: "warning" };
  }
  return { label: copy.recordingUnknown, detail: copy.recordingUnknownDetail, tone: "unknown" };
}

function operationTone(status = "") {
  const value = String(status || "");
  if (value.includes("failed") || value.includes("blocked") || value.includes("permission")) return "warning";
  if (value.includes("completed") || value.includes("preview")) return "ok";
  if (value.includes("running")) return "warning";
  return "neutral";
}

function activationProgressTone(status) {
  if (status?.status === "failed" || status?.status === "failed_recovery_required") return "error";
  if (status?.status === "completed") return "ok";
  if (status?.status === "running") return "warning";
  return "neutral";
}

function activationProgressStatusText(status, copy) {
  if (status?.status === "queued" || status?.status === "running") return copy.activationRunning;
  if (status?.status === "completed") return copy.activationCompleted;
  if (status?.status === "failed_recovery_required") return copy.activationRecoveryRequired;
  if (status?.status === "failed") return copy.activationFailed;
  return copy.activationIdle;
}

function activationProgressReasonText(status, copy, language) {
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

function activationProgressItems(status, copy) {
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

export default function StorageOperationsPage() {
  const [status, setStatus] = useState(null);
  const statusRef = useRef(null);
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [refreshWarning, setRefreshWarning] = useState("");
  const [accessDenied, setAccessDenied] = useState(false);
  const [archiveRootDiscovery, setArchiveRootDiscovery] = useState(null);
  const [archiveRootChoiceId, setArchiveRootChoiceId] = useState("");
  const [archiveRootFolderName, setArchiveRootFolderName] = useState("KM-VMS-Recordings");
  const [archiveRootDialog, setArchiveRootDialog] = useState(null);
  const [trackedActivationOperationId, setTrackedActivationOperationId] = useState(null);
  const [dismissedActivationOperationId, setDismissedActivationOperationId] = useState(null);
  const [migrationTargetRootId, setMigrationTargetRootId] = useState("");
  const [migrationPreviewState, setMigrationPreviewState] = useState(null);
  const [rootAction, setRootAction] = useState("");
  const [autoFreeMessage, setAutoFreeMessage] = useState("");
  const [archiveRootMessage, setArchiveRootMessage] = useState("");
  const [retentionMessage, setRetentionMessage] = useState("");
  const [retentionPreview, setRetentionPreview] = useState(null);
  const [retentionResult, setRetentionResult] = useState(null);
  const [retentionConfirmed, setRetentionConfirmed] = useState(false);
  const [reconciliationMessage, setReconciliationMessage] = useState("");
  const [reconciliationPreview, setReconciliationPreview] = useState(null);
  const [reconciliationResult, setReconciliationResult] = useState(null);
  const [reconciliationConfirmed, setReconciliationConfirmed] = useState(false);
  const [migrationMessage, setMigrationMessage] = useState("");
  const [migrationResult, setMigrationResult] = useState(null);
  const { currentUser, status: currentUserStatus } = useCurrentUser();
  const { locale: language, t } = useI18n();
  const copy = useLocaleText("storagePage");

  const canOpenStorage = currentUser ? canAccessPath(currentUser, "/storage") : false;

  const loadStatus = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setRefreshing(true);
    try {
      const [data, settingsData] = await Promise.all([
        apiFetch("/storage/status"),
        apiFetch("/settings").catch(() => null),
      ]);
      setStatus(data);
      statusRef.current = data;
      setSettings(settingsData);
      setError("");
      setRefreshWarning("");
      setAccessDenied(false);
      return true;
    } catch (err) {
      if (isStorageAccessDeniedError(err)) {
        setAccessDenied(true);
        setError(forbiddenMessage(language));
        return false;
      }
      if (silent && statusRef.current) {
        setRefreshWarning(t("storagePage.refreshFailedStale", { time: formatDateTime(statusRef.current?.storage_operations?.checked_at, language) }));
        return false;
      }
      setError(err?.message || copy.loadFailed);
      return false;
    } finally {
      setLoading(false);
      if (!silent) setRefreshing(false);
    }
  }, [language, copy.loadFailed, t]);

  const loadArchiveRootDiscovery = useCallback(async () => {
    setArchiveRootDiscovery((current) => ({ ...(current || {}), status: "refreshing", freshness: "refreshing", refresh_in_progress: true }));
    try {
      const discovery = await apiFetch("/storage/archive-roots/discovery");
      setArchiveRootDiscovery(discovery);
      return discovery;
    } catch (err) {
      setArchiveRootDiscovery({
        status: "unavailable",
        freshness: "unavailable",
        available: false,
        refresh_error: "storage_discovery_refresh_failed",
      });
      return null;
    }
  }, []);

  useEffect(() => {
    if (currentUserStatus === "loading") return;
    if (!currentUser || !canOpenStorage) {
      setLoading(false);
      setAccessDenied(true);
      setError(forbiddenMessage(language));
      return;
    }

    let cancelled = false;
    let timer = null;
    async function start() {
      const canContinue = await loadStatus({ silent: true });
      if (!cancelled && canContinue) {
        timer = setInterval(() => loadStatus({ silent: true }), REFRESH_MS);
      }
    }
    start();
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [canOpenStorage, currentUser, currentUserStatus, language, loadStatus]);

  const operations = status?.storage_operations || {};
  const storageContract = status?.storage_contract || {};
  const capacity = operations.capacity || {};
  const pathHealth = operations.path_health || {};
  const namespace = operations.namespace_health || {};
  const owned = operations.owned_archive || {};
  const policy = operations.low_disk_policy || {};
  const autoCleanup = operations.auto_free_space_cleanup || {};
  const retention = operations.retention || {};
  const reconciliation = operations.reconciliation || {};
  const recent = operations.recent_operations || {};
  const archiveRoots = status?.archive_roots || operations.archive_roots || [];
  const archiveRootActivation = status?.archive_root_activation || operations.archive_root_activation || {};
  const activationModel = activationProgressModel(archiveRootActivation);
  const activationIsActive = ["queued", "running", "failed_recovery_required"].includes(activationModel.status);
  const activationIsTrackedTerminal = ["failed", "completed"].includes(activationModel.status)
    && trackedActivationOperationId === activationModel.operationId
    && dismissedActivationOperationId !== activationModel.operationId;
  const showArchiveRootActivation = activationIsActive || activationIsTrackedTerminal;
  const archiveRootActivationReason = activationProgressReasonText(archiveRootActivation, copy, language);
  const migrationPreview = migrationPreviewState || operations.migration_preview || status?.migration_preview || {};
  const archiveRootDiscoveryModel = discoveryStateModel(archiveRootDiscovery || {});
  const archiveRootDiscoveryHeader = discoveryHeaderStatusModel(archiveRootDiscovery);
  const archiveRootChoices = archiveRootDiscoveryModel.candidates;
  const inactiveArchiveRoots = archiveRoots.filter((root) => !root.is_active);
  const cameraRows = useMemo(() => cameraStorageRows(operations.per_camera_usage), [operations.per_camera_usage]);
  const autoFreeEnabled = settings?.auto_free_space_cleanup_enabled ?? policy.auto_free_space_cleanup_enabled ?? autoCleanup.enabled;
  const topHealth = storageTopHealthModel({ operations, pathHealth, capacity, policy, reconciliation, migrationPreview, retention }, language);
  const tone = topHealth.tone || healthTone(operations, pathHealth, capacity, policy, reconciliation);
  const accessRights = accessRightsModel(pathHealth, language);
  const availability = availabilityState(pathHealth, copy);
  const recording = recordingState(operations, pathHealth, policy, copy);
  const normalizedReconciliation = normalizeReconciliationSummary(reconciliation, language);
  const archivePathText = storageContract.archive_primary_path || storageContract.archive_host_path || storageContract.storage_host_path || "-";
  const currentArchiveRoot = archiveRoots.find((root) => root.is_active) || archiveRoots[0] || null;
  const archiveVolumeGroups = Array.isArray(operations.volume_groups) && operations.volume_groups.length
    ? operations.volume_groups
    : [
        {
          physical_volume_id: "active",
          display_label: copy.currentArchive,
          capacity,
          archive_size_bytes: owned.size_bytes,
          playable_file_count: owned.segments_count,
          problem_file_count: normalizedReconciliation.problemCount,
          roots: currentArchiveRoot ? [currentArchiveRoot] : [],
        },
      ];
  const problemDetails = reconciliation.problem_details || {};
  const visibleProblemCategories = problemDetails.categories?.length ? problemDetails.categories : normalizedReconciliation.categories;
  const visibleProblemSamples = problemDetails.samples || [];
  const manageSettingsPermission = actionPermissionState(currentUser, "manage_settings", language);
  const retentionPermission = actionPermissionState(currentUser, "delete_recordings", language);
  const diagnosticsPermission = actionPermissionState(currentUser, "run_diagnostics", language);
  const retentionScenario = retentionScenarioModel({
    preview: retentionPreview,
    result: retentionResult,
    retention,
    permission: retentionPermission,
    running: rootAction.startsWith("retention-"),
  }, language);
  const reconciliationScenario = reconciliationScenarioModel({
    preview: reconciliationPreview,
    result: reconciliationResult,
    reconciliation,
    canCheck: diagnosticsPermission,
    canApply: manageSettingsPermission,
    running: rootAction.startsWith("reconciliation-"),
  }, language);
  const migrationScenario = migrationScenarioModel({
    preview: migrationPreview,
    result: migrationResult,
    permission: manageSettingsPermission,
    running: rootAction === "preview" || rootAction === "apply-migration",
  }, language);
  const retentionTone = operationTone(retentionScenario.status);
  const reconciliationTone = operationTone(reconciliationScenario.status);
  const migrationTone = operationTone(migrationScenario.status);
  const currentArchivePath = archiveRootPath(currentArchiveRoot, archivePathText);
  const healthReason = healthReasonText(topHealth, recording, copy);
  const healthAction = healthActionText(topHealth, copy);
  const retentionPrimaryText = retentionPolicyText(retention, copy);
  const autoFreePrimaryText = autoFreePolicyText(autoFreeEnabled, copy);
  const integrityPrimaryText = integrityStatusText(reconciliationScenario, normalizedReconciliation, copy);
  const migrationPrimaryText = migrationStatusText(migrationScenario, archiveRoots, copy);
  const archiveRootSelectionReady = archiveRootDiscoveryModel.current && archiveRootFolderName.trim() && archiveRootChoiceId;

  useEffect(() => {
    if (!archiveRootChoiceId && archiveRootChoices.length) {
      const recommended = archiveRootChoices.find((choice) => choice.recommended) || archiveRootChoices[0];
      setArchiveRootChoiceId(recommended.id);
    }
  }, [archiveRootChoiceId, archiveRootChoices]);

  useEffect(() => {
    if (!migrationTargetRootId && inactiveArchiveRoots.length) {
      setMigrationTargetRootId(inactiveArchiveRoots[0].id);
    }
  }, [inactiveArchiveRoots, migrationTargetRootId]);

  useEffect(() => {
    if (!["queued", "running"].includes(String(archiveRootActivation?.status || ""))) return undefined;
    const timer = setInterval(() => loadStatus({ silent: true }), 1500);
    return () => clearInterval(timer);
  }, [archiveRootActivation?.status, loadStatus]);

  useEffect(() => {
    const operationId = activationModel.operationId;
    if (!operationId) return;
    if (["queued", "running", "failed_recovery_required"].includes(activationModel.status) && trackedActivationOperationId !== operationId) {
      setTrackedActivationOperationId(operationId);
      return;
    }
    if (trackedActivationOperationId !== operationId || dismissedActivationOperationId === operationId) return;
    const recoveryRequired = activationModel.recoveryRequired;
    setArchiveRootDialog({
      activationOperationId: operationId,
      title: copy.activationProgressTitle,
      message: activationProgressStatusText(archiveRootActivation, copy),
      items: activationProgressItems(archiveRootActivation, copy),
      action: archiveRootActivationReason || copy.activationProgressHint,
      confirmLabel: recoveryRequired ? copy.activationRetryRecovery : undefined,
      cancelLabel: copy.close,
      closeLabel: copy.close,
      tone: activationProgressTone(archiveRootActivation),
      onConfirm: recoveryRequired
        ? () => activateRoot(archiveRootActivation.target_root_id || archiveRootActivation.previous_root_id, true)
        : undefined,
    });
  }, [
    activationModel.operationId,
    activationModel.status,
    activationModel.recoveryRequired,
    archiveRootActivation,
    archiveRootActivationReason,
    copy,
    dismissedActivationOperationId,
    trackedActivationOperationId,
  ]);

  async function setAutoFreeSpace(nextEnabled) {
    if (!manageSettingsPermission.allowed) {
      setAutoFreeMessage(manageSettingsPermission.reason);
      return;
    }
    setRootAction("auto-free");
    setAutoFreeMessage("");
    try {
      const updated = await apiFetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_free_space_cleanup_enabled: Boolean(nextEnabled) }),
      });
      setSettings(updated);
      setAutoFreeMessage(nextEnabled ? copy.autoFreeEnabled : copy.autoFreeDisabled);
      await loadStatus({ silent: true });
    } catch (err) {
      setAutoFreeMessage(errorDetailText(err, copy.autoFreeChangeFailed, language));
    } finally {
      setRootAction("");
    }
  }

  function archiveRootSelectionPayload() {
    if (archiveRootChoiceId) {
      return {
        candidate_id: archiveRootChoiceId,
        discovery_snapshot_id: archiveRootDiscoveryModel.snapshotId,
        folder_name: archiveRootFolderName.trim(),
      };
    }
    return {};
  }

  async function addRoot() {
    const payload = archiveRootSelectionPayload();
    if (!payload.candidate_id || !payload.discovery_snapshot_id || !payload.folder_name) return;
    if (!manageSettingsPermission.allowed) {
      setArchiveRootDialog({
        title: copy.archiveRootAddProblemTitle,
        message: manageSettingsPermission.reason,
        action: copy.archiveRootPermissionAction,
        closeLabel: copy.close,
        tone: "warning",
      });
      return;
    }
    setRootAction("add");
    setArchiveRootMessage("");
    try {
      await apiFetch("/storage/archive-roots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, label: archiveRootFolderName.trim() || "Archive root", make_active: false, confirm: false }),
      });
      setArchiveRootMessage(copy.rootAdded);
      await loadStatus({ silent: true });
      await loadArchiveRootDiscovery();
    } catch (err) {
      setArchiveRootDialog(archiveRootDialogText(err, copy, language));
    } finally {
      setRootAction("");
    }
  }

  function requestActivateRoot(root) {
    if (!root?.id) return;
    if (!manageSettingsPermission.allowed) {
      setArchiveRootDialog({
        title: copy.archiveRootAddProblemTitle,
        message: manageSettingsPermission.reason,
        action: copy.archiveRootPermissionAction,
        closeLabel: copy.close,
        tone: "warning",
      });
      return;
    }
    setArchiveRootDialog({
      title: copy.switchArchiveRootTitle,
      message: copy.switchConfirm,
      action: archiveRootPath(root, archivePathText),
      confirmLabel: copy.makeActive,
      cancelLabel: copy.cancel,
      closeLabel: copy.cancel,
      tone: "warning",
      onConfirm: () => {
        setArchiveRootDialog(null);
        activateRoot(root.id);
      },
    });
  }

  async function activateRoot(rootId, recovery = false) {
    if (!rootId) return;
    if (!manageSettingsPermission.allowed) {
      setArchiveRootDialog({
        title: copy.archiveRootAddProblemTitle,
        message: manageSettingsPermission.reason,
        action: copy.archiveRootPermissionAction,
        closeLabel: copy.close,
        tone: "warning",
      });
      return;
    }
    setRootAction(`activate-${rootId}`);
    setArchiveRootMessage("");
    setArchiveRootDialog({
      title: copy.activationProgressTitle,
      message: recovery ? copy.activationRollbackRunning : copy.activationQueued,
      action: copy.activationProgressHint,
      closeLabel: copy.close,
      tone: "warning",
    });
    try {
      const operation = await apiFetch(`/storage/archive-roots/${encodeURIComponent(rootId)}/activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true, recovery }),
      });
      if (operation?.operation_id) {
        setTrackedActivationOperationId(operation.operation_id);
        setDismissedActivationOperationId(null);
      }
      await loadStatus({ silent: true });
    } catch (err) {
      setArchiveRootDialog({
        title: copy.rootNotSwitched,
        message: errorDetailText(err, copy.rootNotSwitched, language),
        closeLabel: copy.close,
        tone: "warning",
      });
    } finally {
      setRootAction("");
    }
  }

  function showRootProblems(root) {
    setArchiveRootDialog({
      title: copy.archiveRootProblemDetailsTitle,
      message: archiveRootPath(root, archivePathText),
      items: rootProblemItems(root, copy, language),
      action: copy.archiveRootProblemDetailsAction,
      closeLabel: copy.close,
      tone: "warning",
    });
  }

  function requestDeleteRoot(root) {
    if (!root?.id || root.is_active) return;
    if (!manageSettingsPermission.allowed) {
      setArchiveRootDialog({
        title: copy.archiveRootDeleteTitle,
        message: manageSettingsPermission.reason,
        action: copy.archiveRootPermissionAction,
        closeLabel: copy.close,
        tone: "warning",
      });
      return;
    }
    if (!retentionPermission.allowed) {
      setArchiveRootDialog({
        title: copy.archiveRootDeleteTitle,
        message: retentionPermission.reason,
        action: copy.archiveRootPermissionAction,
        closeLabel: copy.close,
        tone: "warning",
      });
      return;
    }
    const count = Number(root.segments_count || 0);
    const size = Number(root.size_bytes || 0);
    const message = count > 0
      ? copy.archiveRootDeleteNonEmptyConfirm.replace("{count}", String(count)).replace("{size}", formatBytes(size))
      : copy.archiveRootDeleteEmptyConfirm;
    setArchiveRootDialog({
      title: copy.archiveRootDeleteTitle,
      message,
      action: archiveRootPath(root, archivePathText),
      confirmLabel: copy.delete,
      cancelLabel: copy.cancel,
      closeLabel: copy.cancel,
      tone: "error",
      confirmTone: "danger",
      onConfirm: () => {
        setArchiveRootDialog(null);
        deleteRoot(root.id);
      },
    });
  }

  async function deleteRoot(rootId) {
    if (!rootId) return;
    setRootAction(`delete-${rootId}`);
    setArchiveRootMessage("");
    try {
      await apiFetch(`/storage/archive-roots/${encodeURIComponent(rootId)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      setArchiveRootMessage(copy.archiveRootDeleted);
      await loadStatus({ silent: true });
    } catch (err) {
      const detail = err?.detail || err?.data?.detail;
      if (detail?.status === "partial") {
        setArchiveRootDialog({
          title: copy.archiveRootDeletePartialTitle,
          message: copy.archiveRootDeletePartialMessage
            .replace("{deleted}", String(detail.segments_deleted || 0))
            .replace("{remaining}", String(detail.remaining_count || 0)),
          action: copy.archiveRootDeletePartialAction,
          closeLabel: copy.close,
          tone: "error",
        });
      } else {
        setArchiveRootMessage(errorDetailText(err, copy.archiveRootNotDeleted, language));
      }
      await loadStatus({ silent: true });
    } finally {
      setRootAction("");
    }
  }

  function closeArchiveRootDialog() {
    if (archiveRootDialog?.activationOperationId) {
      setDismissedActivationOperationId(archiveRootDialog.activationOperationId);
    }
    setArchiveRootDialog(null);
  }

  async function runRetentionPreview() {
    if (!retentionPermission.allowed) {
      setRetentionMessage(retentionPermission.reason);
      return;
    }
    setRootAction("retention-preview");
    setRetentionResult(null);
    setRetentionConfirmed(false);
    setRetentionMessage("");
    try {
      const preview = await apiFetch("/recordings/retention/dry-run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      setRetentionPreview(preview);
      setRetentionMessage(copy.retentionPreviewCompleted);
    } catch (err) {
      setRetentionMessage(errorDetailText(err, copy.previewFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function applyRetentionPlan() {
    if (!retentionPreview?.planned_count || !retentionConfirmed) return;
    if (!retentionPermission.allowed) {
      setRetentionMessage(retentionPermission.reason);
      return;
    }
    setRootAction("retention-apply");
    setRetentionMessage("");
    try {
      const result = await apiFetch("/recordings/retention/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      setRetentionResult(result);
      setRetentionPreview(null);
      setRetentionConfirmed(false);
      setRetentionMessage(copy.retentionApplyCompleted);
      await loadStatus({ silent: true });
    } catch (err) {
      setRetentionMessage(errorDetailText(err, copy.applyBlocked, language));
    } finally {
      setRootAction("");
    }
  }

  async function runReconciliationPreview() {
    if (!diagnosticsPermission.allowed) {
      setReconciliationMessage(diagnosticsPermission.reason);
      return;
    }
    setRootAction("reconciliation-preview");
    setReconciliationResult(null);
    setReconciliationConfirmed(false);
    setReconciliationMessage("");
    try {
      const preview = await apiFetch("/storage/reconciliation/summary");
      setReconciliationPreview(preview);
      setReconciliationMessage(copy.reconciliationCheckCompleted);
    } catch (err) {
      setReconciliationMessage(errorDetailText(err, copy.previewFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function applyReconciliationSafe() {
    if (!reconciliationPreview || !reconciliationConfirmed) return;
    if (!manageSettingsPermission.allowed) {
      setReconciliationMessage(manageSettingsPermission.reason);
      return;
    }
    setRootAction("reconciliation-apply");
    setReconciliationMessage("");
    try {
      const result = await apiFetch("/storage/reconcile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "apply_safe" }),
      });
      setReconciliationResult(result);
      setReconciliationPreview(null);
      setReconciliationConfirmed(false);
      setReconciliationMessage(copy.reconciliationApplyCompleted);
      await loadStatus({ silent: true });
    } catch (err) {
      setReconciliationMessage(errorDetailText(err, copy.applyBlocked, language));
    } finally {
      setRootAction("");
    }
  }

  async function refreshMigrationPreview() {
    if (!manageSettingsPermission.allowed) {
      setMigrationMessage(manageSettingsPermission.reason);
      return;
    }
    if (!migrationTargetRootId) {
      setMigrationMessage(copy.migrationChooseTargetFirst);
      return;
    }
    setRootAction("preview");
    setMigrationMessage("");
    try {
      const result = await apiFetch("/storage/migration/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_root_id: migrationTargetRootId }),
      });
      setMigrationPreviewState(result);
      setMigrationMessage(copy.previewUpdated);
      await loadStatus({ silent: true });
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.previewFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function applyMigration() {
    if (!migrationPreview?.apply_available || !migrationTargetRootId) return;
    if (!manageSettingsPermission.allowed) {
      setMigrationMessage(manageSettingsPermission.reason);
      return;
    }
    if (!window.confirm(copy.applyConfirm)) return;
    setRootAction("apply-migration");
    setMigrationMessage("");
    setMigrationResult(null);
    try {
      const result = await apiFetch("/storage/migration/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_root_id: migrationTargetRootId,
          plan_id: migrationPreview.plan_id || null,
          confirm: true,
        }),
      });
      setMigrationResult(result);
      setMigrationMessage(copy.applyCompleted);
      await loadStatus({ silent: true });
    } catch (err) {
      const detail = err?.detail || err?.data?.detail || null;
      setMigrationResult(detail && typeof detail === "object" ? detail : { status: "blocked", blockers: [{ reason: err?.message || copy.applyBlocked }] });
      setMigrationMessage(errorDetailText(err, copy.applyBlocked, language));
    } finally {
      setRootAction("");
    }
  }

  return (
    <Layout>
      <div className="storageOpsPage">
        <header className="pageHeader storageOpsHeader">
          <div className="storageOpsHeaderInner">
            <h1 className="pageTitle">{copy.title}</h1>
            <div className="storageOpsHeaderMeta">
              <div className="pageSubtitle storageOpsHeaderSubtitle">{copy.subtitle}</div>
              <div className="storageOpsHeaderActions">
                <span>{copy.lastCheck}: {formatDateTime(operations.checked_at, language)}</span>
                <button className="button secondary small storageOpsRefreshButton" type="button" onClick={() => loadStatus()} disabled={refreshing || loading || accessDenied}>
                  {refreshing ? copy.refreshing : copy.refresh}
                </button>
              </div>
            </div>
          </div>
        </header>

        {loading ? (
          <div className="storageOpsState">{copy.loading}</div>
        ) : accessDenied ? (
          <div className="storageOpsState storageOpsState-error">{error || forbiddenMessage(language)}</div>
        ) : error ? (
          <div className="storageOpsState storageOpsState-error">{error}</div>
        ) : (
          <>
            <section className={`storageOpsOverview storageOpsOverview-${tone}`}>
              <div className="storageOpsHealthMain">
                <div className={`storageOpsHealthMark storageOpsHealthMark-${tone}`} aria-hidden="true">!</div>
                <div>
                  <strong>{healthTitle(copy, tone)}</strong>
                  <p>{healthReason}</p>
                  <div className="storageOpsBadges">
                    <Badge label={compactAccessLabel(accessRights, copy)} tone={accessRights.tone} />
                    <Badge label={recording.detail} tone={recording.tone} />
                  </div>
                </div>
              </div>
              <div className="storageOpsTopMetrics" aria-label={copy.firstScreenMetrics}>
                <TopMetric label={copy.recording} value={recording.label} detail={recording.detail} tone={recording.tone} />
                <TopMetric label={copy.archiveProblems} value={String(normalizedReconciliation.problemCount || 0)} detail={copy.integrity} tone={normalizedReconciliation.problemCount ? "warning" : "ok"} />
              </div>
              <div className="storageOpsHealthAction">
                <strong>{copy.primaryAction}</strong>
                <span>{healthAction}</span>
              </div>
            </section>
            {refreshWarning ? <div className="storageOpsState storageOpsState-warning">{refreshWarning}</div> : null}

            <div className="storageOpsDashboard">
              <Section title={copy.archiveSpace} className="storageOpsSection-archive">
                <div className="storageOpsVolumeGroups">
                  {archiveVolumeGroups.map((group) => {
                    const groupCapacity = group.capacity || {};
                    const groupUsagePercent = Number(groupCapacity.usage_percent || 0);
                    const groupProblemCount = Number(group.problem_file_count || 0);
                    return (
                      <div className="storageOpsVolumeGroup" key={group.physical_volume_id || group.display_label || "active"}>
                        <div className="storageOpsCapacityHeader">
                          <div>
                            <span>{group.display_label || group.physical_volume_id || copy.archiveSpace}</span>
                            <strong>{formatBytes(groupCapacity.total_bytes)}</strong>
                          </div>
                          <span>{formatPercent(groupCapacity.usage_percent)} {copy.used}</span>
                        </div>
                        <div className="storageOpsCapacityBar" aria-label={copy.storageUsage}>
                          <span style={{ width: `${Math.max(0, Math.min(100, groupUsagePercent))}%` }} />
                        </div>
                        <div className="storageOpsMiniGrid">
                          <MiniFact label={copy.total} value={formatBytes(groupCapacity.total_bytes)} />
                          <MiniFact label={copy.free} value={formatBytes(groupCapacity.free_bytes)} tone={freeSpaceTone(groupCapacity, policy)} />
                          <MiniFact label={copy.archiveSize} value={formatBytes(group.archive_size_bytes)} />
                          <MiniFact label={copy.segments} value={String(group.playable_file_count || 0)} />
                          <MiniFact label={copy.problems} value={String(groupProblemCount)} tone={groupProblemCount ? "warning" : "ok"} />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </Section>

              <Section title={copy.archiveOperations} className="storageOpsSection-operations">
                <div className="storageOpsOperationList">
                  <OperationRow
                    title={copy.retentionRules}
                    status={retentionStatusText(retentionScenario, copy)}
                    tone={retentionTone}
                    description={retentionPrimaryText}
                    meta={(
                      <div className="storageOpsOperationFacts">
                        <MiniFact label={copy.lastRun} value={formatDateTime(retention.last_finished_at || retention.last_started_at, language)} />
                        <MiniFact label={copy.deleted} value={String(retention.last_summary?.deleted_count || 0)} />
                        <MiniFact label={copy.freed} value={formatBytes(retention.last_summary?.bytes_freed)} />
                      </div>
                    )}
                  >
                    {retentionMessage ? <div className="storageOpsNote storageOpsNoteStrong">{retentionMessage}</div> : null}
                    <details className="storageOpsInlineDetails">
                      <summary>{copy.retentionDiagnostics}</summary>
                      <button className="button secondary small" type="button" title={copy.retentionDryRun} onClick={runRetentionPreview} disabled={!!rootAction || !retentionScenario.canPreview}>{rootAction === "retention-preview" ? copy.calculating : copy.retentionPlanShort}</button>
                      <label className="storageOpsConfirm">
                        <input type="checkbox" checked={retentionConfirmed} onChange={(event) => setRetentionConfirmed(event.target.checked)} disabled={!retentionScenario.canApply || !!rootAction} />
                        <span>{copy.retentionConfirm}</span>
                      </label>
                      <button className="button small dangerButton" type="button" title={copy.retentionApply} onClick={applyRetentionPlan} disabled={!retentionScenario.canApply || !retentionConfirmed || !!rootAction}>{rootAction === "retention-apply" ? copy.applying : copy.retentionDeleteShort}</button>
                      <div className="storageOpsNote">{copy.retentionSafetyNote}</div>
                      {!retentionPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{retentionPermission.reason}</div> : null}
                      {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                      <SummaryRow label={copy.blockersReasons} value={reasonText(autoCleanup.last_summary, copy, language)} />
                    </details>
                  </OperationRow>

                  <OperationRow
                    title={copy.autoFreeSpace}
                    status={autoFreeEnabled ? copy.on : copy.off}
                    tone={autoFreeEnabled ? "ok" : "neutral"}
                    description={autoFreePrimaryText}
                    actions={(
                      <button className="button secondary small" type="button" title={autoFreeEnabled ? copy.disableAutoFree : copy.enableAutoFree} onClick={() => setAutoFreeSpace(!autoFreeEnabled)} disabled={!!rootAction || !manageSettingsPermission.allowed}>
                        {rootAction === "auto-free" ? copy.saving : autoFreeEnabled ? copy.disableAutoFreeShort : copy.enableAutoFreeShort}
                      </button>
                    )}
                  >
                    {autoFreeMessage ? <div className="storageOpsNote storageOpsNoteStrong">{autoFreeMessage}</div> : null}
                    <details className="storageOpsInlineDetails">
                      <summary>{copy.supportDetails}</summary>
                      <SummaryRow label={copy.policy} value={copy.lowDiskShort} />
                      {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                    </details>
                  </OperationRow>

                  <OperationRow
                    title={copy.archiveProblems}
                    status={archiveProblemsStatusText(normalizedReconciliation, copy)}
                    tone={reconciliationTone}
                    description={integrityPrimaryText}
                    meta={(
                      <div className="storageOpsOperationFacts">
                        <MiniFact label={copy.problems} value={String(normalizedReconciliation.problemCount || 0)} tone={normalizedReconciliation.problemCount ? "warning" : "ok"} />
                        <MiniFact label={copy.safeFixes} value={String(normalizedReconciliation.safeFixCount || 0)} />
                        <MiniFact label={copy.manualReview} value={String(normalizedReconciliation.reviewOnlyCount || normalizedReconciliation.manualProblemCount || 0)} />
                        <MiniFact label={copy.lastCheck} value={formatDateTime(reconciliation.last_checked_at, language)} />
                      </div>
                    )}
                    actions={(
                      <button className="button secondary small" type="button" title={copy.reconciliationDryRun} onClick={runReconciliationPreview} disabled={!!rootAction || !reconciliationScenario.canCheck}>{rootAction === "reconciliation-preview" ? copy.checking : copy.integrityCheckShort}</button>
                    )}
                  >
                    {reconciliationMessage ? <div className="storageOpsNote storageOpsNoteStrong">{reconciliationMessage}</div> : null}
                    {visibleProblemCategories.length ? (
                      <div className="storageOpsProblemList">
                        {visibleProblemCategories.map((item) => (
                          <div key={item.code || item.label}>
                            <strong>{problemCategoryLabel(item, language)}</strong>
                            <span>{item.count}</span>
                            <small>{problemActionStatusText(item, copy)}</small>
                            {problemCategoryReason(item, language) ? <p>{problemCategoryReason(item, language)}</p> : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {visibleProblemSamples.length ? (
                      <div className="storageOpsNote">{copy.problemSamples}: {visibleProblemSamples.map((item) => item.sample_name || item.category).filter(Boolean).slice(0, 4).join(", ")}</div>
                    ) : null}
                    <details className="storageOpsInlineDetails">
                      <summary>{copy.supportDetails}</summary>
                      <label className="storageOpsConfirm">
                        <input type="checkbox" checked={reconciliationConfirmed} onChange={(event) => setReconciliationConfirmed(event.target.checked)} disabled={!reconciliationScenario.canApply || !!rootAction} />
                        <span>{copy.reconciliationConfirm}</span>
                      </label>
                      <button className="button small" type="button" title={copy.reconciliationApply} onClick={applyReconciliationSafe} disabled={!reconciliationScenario.canApply || !reconciliationConfirmed || !!rootAction}>{rootAction === "reconciliation-apply" ? copy.applying : copy.integrityFixShort}</button>
                      {reconciliationScenario.noAutoFixReason ? <div className="storageOpsNote storageOpsNoteStrong">{copy.reconciliationNoAutoFixes}</div> : null}
                      {reconciliationScenario.categories?.length ? (
                        <div className="storageOpsNote">
                          {copy.reconciliationCategories}: {reconciliationScenario.categories.map((item) => `${item.label}: ${item.count}`).join(", ")}
                        </div>
                      ) : null}
                      {!diagnosticsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{diagnosticsPermission.reason}</div> : null}
                      {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{reconciliationScenario.applyPermissionReason}</div> : null}
                      <div className="storageOpsNote">{copy.integrityNote}</div>
                    </details>
                  </OperationRow>

                  <OperationRow
                    title={copy.archiveMigration}
                    status={archiveMigrationStatusText(migrationScenario, archiveRoots, copy)}
                    tone={migrationTone}
                    description={migrationPrimaryText}
                    meta={(
                      <div className="storageOpsOperationFacts">
                        <MiniFact label={copy.move} value={`${migrationPreview.total_would_move_count || 0} / ${formatBytes(migrationPreview.total_would_move_bytes)}`} />
                        <MiniFact label={copy.willStay} value={String(migrationPreview.total_would_stay_count || 0)} />
                        <MiniFact label={copy.blockers} value={String((migrationPreview.blockers || []).length)} tone={(migrationPreview.blockers || []).length ? "warning" : "ok"} />
                        <MiniFact label={copy.applyState} value={migrationPreview.apply_available ? copy.available : copy.unavailable} tone={migrationPreview.apply_available ? "ok" : "warning"} />
                      </div>
                    )}
                    actions={(
                      <button className="button secondary small" type="button" title={copy.refreshMigrationPreview} onClick={refreshMigrationPreview} disabled={!!rootAction || !migrationScenario.canPreview}>{rootAction === "preview" ? copy.calculating : copy.migrationPreviewShort}</button>
                    )}
                  >
                    {migrationMessage ? <div className="storageOpsNote storageOpsNoteStrong">{migrationMessage}</div> : null}
                    <details className="storageOpsInlineDetails">
                      <summary>{copy.supportDetails}</summary>
                      <div className="storageOpsNote">{copy.migrationSteps}</div>
                      {inactiveArchiveRoots.length ? (
                        <label className="storageOpsField">
                          <span>{copy.migrationTarget}</span>
                          <select className="select" value={migrationTargetRootId} onChange={(event) => setMigrationTargetRootId(event.target.value)} disabled={!!rootAction}>
                            {inactiveArchiveRoots.map((root) => (
                              <option key={root.id} value={root.id}>{archiveRootLabel(root, copy)} - {archiveRootPath(root, archivePathText)}</option>
                            ))}
                          </select>
                        </label>
                      ) : (
                        <div className="storageOpsNote storageOpsNoteStrong">{copy.migrationAddTargetFirst}</div>
                      )}
                      <button className="button small" type="button" title={copy.applyMigration} onClick={applyMigration} disabled={!!rootAction || !migrationScenario.canApply}>{rootAction === "apply-migration" ? copy.applying : copy.applyMigrationShort}</button>
                      {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                      {migrationResult ? <SummaryRow label={copy.applyReport} value={`${statusLabel(migrationResult.status, language)}; ${copy.executed}: ${(migrationResult.executed || []).length}; ${copy.sourcePreserved}: ${boolLabel(migrationResult.source_preserved, language)}; ${copy.cleanupPending}: ${boolLabel(migrationResult.cleanup_pending, language)}`} /> : null}
                      {migrationScenario.manualReviewRequired ? <div className="storageOpsNote">{copy.migrationManualReview}</div> : null}
                    </details>
                  </OperationRow>
                </div>
                <div className="storageOpsNote storageOpsNoteStrong">{copy.safeActionNote}</div>
              </Section>

              <Section title={copy.archiveRoots} className="storageOpsSection-secondary storageOpsSection-roots">
                <div className="storageOpsRootList">
                  {(archiveRoots.length ? archiveRoots : [currentArchiveRoot]).filter(Boolean).map((root) => {
                    const rootScenario = archiveRootScenarioModel({
                      root,
                      permission: manageSettingsPermission,
                      running: rootAction === `activate-${root.id}`,
                    }, language);
                    const active = Boolean(root.is_active);
                    return (
                      <div className="storageOpsRootListRow" key={root.id || archiveRootPath(root, archivePathText)}>
                        <div className="storageOpsRootPath">
                          <span>{active ? copy.currentArchive : copy.archiveLocation}</span>
                          <strong>{archiveRootPath(root, archivePathText)}</strong>
                          <small>{archiveRootLabel(root, copy)}</small>
                        </div>
                        <div className="storageOpsRootDeleteCell">
                          <button
                            className="storageOpsRootDeleteButton"
                            type="button"
                            title={copy.deleteArchiveRoot}
                            aria-label={copy.deleteArchiveRoot}
                            disabled={active || !!rootAction || !retentionPermission.allowed}
                            onClick={() => requestDeleteRoot(root)}
                          >
                            <TrashIcon />
                          </button>
                        </div>
                        <div><span>{copy.state}</span><strong>{archiveRootStateText(root, copy)}</strong></div>
                        <div><span>{copy.size}</span><strong>{formatBytes(root.size_bytes ?? owned.size_bytes)}</strong></div>
                        <div><span>{copy.segments}</span><strong>{root.segments_count ?? owned.segments_count ?? 0}</strong></div>
                        <div>
                          <span>{copy.problems}</span>
                          {rootHasProblems(root) ? (
                            <button className="storageOpsBadgeButton" type="button" onClick={() => showRootProblems(root)}>
                              <Badge label={rootProblemLabel(root, copy, language)} tone={rootProblemTone(root)} />
                            </button>
                          ) : (
                            <Badge label={rootProblemLabel(root, copy, language)} tone={rootProblemTone(root)} />
                          )}
                        </div>
                        <div className="storageOpsRootActionsCell">
                          <button
                            className={`storageOpsRootActivateButton ${active ? "isActive" : ""}`}
                            type="button"
                            title={active ? copy.activeRoot : copy.makeActive}
                            aria-label={active ? copy.activeRoot : copy.makeActive}
                            disabled={active || !rootScenario.canActivate}
                            onClick={() => requestActivateRoot(root)}
                          >
                            <CheckIcon />
                          </button>
                        </div>
                      </div>
                    );
                  })}
                  {!archiveRoots.length && !currentArchiveRoot ? <div className="storageOpsEmpty">{copy.noRoots}</div> : null}
                </div>
                {archiveRootMessage ? <div className="storageOpsNote storageOpsNoteStrong">{archiveRootMessage}</div> : null}
                {showArchiveRootActivation ? (
                  <div className={`storageOpsNote storageOpsNoteStrong storageOpsActivationProgress storageOpsActivationProgress-${activationProgressTone(archiveRootActivation)}`}>
                    <strong>{copy.activationProgressTitle}: {activationProgressStatusText(archiveRootActivation, copy)}</strong>
                    <ul className="storageOpsDialogList">
                      {activationProgressItems(archiveRootActivation, copy).map((item) => <li key={item}>{item}</li>)}
                    </ul>
                    {archiveRootActivationReason ? <div>{archiveRootActivationReason}</div> : null}
                  </div>
                ) : null}
                <details
                  className="storageOpsDetails storageOpsAdvancedRoot"
                  onToggle={(event) => {
                    if (event.currentTarget.open && !archiveRootDiscoveryModel.refreshing) loadArchiveRootDiscovery();
                  }}
                >
                  <summary>
                    <span className="storageOpsAdvancedRootSummaryContent">
                      <span>{copy.addArchiveRoot}</span>
                      <span
                        className={`storageOpsDiscoveryStatus storageOpsDiscoveryStatus-${archiveRootDiscoveryHeader.tone}`}
                        role="status"
                        aria-live="polite"
                        aria-atomic="true"
                        title={copy[`discoveryStatus_${archiveRootDiscoveryHeader.state}`]}
                      >
                        {copy[`discoveryStatus_${archiveRootDiscoveryHeader.state}`]}
                      </span>
                    </span>
                  </summary>
                  <div className="storageOpsAdvancedRootBody">
                    <div className="storageOpsRootForm storageOpsRootForm-product">
                      <label className="storageOpsField">
                        <span>{copy.storageRootLabel}</span>
                        <select className="select" value={archiveRootChoiceId} onChange={(event) => setArchiveRootChoiceId(event.target.value)} disabled={!!rootAction}>
                          {archiveRootChoices.map((choice) => (
                            <option key={choice.id} value={choice.id}>
                              {choice.label || choice.path} - {copy.discoveryFreeOfTotal
                                .replace("{free}", formatBytes(choice.free_bytes))
                                .replace("{total}", formatBytes(choice.total_bytes))}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="storageOpsField">
                        <span>{copy.storageFolder}</span>
                        <input className="input" value={archiveRootFolderName} onChange={(event) => setArchiveRootFolderName(event.target.value)} placeholder="KM-VMS-Recordings" />
                      </label>
                      <button className="button small storageOpsRootAddButton" type="button" onClick={addRoot} disabled={!!rootAction || !archiveRootSelectionReady || !manageSettingsPermission.allowed}>{rootAction === "add" ? copy.adding : copy.add}</button>
                    </div>
                    <div className="storageOpsDiscoveryFeedback">
                      {archiveRootDiscoveryHeader.needsRefresh ? (
                        <button className="button secondary small" type="button" onClick={loadArchiveRootDiscovery} disabled={!!rootAction}>
                          {copy.refreshDiscovery}
                        </button>
                      ) : null}
                    </div>
                    {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                  </div>
                </details>
              </Section>

              {recent.available && recent.items?.length ? (
                <Section title={copy.recentOperations} className="storageOpsSection-secondary storageOpsSection-recent">
                  <div className="storageOpsRecent">
                    {recent.items.map((item, index) => (
                      <div className="storageOpsRecentItem" key={`${item.type || "operation"}-${index}`}>
                        <strong>{item.title || statusLabel(item.type, language)}</strong>
                        <span>{item.summary || statusLabel(item.status, language)}</span>
                      </div>
                    ))}
                  </div>
                </Section>
              ) : null}
            </div>

            <Section title={copy.cameras}>
              {cameraRows.length ? (
                <>
                  <div className="storageOpsTableWrap storageOpsCameraTable">
                    <table className="storageOpsTable">
                      <thead>
                        <tr>
                          <th>{copy.camera}</th>
                          <th>{copy.size}</th>
                          <th>{copy.segments}</th>
                          <th>{copy.problems}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {cameraRows.map((row) => (
                          <tr key={row.camera_id || row.camera_name}>
                            <td><strong>{row.camera_name}</strong></td>
                            <td>{formatBytes(row.size_bytes)}</td>
                            <td>{row.segment_count}</td>
                            <td>{row.missing_file_count} / {row.problem_file_count}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div className="storageOpsCameraCards">
                    {cameraRows.map((row) => (
                      <div className="storageOpsCameraCard" key={`card-${row.camera_id || row.camera_name}`}>
                        <div className="storageOpsCameraCardIdentity">
                          <strong>{row.camera_name}</strong>
                        </div>
                        <div className="storageOpsCameraCardMetric"><span>{copy.size}</span><strong>{formatBytes(row.size_bytes)}</strong></div>
                        <div className="storageOpsCameraCardMetric"><span>{copy.segments}</span><strong>{row.segment_count}</strong></div>
                        <div className="storageOpsCameraCardMetric"><span>{copy.problems}</span><strong>{row.missing_file_count} / {row.problem_file_count}</strong></div>
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <div className="storageOpsEmpty">{copy.noCameraOwned}</div>
              )}
            </Section>
            <details className="storageOpsSupportDetails">
              <summary>{copy.supportDetails}</summary>
              <dl>
                <div><dt>{copy.namespaceState}</dt><dd>{namespace.storage_namespace || "-"}</dd></div>
                <div><dt>{copy.availability}</dt><dd>{availability.label}</dd></div>
                <div><dt>{copy.namespaceExists}</dt><dd>{boolLabel(namespace.namespace_exists, language)}</dd></div>
                <div><dt>{copy.scanMode}</dt><dd>{namespace.scan_mode || "-"}</dd></div>
                <div><dt>{copy.partialScanReason}</dt><dd>{namespace.partial_reason || "-"}</dd></div>
                <div><dt>{copy.missingNoMetadata}</dt><dd>{`${reconciliation.missing_file_count || 0} / ${reconciliation.orphan_file_count || 0}`}</dd></div>
                <div><dt>{copy.reasonOrphanFile}</dt><dd>{String(reconciliation.orphan_file_count || 0)}</dd></div>
                <div><dt>{copy.invalidOutside}</dt><dd>{`${reconciliation.invalid_path_count || 0} / ${reconciliation.path_outside_storage_count || 0}`}</dd></div>
              </dl>
            </details>
          </>
        )}
        <StorageDialog dialog={archiveRootDialog} onClose={closeArchiveRootDialog} />
      </div>
    </Layout>
  );
}
