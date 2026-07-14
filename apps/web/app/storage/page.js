"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { OperationDialog, OperationToast } from "../../components/OperationFeedback";
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
  archiveIntegrityActionContract,
  archiveIntegrityCategoryPresentations,
  archiveIntegrityFindingPresentation,
  archiveIntegrityScanModel,
  archiveRootCleanupCapabilityModel,
  archiveRootScenarioModel,
  freeSpaceTone,
  migrationScenarioModel,
  normalizeReconciliationSummary,
  recentOperationPresentations,
  reconciliationScenarioModel,
  retentionScenarioModel,
  statusLabel,
  storageTopHealthModel,
  topReasonEntries,
} from "../../lib/storageOperations";

const REFRESH_MS = 30000;
const ACTIVATION_ACK_KEY = "kmvms.storage.activation.acknowledged";

function newStorageOperationId(prefix) {
  const randomPart = globalThis.crypto?.randomUUID
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 14)}`;
  return `${prefix}-${randomPart}`;
}

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

function CheckIcon() {
  return (
    <span aria-hidden="true" className="storageOpsCheckIcon">✓</span>
  );
}

function TrashIcon() {
  return (
    <svg className="recordingsUiIcon recordingsTrashIcon recordingsRowSvgIcon storageOpsTrashIcon" viewBox="0 1 24 24" aria-hidden="true" focusable="false">
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

function ArchiveIntegrityDialog({
  open,
  scan,
  findings,
  nextCursor,
  busy,
  error,
  selectedFinding,
  plan,
  confirmed,
  copy,
  language,
  permission,
  onClose,
  onStart,
  onCancel,
  onLoadMore,
  onPrepare,
  onClearPlan,
  onConfirmChange,
  onApply,
}) {
  const dialogRef = useRef(null);
  const closeRef = useRef(null);
  const scanModel = archiveIntegrityScanModel(scan || {}, permission);
  const categories = archiveIntegrityCategoryPresentations(scan?.category_counts);
  const findingRows = findings.map(archiveIntegrityFindingPresentation);
  const selectedRow = selectedFinding ? archiveIntegrityFindingPresentation(selectedFinding) : null;
  const actionContract = archiveIntegrityActionContract(selectedRow?.actionKey);
  const resultState = plan && ["completed", "partial", "blocked", "failed", "cancelled"].includes(String(plan.state || ""))
    ? String(plan.state)
    : null;

  useEffect(() => {
    if (!open) return undefined;
    const timer = window.setTimeout(() => closeRef.current?.focus({ preventScroll: true }), 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  if (!open) return null;

  function handleKeyDown(event) {
    if (event.key === "Escape" && !busy) {
      event.preventDefault();
      onClose();
    }
  }

  const checkedTime = scan?.finished_at || scan?.started_at || scan?.created_at;
  const canShowFindings = ["completed", "partial"].includes(scanModel.status) && scanModel.found > 0;
  const primaryStartLabel = scanModel.status === "not_run" ? copy.integrityCheckArchive : copy.integrityCheckAgain;
  const resultTone = resultState === "completed" ? "ok" : resultState === "failed" ? "error" : "warning";

  return (
    <div className="storageIntegrityOverlay" role="presentation">
      <section
        ref={dialogRef}
        className="storageIntegrityDialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="storage-integrity-dialog-title"
        aria-busy={busy ? "true" : "false"}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="storageIntegrityDialogHead">
          <div>
            <span className="storageIntegrityEyebrow">{copy.archiveProblems}</span>
            <h2 id="storage-integrity-dialog-title">{copy.integrityModalTitle}</h2>
          </div>
          <button ref={closeRef} className="storageIntegrityClose" type="button" onClick={onClose} disabled={busy} aria-label={copy.close}>×</button>
        </header>

        <div className="storageIntegrityDialogBody">
          <section className={`storageIntegrityState storageIntegrityState-${scanModel.tone}`}>
            <div className="storageIntegrityStateHead">
              <div>
                <strong>{copy[scanModel.titleKey] || copy.integrityScanFailedTitle}</strong>
                <p>{scanModel.stale ? copy.integrityScanStaleText : copy[scanModel.detailKey] || copy.integrityScanFailedText}</p>
              </div>
              {checkedTime ? <time dateTime={checkedTime}>{formatDateTime(checkedTime, language)}</time> : null}
            </div>
            {scanModel.running ? (
              <>
                <div className="storageIntegrityProgress" aria-label={copy.integrityProgress}>
                  <span style={{ width: `${scanModel.percent}%` }} />
                </div>
                <div className="storageIntegrityProgressFacts">
                  <span>{copy.integrityPhase}: {copy[scanModel.phaseKey] || copy.integrityPhaseChecking}</span>
                  <span>{copy.integrityChecked}: {scanModel.checked}{scanModel.planned ? ` / ${scanModel.planned}` : ""}</span>
                  <span>{copy.problems}: {scanModel.found}</span>
                  {scanModel.failed ? <span>{copy.integrityFailedItems}: {scanModel.failed}</span> : null}
                </div>
              </>
            ) : null}
          </section>

          {error ? <div className="storageIntegrityMessage storageIntegrityMessage-error" role="alert">{error}</div> : null}
          {!permission.allowed ? <div className="storageIntegrityMessage storageIntegrityMessage-warning">{permission.reason}</div> : null}

          {categories.length ? (
            <section className="storageIntegrityCategories" aria-label={copy.integrityCategorySummaryTitle}>
              {categories.map((item) => (
                <span key={item.category}>{copy[item.labelKey] || copy.integrityCategoryUnknown}<strong>{item.count}</strong></span>
              ))}
            </section>
          ) : null}

          {canShowFindings ? (
            <section className="storageIntegrityFindings">
              <div className="storageIntegritySectionHead">
                <h3>{copy.integrityFindingsTitle}</h3>
                <span>{scanModel.found}</span>
              </div>
              <div className="storageIntegrityFindingList">
                {findingRows.map((item, index) => (
                  <article className={`storageIntegrityFinding storageIntegrityFinding-${item.tone}`} key={item.key || `${item.categoryKey}-${index}`}>
                    <div className="storageIntegrityFindingHead">
                      <strong>{copy[item.categoryKey] || copy.integrityCategoryUnknown}</strong>
                      <span>{copy[item.impactKey] || copy.integrityImpactUnknown}</span>
                    </div>
                    <dl>
                      {item.cameraName ? <div><dt>{copy.camera}</dt><dd>{item.cameraName}</dd></div> : null}
                      {item.rootLabel ? <div><dt>{copy.integrityRootLabel}</dt><dd>{item.rootLabel}</dd></div> : null}
                      {item.displayName ? <div><dt>{copy.integrityFileLabel}</dt><dd>{item.displayName}</dd></div> : null}
                    </dl>
                    <div className="storageIntegrityFindingAction">
                      {item.actionAllowed && !item.stale ? (
                        <button className="button secondary small" type="button" onClick={() => onPrepare(findings[index])} disabled={busy || scanModel.stale}>
                          {copy[item.actionLabelKey]}
                        </button>
                      ) : (
                        <span>{item.stale ? copy.integrityNoActionStale : copy[item.noActionLabelKey] || copy.integrityNoActionUnavailable}</span>
                      )}
                    </div>
                  </article>
                ))}
              </div>
              {nextCursor ? (
                <button className="button secondary small storageIntegrityLoadMore" type="button" onClick={onLoadMore} disabled={busy}>
                  {busy ? copy.loading : copy.integrityLoadMore}
                </button>
              ) : null}
            </section>
          ) : null}

          {["completed", "partial"].includes(scanModel.status) && scanModel.found === 0 ? (
            <div className="storageIntegrityClean">{copy.integrityNoFindings}</div>
          ) : null}

          {selectedRow && plan ? (
            <section className={`storageIntegrityPlan storageIntegrityPlan-${resultTone}`}>
              <div className="storageIntegritySectionHead">
                <h3>{resultState ? copy.integrityResultTitle : copy.integrityConfirmationTitle}</h3>
                <button type="button" className="storageIntegrityBack" onClick={onClearPlan} disabled={busy}>{copy.integrityBackToFindings}</button>
              </div>
              <strong>{copy[selectedRow.categoryKey] || copy.integrityCategoryUnknown}</strong>
              <p>{resultState
                ? copy[`integrityResult${resultState.charAt(0).toUpperCase()}${resultState.slice(1)}`] || copy.integrityResultFailed
                : copy[actionContract.confirmationKey] || copy.integrityNoActionUnavailable}</p>
              {!resultState ? (
                <>
                  <label className="storageIntegrityConfirm">
                    <input type="checkbox" checked={confirmed} onChange={(event) => onConfirmChange(event.target.checked)} disabled={busy} />
                    <span>{copy.integrityConfirmationAcknowledge}</span>
                  </label>
                  <button className={`button small ${actionContract.destructive ? "dangerButton" : ""}`} type="button" onClick={onApply} disabled={busy || !confirmed}>
                    {busy ? copy.applying : copy.integrityApplyAction}
                  </button>
                </>
              ) : (
                <button className="button secondary small" type="button" onClick={onClearPlan}>{copy.integrityAcknowledgeResult}</button>
              )}
            </section>
          ) : null}
        </div>

        <footer className="storageIntegrityDialogFooter">
          {scanModel.canCancel ? <button className="button secondary small" type="button" onClick={onCancel} disabled={busy}>{copy.integrityCancelScan}</button> : null}
          {scanModel.canStart ? <button className="button small" type="button" onClick={onStart} disabled={busy || !permission.allowed}>{busy ? copy.checking : primaryStartLabel}</button> : null}
          <button className="button secondary small" type="button" onClick={onClose} disabled={busy}>{copy.close}</button>
        </footer>
      </section>
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

function operationReasonText(operation, copy, language) {
  const entries = topReasonEntries(operation?.last_summary);
  if (entries.length) {
    return entries.map(([key, value]) => `${humanBlockerReason(key, language)}: ${value}`).join(", ");
  }
  return operation?.last_error ? humanBlockerReason(operation.last_error, language) : copy.noReasons;
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
    if (detail.reason_code) return humanBlockerReason(detail.reason_code, language);
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

function archiveRootCleanupReason(detail, copy) {
  const reason = String(detail?.reason || detail?.failure?.reason || "");
  if ([
    "root_marker_missing",
    "root_marker_product_mismatch",
    "root_marker_path_mismatch",
    "root_marker_container_path_mismatch",
    "root_marker_not_regular_file",
    "root_marker_symlink_rejected",
    "root_path_symlink_rejected",
  ].includes(reason)) {
    return copy.archiveRootCleanupMarkerProblem;
  }
  if ([
    "archive_root_cleanup_identity_revalidation_failed",
    "archive_root_cleanup_physical_identity_missing",
    "selected_mount_missing",
    "storage_discovery_refresh_failed",
  ].includes(reason)) {
    return copy.archiveRootCleanupDiskChanged;
  }
  if ([
    "selected_mount_not_readable",
    "selected_mount_not_searchable",
    "selected_mount_not_writable",
    "root_marker_remove_failed",
    "filesystem_delete_failed",
  ].includes(reason)) {
    return copy.archiveRootCleanupAccessProblem;
  }
  if (["root_directory_remove_failed", "foreign_or_user_content_preserved"].includes(reason)) {
    return copy.archiveRootCleanupFolderBusy;
  }
  if (["filesystem_delete_failed", "metadata_update_failed_after_file_delete"].includes(reason)) {
    return copy.archiveRootCleanupRecordingProblem;
  }
  return copy.archiveRootCleanupGenericProblem;
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
  return rootHasProblems(root) ? "error" : "ok";
}

function rootProblemLabel(root, copy, language) {
  return rootHasProblems(root) ? copy.yes : copy.no;
}

function rootHasProblems(root) {
  if (!root) return false;
  if (root.requires_activation && !root.is_active && Number(root.segments_count || 0) === 0) return false;
  return Boolean(root.problem || root.is_available === false || root.is_readable === false || (root.is_active && root.is_writable === false) || root.namespace_exists === false);
}

function rootProblemItems(root, copy, language) {
  const items = [];
  if (!root) return [copy.no];
  if (root.requires_activation && !root.is_active && Number(root.segments_count || 0) === 0) return [copy.no];
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
  if (normalized.problemCount > 0) return copy.archiveProblemsFound.replace("{count}", String(normalized.problemCount));
  if (scenario.status === "preview_completed") return copy.integrityNoProblems;
  return copy.integrityNotChecked;
}

function retentionStatusText(scenario, copy) {
  if (scenario.status === "running") return copy.running;
  if (scenario.status === "pending") return copy.retentionPendingStatus;
  if (scenario.status === "apply_failed") return copy.retentionFailedStatus;
  if (scenario.status === "unavailable_due_to_permissions") return copy.unavailable;
  return copy.retentionAutomaticStatus;
}

function archiveProblemsStatusText(normalized, reconciliation, copy) {
  if (reconciliation?.active) return copy.integrityRunning;
  if (!["completed", "partial"].includes(String(reconciliation?.status || ""))) return copy.integrityNotChecked;
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
  const [operationToast, setOperationToast] = useState(null);
  const [trackedActivationOperationId, setTrackedActivationOperationId] = useState(null);
  const [dismissedActivationOperationId, setDismissedActivationOperationId] = useState(null);
  const [migrationTargetRootId, setMigrationTargetRootId] = useState("");
  const [migrationPreviewState, setMigrationPreviewState] = useState(null);
  const [rootAction, setRootAction] = useState("");
  const [integrityDialogOpen, setIntegrityDialogOpen] = useState(false);
  const [integrityScan, setIntegrityScan] = useState(null);
  const [integrityFindings, setIntegrityFindings] = useState([]);
  const [integrityNextCursor, setIntegrityNextCursor] = useState(null);
  const [integrityBusy, setIntegrityBusy] = useState(false);
  const [integrityError, setIntegrityError] = useState("");
  const [integritySelectedFinding, setIntegritySelectedFinding] = useState(null);
  const [integrityPlan, setIntegrityPlan] = useState(null);
  const [integrityConfirmed, setIntegrityConfirmed] = useState(false);
  const [migrationMessage, setMigrationMessage] = useState("");
  const [migrationResult, setMigrationResult] = useState(null);
  const { currentUser, status: currentUserStatus } = useCurrentUser();
  const { locale: language, t } = useI18n();
  const copy = useLocaleText("storagePage");

  const canOpenStorage = currentUser ? canAccessPath(currentUser, "/storage") : false;

  useEffect(() => {
    try {
      setDismissedActivationOperationId(window.sessionStorage.getItem(ACTIVATION_ACK_KEY));
    } catch (_) {}
  }, []);

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
  const owned = operations.owned_archive || {};
  const policy = operations.low_disk_policy || {};
  const autoCleanup = operations.auto_free_space_cleanup || {};
  const retention = operations.retention || {};
  const reconciliation = operations.reconciliation || {};
  const recent = operations.recent_operations || {};
  const recentOperationRows = recentOperationPresentations(recent.items, 5);
  const archiveRoots = status?.archive_roots || operations.archive_roots || [];
  const archiveRootActivation = status?.archive_root_activation || operations.archive_root_activation || {};
  const activationModel = activationProgressModel(archiveRootActivation);
  const archiveRootActivationReason = activationProgressReasonText(archiveRootActivation, copy, language);
  const migrationPreview = migrationPreviewState || {};
  const archiveRootDiscoveryModel = discoveryStateModel(archiveRootDiscovery || {});
  const archiveRootDiscoveryHeader = discoveryHeaderStatusModel(archiveRootDiscovery);
  const archiveRootChoices = archiveRootDiscoveryModel.candidates;
  const inactiveArchiveRoots = archiveRoots.filter((root) => !root.is_active);
  const cameraRows = useMemo(() => cameraStorageRows(operations.per_camera_usage), [operations.per_camera_usage]);
  const autoFreeConfigured = settings?.auto_free_space_cleanup_enabled ?? policy.auto_free_space_cleanup_enabled ?? autoCleanup.enabled;
  const autoFreeEnabled = settings?.auto_free_space_cleanup_effective ?? policy.auto_free_space_cleanup_effective ?? autoCleanup.effective_enabled ?? false;
  const autoFreeTermsVersion = settings?.auto_free_space_terms_version || policy.terms_version || autoCleanup.terms_version || "";
  const autoFreeAcknowledgedVersion = settings?.auto_free_space_acknowledged_terms_version || null;
  const autoFreeAcknowledgementRequired = Boolean(
    settings?.auto_free_space_acknowledgement_required
      ?? policy.acknowledgement_required
      ?? autoCleanup.acknowledgement_required
      ?? (autoFreeAcknowledgedVersion !== autoFreeTermsVersion)
  );
  const topHealth = storageTopHealthModel({ operations, pathHealth, capacity, policy, reconciliation, retention }, language);
  const tone = topHealth.tone || healthTone(operations, pathHealth, capacity, policy, reconciliation);
  const accessRights = accessRightsModel(pathHealth, language);
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
  const manageSettingsPermission = actionPermissionState(currentUser, "manage_settings", language);
  const manageCamerasPermission = actionPermissionState(currentUser, "manage_cameras", language);
  const retentionPermission = actionPermissionState(currentUser, "delete_recordings", language);
  const diagnosticsPermission = actionPermissionState(currentUser, "run_diagnostics", language);
  const retentionScenario = retentionScenarioModel({
    preview: null,
    result: null,
    retention,
    permission: { allowed: true, reason: "" },
    running: rootAction.startsWith("retention-"),
  }, language);
  const reconciliationScenario = reconciliationScenarioModel({
    preview: null,
    result: null,
    reconciliation,
    canCheck: diagnosticsPermission,
    canApply: manageSettingsPermission,
    running: Boolean(reconciliation.active),
  }, language);
  const migrationScenario = migrationScenarioModel({
    preview: migrationPreview,
    result: migrationResult,
    permission: manageSettingsPermission,
    running: rootAction === "preview" || rootAction === "apply-migration",
  }, language);
  const retentionTone = operationTone(retentionScenario.status);
  const reconciliationTone = !diagnosticsPermission.allowed
    ? "neutral"
    : reconciliation.active
      ? "warning"
      : normalizedReconciliation.problemCount > 0
        ? "warning"
        : reconciliation.status === "completed"
          ? "ok"
          : "unknown";
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
    const completed = activationModel.status === "completed";
    const running = ["queued", "running"].includes(activationModel.status);
    setArchiveRootDialog({
      id: `activation-${operationId}`,
      activationOperationId: operationId,
      title: completed ? copy.activationCompletedTitle : copy.activationProgressTitle,
      message: completed ? copy.activationCompletedMessage : activationProgressStatusText(archiveRootActivation, copy),
      items: activationProgressItems(archiveRootActivation, copy),
      action: completed ? "" : (archiveRootActivationReason || copy.activationProgressHint),
      confirmLabel: recoveryRequired ? copy.activationRetryRecovery : undefined,
      cancelLabel: copy.close,
      closeLabel: copy.close,
      tone: completed ? "success" : activationProgressTone(archiveRootActivation),
      busy: running,
      dismissible: !running,
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

  useEffect(() => {
    const operationId = archiveRootDialog?.activationOperationId;
    if (!operationId || activationModel.status !== "completed") return undefined;
    const timer = window.setTimeout(() => {
      setDismissedActivationOperationId(operationId);
      try {
        window.sessionStorage.setItem(ACTIVATION_ACK_KEY, operationId);
      } catch (_) {}
      setArchiveRootDialog((current) => current?.activationOperationId === operationId ? null : current);
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [activationModel.status, archiveRootDialog?.activationOperationId]);

  useEffect(() => {
    const scanId = integrityScan?.scan_id;
    const scanStatus = String(integrityScan?.status || "");
    if (!integrityDialogOpen || !scanId || !["queued", "running", "cancel_requested", "interrupted"].includes(scanStatus)) {
      return undefined;
    }
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const next = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(scanId)}`);
        if (cancelled) return;
        setIntegrityError("");
        if (["completed", "partial"].includes(String(next.status || ""))) {
          const page = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(scanId)}/findings?limit=50`);
          if (!cancelled) {
            setIntegrityFindings(page.items || []);
            setIntegrityNextCursor(page.next_cursor || null);
            setIntegrityScan(next);
            await loadStatus({ silent: true });
          }
        } else {
          setIntegrityScan(next);
        }
      } catch (err) {
        if (!cancelled) setIntegrityError(errorDetailText(err, copy.integrityLoadFailed, language));
      } finally {
        polling = false;
      }
    };
    const timer = window.setInterval(poll, 1500);
    poll();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [copy.integrityLoadFailed, integrityDialogOpen, integrityScan?.scan_id, integrityScan?.status, language, loadStatus]);

  function requestAutoFreeSpace(nextEnabled) {
    if (!manageSettingsPermission.allowed) {
      setArchiveRootDialog({
        id: "auto-free-permission",
        title: copy.autoFreeChangeFailed,
        message: manageSettingsPermission.reason,
        closeLabel: copy.close,
        tone: "warning",
      });
      return;
    }
    if (nextEnabled && autoFreeAcknowledgementRequired) {
      if (!autoFreeTermsVersion) {
        setArchiveRootDialog({
          id: "auto-free-terms-unavailable",
          title: copy.autoFreeChangeFailed,
          message: copy.autoFreeTermsUnavailable,
          closeLabel: copy.close,
          tone: "warning",
        });
        return;
      }
      setArchiveRootDialog({
        id: "auto-free-confirm",
        title: copy.autoFreeConfirmTitle,
        message: copy.autoFreeConfirmMessage,
        items: [
          copy.autoFreeConfirmOldest,
          copy.autoFreeConfirmVolume,
          copy.autoFreeConfirmThresholds
            .replace("{trigger}", String(policy.cleanup_threshold_percent ?? 5))
            .replace("{target}", String(policy.recovery_threshold_percent ?? 9))
            .replace("{critical}", String(policy.critical_threshold_percent ?? 1)),
          copy.autoFreeConfirmAcrossCameras,
          copy.autoFreeConfirmDisable,
        ],
        detail: copy.autoFreeConfirmIrreversible,
        confirmLabel: copy.enableAutoFreeShort,
        cancelLabel: copy.cancel,
        closeLabel: copy.cancel,
        confirmTone: "danger",
        tone: "warning",
        onConfirm: () => setAutoFreeSpace(true, { acknowledge: true }),
      });
      return;
    }
    setAutoFreeSpace(nextEnabled);
  }

  async function setAutoFreeSpace(nextEnabled, { acknowledge = false } = {}) {
    setRootAction("auto-free");
    if (acknowledge) {
      setArchiveRootDialog((current) => ({
        ...(current || {}),
        busy: true,
        dismissible: false,
      }));
    }
    try {
      const requestBody = { auto_free_space_cleanup_enabled: Boolean(nextEnabled) };
      if (acknowledge) {
        requestBody.auto_free_space_acknowledgement = {
          acknowledged: true,
          terms_version: autoFreeTermsVersion,
        };
      }
      const updated = await apiFetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      });
      setSettings(updated);
      await loadStatus({ silent: true });
      setArchiveRootDialog(null);
      setOperationToast({
        id: `auto-free-${nextEnabled ? "enabled" : "disabled"}-${Date.now()}`,
        title: nextEnabled ? copy.autoFreeEnabled : copy.autoFreeDisabled,
        message: nextEnabled ? copy.autoFreeEnabledDetail : copy.autoFreeDisabledDetail,
        closeLabel: copy.close,
        tone: "success",
      });
    } catch (err) {
      setArchiveRootDialog({
        id: `auto-free-error-${Date.now()}`,
        title: copy.autoFreeChangeFailed,
        message: errorDetailText(err, copy.autoFreeChangeFailed, language),
        action: copy.autoFreeErrorAction,
        closeLabel: copy.close,
        tone: "warning",
      });
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
    try {
      await apiFetch("/storage/archive-roots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...payload, label: archiveRootFolderName.trim() || "Archive root", make_active: false, confirm: false }),
      });
      await loadStatus({ silent: true });
      setOperationToast({
        id: `root-added-${Date.now()}`,
        title: copy.rootAddedTitle,
        message: copy.rootAddedInactive || "",
        closeLabel: copy.close,
        tone: "success",
      });
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
    setArchiveRootDialog({
      id: `activation-request-${rootId}`,
      title: copy.activationProgressTitle,
      message: recovery ? copy.activationRollbackRunning : copy.activationQueued,
      action: copy.activationProgressHint,
      closeLabel: copy.close,
      tone: "warning",
      busy: true,
      dismissible: false,
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

  function showCameraProblems(row) {
    const labels = {
      missing_file: copy.cameraProblemMissingFile,
      root_unavailable: copy.cameraProblemRootUnavailable,
      root_unresolved: copy.cameraProblemRootUnresolved,
      path_outside_storage: copy.cameraProblemUnsafePath,
      invalid_path: copy.cameraProblemInvalidPath,
      not_file: copy.cameraProblemNotFile,
      metadata_unavailable: copy.cameraProblemMetadataUnavailable,
      verification_error: copy.cameraProblemVerificationError,
    };
    const details = {
      missing_file: copy.cameraProblemMissingFileDetail,
      root_unavailable: copy.cameraProblemRootUnavailableDetail,
      root_unresolved: copy.cameraProblemRootUnresolvedDetail,
      path_outside_storage: copy.cameraProblemUnsafePathDetail,
      invalid_path: copy.cameraProblemInvalidPathDetail,
      not_file: copy.cameraProblemNotFileDetail,
      metadata_unavailable: copy.cameraProblemMetadataUnavailableDetail,
      verification_error: copy.cameraProblemVerificationErrorDetail,
    };
    const reasons = Object.entries(row.problem_counts || {})
      .filter(([, count]) => Number(count || 0) > 0)
      .map(([code, count]) => ({ code, count, label: labels[code] || copy.cameraProblemOther, detail: details[code] || copy.cameraProblemOtherDetail }));
    const actions = [
      {
        id: "refresh-storage",
        label: copy.refresh,
        onClick: async () => {
          setArchiveRootDialog(null);
          await loadStatus();
        },
      },
    ];
    if (diagnosticsPermission.allowed) {
      actions.push({
        id: "check-archive",
        label: copy.integrityCheckShort,
        onClick: () => {
          setArchiveRootDialog(null);
          openIntegrityDialog();
        },
      });
    }
    setArchiveRootDialog({
      id: `camera-problems-${row.camera_id || row.camera_name}`,
      title: copy.cameraProblemDialogTitle,
      message: row.camera_name,
      summary: [{ label: copy.problems, value: String(row.problem_file_count || 0) }],
      reasons,
      detail: diagnosticsPermission.allowed ? copy.cameraProblemNextAction : copy.cameraProblemNoAutomaticFix,
      actions,
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
        setArchiveRootDialog({
          id: `root-delete-running-${root.id}`,
          title: copy.deleteArchiveRoot,
          message: copy.archiveRootDeleteRunningMessage,
          tone: "warning",
          busy: true,
          dismissible: false,
        });
        deleteRoot(root.id, newStorageOperationId("archive-root-delete"));
      },
    });
  }

  async function deleteRoot(rootId, operationId) {
    if (!rootId) return;
    setRootAction(`delete-${rootId}`);
    try {
      const result = await apiFetch(`/storage/archive-roots/${encodeURIComponent(rootId)}`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true, operation_id: operationId || null }),
      });
      const cleanupMessage = result?.cleanup_status === "completed_preserved_nonempty"
        ? copy.archiveRootDirectoryPreserved
        : result?.root_directory_removed
          ? copy.archiveRootDirectoryRemoved
          : copy.archiveRootDeleted;
      await loadStatus({ silent: true });
      setArchiveRootDialog(null);
      setOperationToast({
        id: `root-deleted-${result?.operation_id || Date.now()}`,
        title: copy.archiveRootDeletedTitle,
        message: cleanupMessage,
        closeLabel: copy.close,
        tone: "success",
      });
    } catch (err) {
      const detail = err?.detail || err?.data?.detail;
      if (detail?.status === "partial") {
        const capability = archiveRootCleanupCapabilityModel(detail);
        const partialAction = capability.canRetryNow
          ? copy.archiveRootDeletePartialAction
          : capability.shouldRefresh
            ? copy.archiveRootCleanupRefreshAction
            : capability.needsExternalFix
              ? copy.archiveRootCleanupExternalFixAction
              : copy.archiveRootCleanupNoRetryAction;
        setArchiveRootDialog({
          id: `root-delete-partial-${detail.operation_id || Date.now()}`,
          title: copy.archiveRootDeletePartialTitle,
          message: copy.archiveRootDeletePartialMessage
            .replace("{deleted}", String(detail.segments_deleted || 0))
            .replace("{remaining}", String(detail.remaining_count || 0)),
          reasons: [{
            code: "archive-root-cleanup",
            label: copy.archiveRootCleanupPending,
            detail: archiveRootCleanupReason(detail, copy),
          }],
          action: partialAction,
          closeLabel: copy.close,
          ...(capability.canRetryNow ? {
            confirmLabel: copy.archiveRootCleanupRetry,
            cancelLabel: copy.close,
            onConfirm: () => {
              setArchiveRootDialog(null);
              deleteRoot(rootId, newStorageOperationId("archive-root-delete"));
            },
          } : (capability.shouldRefresh || capability.needsExternalFix) ? {
            actions: [{
              id: "refresh-storage",
              label: copy.refresh,
              onClick: async () => {
                setArchiveRootDialog(null);
                await loadStatus();
              },
            }],
          } : {}),
          tone: "error",
        });
      } else {
        setArchiveRootDialog({
          id: `root-delete-error-${Date.now()}`,
          title: copy.archiveRootNotDeleted,
          message: errorDetailText(err, copy.archiveRootNotDeleted, language),
          closeLabel: copy.close,
          tone: "error",
        });
      }
      await loadStatus({ silent: true });
    } finally {
      setRootAction("");
    }
  }

  function closeArchiveRootDialog() {
    if (archiveRootDialog?.activationOperationId) {
      setDismissedActivationOperationId(archiveRootDialog.activationOperationId);
      try {
        window.sessionStorage.setItem(ACTIVATION_ACK_KEY, archiveRootDialog.activationOperationId);
      } catch (_) {}
    }
    setArchiveRootDialog(null);
  }

  async function loadIntegrityFindingPage(scanId, cursor = null, { append = false } = {}) {
    if (!scanId) return;
    const query = new URLSearchParams({ limit: "50" });
    if (cursor) query.set("cursor", cursor);
    const page = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(scanId)}/findings?${query.toString()}`);
    setIntegrityFindings((current) => append ? [...current, ...(page.items || [])] : (page.items || []));
    setIntegrityNextCursor(page.next_cursor || null);
  }

  async function openIntegrityDialog() {
    setIntegrityDialogOpen(true);
    setIntegrityError("");
    if (!diagnosticsPermission.allowed) return;
    setIntegrityBusy(true);
    try {
      const latest = await apiFetch("/storage/integrity/scans/latest");
      setIntegrityScan(latest);
      if (["completed", "partial"].includes(String(latest.status || "")) && latest.scan_id) {
        await loadIntegrityFindingPage(latest.scan_id);
      } else {
        setIntegrityFindings([]);
        setIntegrityNextCursor(null);
      }
    } catch (err) {
      setIntegrityError(errorDetailText(err, copy.integrityLoadFailed, language));
    } finally {
      setIntegrityBusy(false);
    }
  }

  async function startIntegrityScan() {
    if (!diagnosticsPermission.allowed) {
      setIntegrityError(diagnosticsPermission.reason);
      return;
    }
    setIntegrityBusy(true);
    setIntegrityError("");
    setIntegrityFindings([]);
    setIntegrityNextCursor(null);
    setIntegritySelectedFinding(null);
    setIntegrityPlan(null);
    setIntegrityConfirmed(false);
    try {
      const next = await apiFetch("/storage/integrity/scans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: newStorageOperationId("integrity-scan") }),
      });
      setIntegrityScan(next);
      if (["completed", "partial"].includes(String(next.status || "")) && next.scan_id) {
        await loadIntegrityFindingPage(next.scan_id);
      }
      await loadStatus({ silent: true });
    } catch (err) {
      setIntegrityError(errorDetailText(err, copy.integrityStartFailed, language));
    } finally {
      setIntegrityBusy(false);
    }
  }

  async function cancelIntegrityScan() {
    if (!integrityScan?.scan_id) return;
    setIntegrityBusy(true);
    setIntegrityError("");
    try {
      const next = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(integrityScan.scan_id)}/cancel`, { method: "POST" });
      setIntegrityScan(next);
    } catch (err) {
      setIntegrityError(errorDetailText(err, copy.integrityCancelFailed, language));
    } finally {
      setIntegrityBusy(false);
    }
  }

  async function loadMoreIntegrityFindings() {
    if (!integrityScan?.scan_id || !integrityNextCursor) return;
    setIntegrityBusy(true);
    setIntegrityError("");
    try {
      await loadIntegrityFindingPage(integrityScan.scan_id, integrityNextCursor, { append: true });
    } catch (err) {
      setIntegrityError(errorDetailText(err, copy.integrityLoadFailed, language));
    } finally {
      setIntegrityBusy(false);
    }
  }

  async function prepareIntegrityAction(finding) {
    const presentation = archiveIntegrityFindingPresentation(finding);
    const contract = archiveIntegrityActionContract(presentation.actionKey);
    if (!presentation.actionAllowed || !contract.planKind) return;
    setIntegrityBusy(true);
    setIntegrityError("");
    setIntegritySelectedFinding(finding);
    setIntegrityPlan(null);
    setIntegrityConfirmed(false);
    try {
      const plan = await apiFetch(`/storage/integrity/findings/${encodeURIComponent(finding.finding_id)}/${contract.planKind}-plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action_key: presentation.actionKey,
          idempotency_key: newStorageOperationId("integrity-plan"),
        }),
      });
      setIntegrityPlan(plan);
    } catch (err) {
      setIntegritySelectedFinding(null);
      setIntegrityError(errorDetailText(err, copy.integrityPlanFailed, language));
    } finally {
      setIntegrityBusy(false);
    }
  }

  async function applyIntegrityAction() {
    if (!integrityPlan?.plan_id || !integrityConfirmed || !integritySelectedFinding) return;
    const presentation = archiveIntegrityFindingPresentation(integritySelectedFinding);
    const contract = archiveIntegrityActionContract(presentation.actionKey);
    if (!contract.planKind) return;
    setIntegrityBusy(true);
    setIntegrityError("");
    try {
      const result = await apiFetch(`/storage/integrity/remediation-plans/${encodeURIComponent(integrityPlan.plan_id)}/apply-${contract.planKind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirm: true,
          operation_id: newStorageOperationId("integrity-apply"),
        }),
      });
      setIntegrityPlan(result);
      setIntegrityConfirmed(false);
      const latest = await apiFetch("/storage/integrity/scans/latest");
      setIntegrityScan(latest);
      if (latest.scan_id) await loadIntegrityFindingPage(latest.scan_id);
      await loadStatus({ silent: true });
    } catch (err) {
      setIntegrityError(errorDetailText(err, copy.integrityApplyFailed, language));
    } finally {
      setIntegrityBusy(false);
    }
  }

  async function clearIntegrityPlan() {
    setIntegritySelectedFinding(null);
    setIntegrityPlan(null);
    setIntegrityConfirmed(false);
    setIntegrityError("");
    if (integrityScan?.scan_id && ["completed", "partial"].includes(String(integrityScan.status || ""))) {
      try {
        await loadIntegrityFindingPage(integrityScan.scan_id);
      } catch (err) {
        setIntegrityError(errorDetailText(err, copy.integrityLoadFailed, language));
      }
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

              <Section title={copy.cameras} className="storageOpsSection-cameras">
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
                              <td>{row.camera_name}</td>
                              <td>{formatBytes(row.size_bytes)}</td>
                              <td>{row.segment_count}</td>
                              <td>
                                {row.problem_file_count > 0 ? (
                                  <button className="storageOpsCameraProblemButton" type="button" onClick={() => showCameraProblems(row)} aria-label={`${copy.problems}: ${row.problem_file_count}`}>
                                    {row.problem_file_count}
                                  </button>
                                ) : "0"}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="storageOpsCameraCards">
                      {cameraRows.map((row) => (
                        <div className="storageOpsCameraCard" key={`card-${row.camera_id || row.camera_name}`}>
                          <div className="storageOpsCameraCardIdentity"><strong>{row.camera_name}</strong></div>
                          <div className="storageOpsCameraCardMetric"><span>{copy.size}</span><strong>{formatBytes(row.size_bytes)}</strong></div>
                          <div className="storageOpsCameraCardMetric"><span>{copy.segments}</span><strong>{row.segment_count}</strong></div>
                          <div className="storageOpsCameraCardMetric">
                            <span>{copy.problems}</span>
                            {row.problem_file_count > 0 ? (
                              <button className="storageOpsCameraProblemButton" type="button" onClick={() => showCameraProblems(row)}>{row.problem_file_count}</button>
                            ) : <strong>0</strong>}
                          </div>
                        </div>
                      ))}
                    </div>
                  </>
                ) : <div className="storageOpsEmpty">{copy.noCameraOwned}</div>}
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
                    actions={(
                      manageCamerasPermission.allowed
                        ? <a className="button secondary small" href="/cameras">{copy.configureCameras}</a>
                        : <button className="button secondary small" type="button" disabled title={manageCamerasPermission.reason}>{copy.configureCameras}</button>
                    )}
                  >
                    <details className="storageOpsInlineDetails">
                      <summary>{copy.supportDetails}</summary>
                      <SummaryRow label={copy.retentionScope} value={copy.retentionScopeValue
                        .replace("{active}", String(retention.active_camera_count || 0))
                        .replace("{disabled}", String(retention.disabled_camera_count || 0))
                        .replace("{retained}", String(retention.retained_deleted_camera_count || 0))} />
                      <SummaryRow label={copy.retentionRulesMissing} value={String(retention.missing_or_invalid_rule_camera_count || 0)} />
                      <SummaryRow label={copy.nextCheck} value={formatDateTime(retention.next_due_at, language)} />
                      <SummaryRow label={copy.blockersReasons} value={operationReasonText(retention, copy, language)} />
                      {!manageCamerasPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageCamerasPermission.reason}</div> : null}
                    </details>
                  </OperationRow>

                  <OperationRow
                    title={copy.autoFreeSpace}
                    status={autoFreeConfigured && autoFreeAcknowledgementRequired ? copy.confirmationRequired : autoFreeEnabled ? copy.on : copy.off}
                    tone={autoFreeConfigured && autoFreeAcknowledgementRequired ? "warning" : autoFreeEnabled ? "ok" : "neutral"}
                    description={autoFreePrimaryText}
                    meta={(
                      <div className="storageOpsOperationFacts">
                        <MiniFact label={copy.lastRun} value={formatDateTime(autoCleanup.last_finished_at || autoCleanup.last_started_at, language)} />
                        <MiniFact label={copy.deleted} value={String(autoCleanup.last_summary?.deleted_count || 0)} />
                        <MiniFact label={copy.freed} value={formatBytes(autoCleanup.last_summary?.bytes_freed)} />
                      </div>
                    )}
                    actions={(
                      <>
                        <button className="button secondary small" type="button" title={autoFreeEnabled ? copy.disableAutoFree : copy.enableAutoFree} onClick={() => requestAutoFreeSpace(!autoFreeEnabled)} disabled={!!rootAction || !manageSettingsPermission.allowed}>
                          {rootAction === "auto-free" ? copy.saving : autoFreeEnabled ? copy.disableAutoFreeShort : copy.enableAutoFreeShort}
                        </button>
                        {autoFreeConfigured && autoFreeAcknowledgementRequired ? (
                          <button className="button secondary small" type="button" title={copy.disableAutoFree} onClick={() => requestAutoFreeSpace(false)} disabled={!!rootAction || !manageSettingsPermission.allowed}>
                            {copy.disableAutoFreeShort}
                          </button>
                        ) : null}
                      </>
                    )}
                  >
                    <details className="storageOpsInlineDetails">
                      <summary>{copy.supportDetails}</summary>
                      <SummaryRow label={copy.policy} value={copy.autoFreeThresholdSummary
                        .replace("{trigger}", String(policy.cleanup_threshold_percent ?? 5))
                        .replace("{target}", String(policy.recovery_threshold_percent ?? 9))
                        .replace("{critical}", String(policy.critical_threshold_percent ?? 1))} />
                      <SummaryRow label={copy.lastError} value={operationReasonText(autoCleanup, copy, language)} />
                      {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                    </details>
                  </OperationRow>

                  <OperationRow
                    title={copy.archiveProblems}
                    status={archiveProblemsStatusText(normalizedReconciliation, reconciliation, copy)}
                    tone={reconciliationTone}
                    description={integrityPrimaryText}
                    meta={(
                      <div className="storageOpsOperationFacts">
                        <MiniFact label={copy.problems} value={String(normalizedReconciliation.problemCount || 0)} tone={normalizedReconciliation.problemCount ? "warning" : "ok"} />
                        <MiniFact label={copy.integrityChecked} value={String(reconciliation.checked_count || 0)} />
                        <MiniFact label={copy.integrityFailedItems} value={String(reconciliation.failed_count || 0)} tone={reconciliation.failed_count ? "warning" : "neutral"} />
                        <MiniFact label={copy.lastCheck} value={formatDateTime(reconciliation.last_checked_at, language)} />
                      </div>
                    )}
                    actions={(
                      <button className="button secondary small" type="button" title={copy.integrityOpenCheck} onClick={openIntegrityDialog} disabled={!!rootAction}>{copy.integrityOpenCheck}</button>
                    )}
                  />

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

              {recent.available && recentOperationRows.length ? (
                <details className="storageOpsSection storageOpsSection-secondary storageOpsSection-recent">
                  <summary className="storageOpsSectionHead storageOpsRecentSummary">
                    <h2>{copy.recentOperations}</h2>
                    <span
                      className="storageOpsRecentCount"
                      aria-label={t("storagePage.recentOperationsCount", { count: recentOperationRows.length })}
                    >
                      {recentOperationRows.length}
                    </span>
                  </summary>
                  <div className="storageOpsRecent">
                    {recentOperationRows.map((item) => (
                      <div className="storageOpsRecentItem" key={item.key}>
                        <div className="storageOpsRecentPrimary">
                          <span className="storageOpsRecentTitle">{copy[item.typeKey] || copy.recentOperationGeneric}</span>
                          <span className={`storageOpsStatusPill storageOpsStatusPill-${item.tone}`}>
                            {copy[item.statusKey] || copy.recentOperationStatusUnknown}
                          </span>
                          {item.timestamp ? (
                            <time dateTime={item.timestamp}>{formatDateTime(item.timestamp, language)}</time>
                          ) : null}
                        </div>
                        {item.facts.length ? (
                          <div className="storageOpsRecentFacts">
                            {item.facts.map((fact) => (
                              <span key={fact.labelKey}>
                                {copy[fact.labelKey]}: {fact.format === "bytes" ? formatBytes(fact.value) : fact.value}
                              </span>
                            ))}
                          </div>
                        ) : null}
                        {item.reasonCode ? (
                          <div className="storageOpsRecentMessage">
                            <span>{copy.recentOperationReason}:</span> {humanBlockerReason(item.reasonCode, language)}
                          </div>
                        ) : null}
                        {item.nextActionKey ? (
                          <div className="storageOpsRecentMessage">
                            <span>{copy.recentOperationNextAction}:</span> {copy[item.nextActionKey]}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </details>
              ) : null}
            </div>

          </>
        )}
        <ArchiveIntegrityDialog
          open={integrityDialogOpen}
          scan={integrityScan}
          findings={integrityFindings}
          nextCursor={integrityNextCursor}
          busy={integrityBusy}
          error={integrityError}
          selectedFinding={integritySelectedFinding}
          plan={integrityPlan}
          confirmed={integrityConfirmed}
          copy={copy}
          language={language}
          permission={diagnosticsPermission}
          onClose={() => setIntegrityDialogOpen(false)}
          onStart={startIntegrityScan}
          onCancel={cancelIntegrityScan}
          onLoadMore={loadMoreIntegrityFindings}
          onPrepare={prepareIntegrityAction}
          onClearPlan={clearIntegrityPlan}
          onConfirmChange={setIntegrityConfirmed}
          onApply={applyIntegrityAction}
        />
        <OperationDialog dialog={archiveRootDialog} onClose={closeArchiveRootDialog} />
        <OperationToast toast={operationToast} onClose={() => setOperationToast(null)} />
      </div>
    </Layout>
  );
}
