"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Layout from "../../components/Layout";
import { OperationDialog, OperationToast, useModalBodyScrollLock } from "../../components/OperationFeedback";
import {
  ArchiveManagementCenter,
  ArchiveOperationHistoryContent,
  ArchivePolicySwitch,
} from "../../components/storage/ArchiveManagementCenter";
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
  archiveIntegrityActionContract,
  archiveIntegrityCategoryPresentations,
  archiveIntegrityFindingPresentation,
  archiveIntegrityScanModel,
  autoFreeOperationPresentation,
  archiveRootCleanupCapabilityModel,
  archiveRootScenarioModel,
  freeSpaceTone,
  integrityOperationPresentation,
  migrationScenarioModel,
  migrationOperationPresentation,
  normalizeReconciliationSummary,
  publishStorageMigrationActivity,
  retentionOperationPresentation,
  statusLabel,
  storageTopHealthModel,
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

function TopMetric({ label, value, tone = "neutral", onValueClick = null, actionLabel = "", disabled = false }) {
  return (
    <div className={`storageOpsTopMetric storageOpsTopMetric-${tone}`}>
      <span>{label}</span>
      {onValueClick ? (
        <button
          type="button"
          className="storageOpsTopMetricValueButton"
          onClick={onValueClick}
          disabled={disabled}
          title={actionLabel}
          aria-label={actionLabel}
        >
          {value}
        </button>
      ) : <strong>{value}</strong>}
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

const INTEGRITY_ACTION_ICONS = Object.freeze({
  retire_missing_recording: "/assets/icons/ui/retire-missing-recording.svg",
  delete_unusable_recording: "/assets/icons/ui/delete-recording.svg",
  delete_proven_orphan: "/assets/icons/ui/delete-orphan-file.svg",
});

function dialogFocusableElements(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  )).filter((element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true");
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
  copy,
  language,
  permission,
  onClose,
  onStart,
  onCancel,
  onLoadMore,
  onPrepare,
  onClearPlan,
  onApply,
}) {
  const dialogRef = useRef(null);
  const confirmationRef = useRef(null);
  const closeRef = useRef(null);
  const returnFocusRef = useRef(null);
  useModalBodyScrollLock(open);
  const scanModel = archiveIntegrityScanModel(scan || {}, permission);
  const categories = archiveIntegrityCategoryPresentations(scanModel.stale ? {} : scan?.category_counts);
  const findingRows = findings.map(archiveIntegrityFindingPresentation);
  const selectedRow = selectedFinding ? archiveIntegrityFindingPresentation(selectedFinding) : null;
  const actionContract = archiveIntegrityActionContract(selectedRow?.actionKey);
  const resultState = plan && ["completed", "partial", "blocked", "failed", "cancelled"].includes(String(plan.state || ""))
    ? String(plan.state)
    : null;

  useEffect(() => {
    if (!open) return undefined;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => {
      if (returnFocusRef.current?.isConnected) returnFocusRef.current.focus({ preventScroll: true });
      returnFocusRef.current = null;
    };
  }, [open]);

  useEffect(() => {
    if (!open || plan) return undefined;
    const timer = window.setTimeout(() => {
      const activeElement = document.activeElement;
      const activeInsideDialog = activeElement instanceof HTMLElement && dialogRef.current?.contains(activeElement);
      if (!busy && activeInsideDialog && activeElement !== dialogRef.current) return;
      const target = busy ? dialogRef.current : dialogFocusableElements(dialogRef.current)[0] || dialogRef.current;
      target?.focus({ preventScroll: true });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open, busy, plan]);

  useEffect(() => {
    if (!open || !plan) return undefined;
    const timer = window.setTimeout(() => {
      const target = dialogFocusableElements(confirmationRef.current)[0] || confirmationRef.current;
      target?.focus({ preventScroll: true });
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open, plan, busy]);

  if (!open) return null;

  function handleKeyDown(event) {
    if (event.key === "Escape" && !busy) {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const elements = dialogFocusableElements(dialogRef.current);
    if (!elements.length) {
      event.preventDefault();
      dialogRef.current?.focus();
      return;
    }
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function handleConfirmationKeyDown(event) {
    if (event.key === "Escape" && !busy) {
      event.preventDefault();
      onClearPlan();
      return;
    }
    if (event.key !== "Tab") return;
    const elements = dialogFocusableElements(confirmationRef.current);
    if (!elements.length) {
      event.preventDefault();
      confirmationRef.current?.focus();
      return;
    }
    const first = elements[0];
    const last = elements[elements.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  const checkedTime = scan?.finished_at || scan?.started_at || scan?.created_at;
  const canShowFindings = ["completed", "partial"].includes(scanModel.status) && scanModel.found > 0;
  const primaryStartLabel = scanModel.status === "not_run" ? copy.integrityCheckArchive : copy.integrityCheckAgain;
  const resultTone = resultState === "completed" ? "ok" : resultState === "failed" ? "error" : "warning";
  const scanDetail = String(copy[scanModel.detailKey] || copy.integrityScanFailedText)
    .replace("{count}", String(scanModel.found));

  return (
    <>
    <div className="storageIntegrityOverlay" role="presentation" aria-hidden={plan ? "true" : undefined}>
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
                <p>{scanModel.stale ? copy.integrityScanStaleText : scanDetail}</p>
              </div>
              {checkedTime ? <time dateTime={checkedTime}>{formatDateTime(checkedTime, language)}</time> : null}
            </div>
            {scanModel.running ? (
              <>
                <div className={`storageIntegrityProgress ${scanModel.progressIndeterminate ? "isIndeterminate" : ""}`} aria-label={copy.integrityProgress}>
                  <span style={scanModel.progressIndeterminate ? undefined : { width: `${scanModel.percent}%` }} />
                </div>
                <div className="storageIntegrityProgressFacts">
                  <span>{copy.integrityPhase}: {copy[scanModel.phaseKey] || copy.integrityPhaseChecking}</span>
                  {scanModel.progressIndeterminate
                    ? <span>{copy.integrityFilesystemChecked}: {scanModel.filesystemChecked}</span>
                    : <span>{copy.integrityChecked}: {scanModel.metadataChecked}{scanModel.planned ? ` / ${scanModel.planned}` : ""}</span>}
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
                      <span>{copy[item.detailKey] || copy[item.impactKey] || copy.integrityImpactUnknown}</span>
                    </div>
                    <dl>
                      {item.cameraName ? <div><dt>{copy.camera}</dt><dd>{item.cameraName}</dd></div> : null}
                      {item.rootLabel ? <div><dt>{copy.integrityRootLabel}</dt><dd>{item.rootLabel}</dd></div> : null}
                      {item.displayName ? <div><dt>{copy.integrityFileLabel}</dt><dd>{item.displayName}</dd></div> : null}
                    </dl>
                    <div className="storageIntegrityFindingAction">
                      {item.actionAllowed && !item.stale ? (
                        <button className={`button secondary small appIllustratedAction storageIntegrityIconAction settingsInfoTip ${item.destructive ? "isDestructive" : ""}`} type="button" onClick={() => onPrepare(findings[index])} disabled={busy || scanModel.stale} title={copy[item.actionLabelKey]} aria-label={copy[item.actionLabelKey]}>
                          <img src={INTEGRITY_ACTION_ICONS[item.actionKey] || "/assets/icons/ui/open.png"} alt="" aria-hidden="true" />
                          <span className="settingsInfoBubble" role="tooltip">{copy[item.actionLabelKey]}</span>
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

        </div>

        <footer className="storageIntegrityDialogFooter">
          <button className="button secondary small" type="button" onClick={onClose} disabled={busy}>{copy.close}</button>
          <div className="storageIntegrityDialogPrimaryActions">
            {scanModel.canCancel ? <button className="button secondary small" type="button" onClick={onCancel} disabled={busy}>{copy.integrityCancelScan}</button> : null}
            {scanModel.canStart ? <button className="button small" type="button" onClick={onStart} disabled={busy || !permission.allowed}>{busy ? copy.checking : primaryStartLabel}</button> : null}
          </div>
        </footer>
      </section>
    </div>
    {selectedRow && plan ? (
      <div className="storageIntegrityConfirmationOverlay" role="presentation">
        <section
          ref={confirmationRef}
          className={`storageIntegrityConfirmation storageIntegrityPlan-${resultTone}`}
          role="dialog"
          aria-modal="true"
          aria-labelledby="storage-integrity-confirmation-title"
          tabIndex={-1}
          onKeyDown={handleConfirmationKeyDown}
        >
          <header>
            <h3 id="storage-integrity-confirmation-title">{resultState ? copy.integrityResultTitle : copy.integrityConfirmationTitle}</h3>
            <button className="storageIntegrityClose" type="button" onClick={onClearPlan} disabled={busy} aria-label={copy.close}>×</button>
          </header>
          <strong>{copy[selectedRow.categoryKey] || copy.integrityCategoryUnknown}</strong>
          {selectedRow.cameraName ? <span>{selectedRow.cameraName}</span> : null}
          {selectedRow.displayName ? <span>{selectedRow.displayName}</span> : null}
          <p>{resultState
            ? copy[`integrityResult${resultState.charAt(0).toUpperCase()}${resultState.slice(1)}`] || copy.integrityResultFailed
            : copy[actionContract.confirmationKey] || copy.integrityNoActionUnavailable}</p>
          {error ? <div className="storageIntegrityMessage storageIntegrityMessage-error" role="alert">{error}</div> : null}
          <footer>
            {!resultState ? (
              <>
                <button className="button secondary small" type="button" onClick={onClearPlan} disabled={busy}>{copy.cancel}</button>
                <button className={`button small ${actionContract.destructive ? "dangerButton" : ""}`} type="button" onClick={onApply} disabled={busy}>
                  {busy ? copy.applying : copy[selectedRow.actionLabelKey] || copy.integrityApplyAction}
                </button>
              </>
            ) : (
              <button className="button secondary small" type="button" onClick={onClearPlan}>{copy.integrityAcknowledgeResult}</button>
            )}
          </footer>
        </section>
      </div>
    ) : null}
    </>
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
  const candidate = typeof detail === "string" ? detail : String(error?.message || "");
  const normalized = candidate.trim();
  if (!normalized || normalized.length > 320) return fallback;
  if (/^(http\s+\d+|[a-z0-9]+(?:[_.:-][a-z0-9]+)+)$/i.test(normalized)) return fallback;
  if (/[{}\[\]\\]|\/(?:api|storage|volume|app)\//i.test(normalized)) return fallback;
  return normalized;
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

function archiveMigrationStatusText(scenario, archiveRoots, copy) {
  if (archiveRoots.length < 2) return copy.migrationNeedsTargetStatus;
  const labels = {
    building: copy.migrationStatusPreparing,
    ready: copy.migrationPlanReadyStatus,
    ready_with_exclusions: copy.migrationPlanReadyWithExclusions,
    queued: copy.migrationStatusQueued,
    running: copy.migrationStatusRunning,
    cancel_requested: copy.migrationStatusCancelRequested,
    completed: copy.migrationStatusCompleted,
    partial: copy.migrationStatusPartial,
    failed: copy.migrationStatusFailed,
    blocked: copy.migrationStatusBlocked,
    cancelled: copy.migrationStatusCancelled,
    expired: copy.migrationStatusExpired,
    interrupted: copy.migrationStatusInterrupted,
    unknown: copy.migrationUnknownValue,
  };
  return labels[scenario.status] || copy.migrationChooseTargetStatus;
}

function copyStatus(copy, prefix, status, fallback = "archiveManagementStatusUnknown") {
  return copy[`${prefix}${String(status || "unknown").replace(/(^|_)([a-z])/g, (_, _separator, letter) => letter.toUpperCase())}`]
    || copy[fallback];
}

function retentionManagementDescription(model, copy, language) {
  if (model.status === "not_configured") return copy.archiveManagementRetentionNotConfiguredText;
  if (model.status === "incomplete") {
    return copy.archiveManagementRetentionIncompleteText
      .replace("{configured}", String(model.configuredCount))
      .replace("{total}", String(model.totalCameraCount));
  }
  if (model.status === "running") return copy.archiveManagementRetentionRunningText;
  if (model.status === "pending") return copy.archiveManagementRetentionPendingText;
  if (model.status === "needs_attention") {
    return model.last.reasonCode ? humanBlockerReason(model.last.reasonCode, language) : copy.archiveManagementRetentionAttentionText;
  }
  if (model.status === "unknown") return copy.archiveManagementRetentionUnknownText;
  return copy.archiveManagementRetentionHealthyText.replace("{count}", String(model.configuredCount));
}

function autoFreeManagementDescription(model, copy, language) {
  if (model.status === "acknowledgement_required") return copy.archiveManagementAutoFreeAcknowledgementText;
  if (model.status === "disabled") return copy.archiveManagementAutoFreeDisabledText;
  if (model.status === "critical") return copy.archiveManagementAutoFreeCriticalText;
  if (model.status === "cleanup") return copy.archiveManagementAutoFreeCleanupText;
  if (model.status === "recovery") return copy.archiveManagementAutoFreeRecoveryText;
  if (model.status === "warning") return copy.archiveManagementAutoFreeWarningText;
  if (model.status === "failed") {
    return model.last.reasonCode ? humanBlockerReason(model.last.reasonCode, language) : copy.archiveManagementAutoFreeFailedText;
  }
  if (model.status === "unknown") return copy.archiveManagementAutoFreeUnknownText;
  return copy.archiveManagementAutoFreeEnabledText
    .replace("{warning}", String(model.warningPercent))
    .replace("{cleanup}", String(model.cleanupPercent))
    .replace("{target}", String(model.recoveryPercent))
    .replace("{critical}", String(model.criticalPercent));
}

function integrityManagementDescription(model, copy) {
  if (model.status === "clean") return copy.archiveManagementIntegrityCleanText;
  if (model.status === "findings") return copy.archiveManagementIntegrityFindingsText.replace("{count}", String(model.problemCount));
  if (model.status === "stale") return copy.archiveManagementIntegrityStaleText.replace("{count}", String(model.problemCount));
  if (model.status === "running") return copy.archiveManagementIntegrityRunningText;
  if (model.status === "cancel_requested") return copy.archiveManagementIntegrityCancelText;
  if (["partial", "failed", "interrupted", "cancelled"].includes(model.status)) return copy.archiveManagementIntegrityIncompleteText;
  if (model.status === "unknown") return copy.archiveManagementIntegrityUnknownText;
  return copy.archiveManagementIntegrityNotRunText;
}

function migrationManagementDescription(model, scenario, copy) {
  if (model.status === "needs_target") return copy.migrationNeedsTarget;
  if (model.status === "running" || model.status === "cancel_requested") return copy.migrationBackgroundActive;
  if (model.status === "completed") return copy.migrationCompletedSelectedPlan;
  if (model.status === "needs_attention") return scenario.reason || copy.migrationNeedsAttention;
  if (model.status === "cancelled") return copy.archiveManagementMigrationCancelledText;
  if (["ready", "ready_with_exclusions", "building"].includes(model.status)) return copy.migrationPlanReady;
  if (model.status === "unknown") return copy.archiveManagementMigrationUnknownText;
  return copy.archiveManagementMigrationIdleText;
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

function migrationPhaseText(phase, copy) {
  const labels = {
    inventory: copy.migrationPhaseInventory,
    building: copy.migrationPhaseInventory,
    queued: copy.migrationPhaseQueued,
    target_temp_create_pending: copy.migrationPhasePreparingTarget,
    copying: copy.migrationPhaseCopying,
    target_temp_written: copy.migrationPhaseVerifying,
    target_verified: copy.migrationPhaseFinalizing,
    target_finalized: copy.migrationPhaseMetadata,
    metadata_switched: copy.migrationPhaseCleanup,
    source_cleanup_pending: copy.migrationPhaseCleanup,
    source_quarantined: copy.migrationPhaseCleanup,
    source_delete_committing: copy.migrationPhaseCleanup,
    cancel_requested: copy.migrationPhaseCancelRequested,
    completed: copy.migrationStatusCompleted,
    partial: copy.migrationStatusPartial,
    failed: copy.migrationStatusFailed,
    blocked: copy.migrationStatusBlocked,
    cancelled: copy.migrationStatusCancelled,
    expired: copy.migrationStatusExpired,
  };
  return labels[String(phase || "")] || copy.migrationPhaseUnknown;
}

function migrationEtaText(seconds, copy) {
  if (seconds === null || seconds === undefined || seconds === "") return copy.migrationCalculating;
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return copy.migrationCalculating;
  const rounded = Math.ceil(value);
  if (rounded >= 86400) return copy.migrationEtaDays.replace("{value}", String(Math.ceil(rounded / 86400)));
  if (rounded >= 3600) return copy.migrationEtaHours.replace("{value}", String(Math.ceil(rounded / 3600)));
  if (rounded >= 60) return copy.migrationEtaMinutes.replace("{value}", String(Math.ceil(rounded / 60)));
  return copy.migrationEtaSeconds.replace("{value}", String(rounded));
}

function migrationNextActionText(value, copy) {
  const labels = {
    refresh: copy.migrationNextRefreshPlan,
    refresh_plan: copy.migrationNextRefreshPlan,
    cleanup_only: copy.migrationNextRetryCleanup,
    after_free_space: copy.migrationNextFreeSpace,
    after_permission_restore: copy.migrationNextRestorePermission,
    retry: copy.migrationNextRetry,
  };
  return labels[String(value || "")] || copy.migrationNextReview;
}

function ArchiveMigrationDialog({
  open,
  roots,
  sourceRootId,
  targetRootId,
  plan,
  operation,
  scenario,
  busy,
  error,
  copy,
  language,
  onClose,
  onSourceChange,
  onTargetChange,
  onPrepare,
  onApply,
  onCancel,
  onRetry,
  onCleanupTakeover,
  onReset,
}) {
  if (!open) return <OperationDialog dialog={null} onClose={onClose} />;
  const source = roots.find((root) => String(root.id) === String(sourceRootId));
  const target = roots.find((root) => String(root.id) === String(targetRootId));
  const excluded = Object.entries(plan?.excluded_summary || {}).filter(([, count]) => Number(count) > 0).slice(0, 8);
  const blocked = Object.entries(plan?.blocker_summary || {}).filter(([, count]) => Number(count) > 0).slice(0, 8);
  const progressKnown = scenario.percent !== null;
  const selectedDifferent = Boolean(sourceRootId && targetRootId && String(sourceRootId) !== String(targetRootId));
  const draft = !scenario.planId && !scenario.operationId;
  const terminal = scenario.terminal || scenario.status === "interrupted";
  const facts = [
    { label: copy.migrationFiles, value: scenario.itemCount === null ? copy.migrationUnknownValue : String(scenario.itemCount) },
    { label: copy.migrationVolume, value: scenario.totalBytes === null ? copy.migrationUnknownValue : formatBytes(scenario.totalBytes) },
    { label: copy.migrationExcluded, value: scenario.excludedCount === null ? copy.migrationUnknownValue : String(scenario.excludedCount) },
  ];
  const actions = [];
  if (scenario.canCancel) actions.push({ id: "migration-cancel", label: copy.migrationCancel, onClick: onCancel });
  if (terminal && scenario.canRetry) actions.push({ id: "migration-retry", label: scenario.retryMode === "cleanup_only" ? copy.migrationRetryCleanup : copy.migrationRetry, onClick: onRetry });
  if (terminal && scenario.canCleanupTakeover) actions.push({ id: "migration-cleanup-takeover", label: copy.migrationCleanupTakeover, onClick: onCleanupTakeover });
  if (terminal) actions.push({ id: "migration-new-plan", label: copy.migrationNewPlan, onClick: onReset });
  const canPrepare = draft && roots.length > 1 && selectedDifferent && scenario.canPrepare;
  const hasConfirm = canPrepare || scenario.canApply;

  const content = (
    <div className="archiveMigrationWizard">
      <div className="archiveMigrationSteps" aria-label={copy.migrationStepsLabel}>
        <span className={draft ? "active" : "done"}>1. {copy.migrationStepSelect}</span>
        <span className={scenario.status === "building" ? "active" : scenario.planId ? "done" : ""}>2. {copy.migrationStepPlan}</span>
        <span className={scenario.ready ? "active" : scenario.operationId ? "done" : ""}>3. {copy.migrationStepConfirm}</span>
        <span className={scenario.active || terminal ? "active" : ""}>4. {copy.migrationStepProgress}</span>
      </div>

      <div className="archiveMigrationFields">
        <label>
          <span>{copy.migrationSource}</span>
          <select className="select" value={sourceRootId} onChange={(event) => onSourceChange(event.target.value)} disabled={!draft || busy || scenario.active}>
            <option value="">{copy.migrationChooseSource}</option>
            {roots.map((root) => <option key={root.id} value={root.id}>{archiveRootLabel(root, copy)} - {archiveRootPath(root)}</option>)}
          </select>
        </label>
        <label>
          <span>{copy.migrationTarget}</span>
          <select className="select" value={targetRootId} onChange={(event) => onTargetChange(event.target.value)} disabled={!draft || busy || scenario.active}>
            <option value="">{copy.migrationChooseTarget}</option>
            {roots.map((root) => <option key={root.id} value={root.id} disabled={String(root.id) === String(sourceRootId)}>{archiveRootLabel(root, copy)} - {archiveRootPath(root)}</option>)}
          </select>
        </label>
      </div>
      <div className="archiveMigrationHint">{copy.migrationActivationDifference}</div>

      {scenario.planId ? (
        <>
          <dl className="archiveMigrationFacts">
            {facts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}
          </dl>
          <div className="archiveMigrationCapacity">
            <strong>{scenario.samePhysicalVolume === null ? copy.migrationCapacityUnchecked : scenario.samePhysicalVolume ? copy.migrationSameVolume : copy.migrationDifferentVolume}</strong>
            <span>{copy.migrationFree}: {plan?.capacity_free_bytes == null ? copy.migrationUnknownValue : formatBytes(plan.capacity_free_bytes)}</span>
            <span>{copy.migrationRequired}: {plan?.required_free_bytes == null ? copy.migrationUnknownValue : formatBytes(plan.required_free_bytes)}</span>
            <span>{copy.migrationReserve}: {plan?.reserve_bytes == null ? copy.migrationUnknownValue : formatBytes(plan.reserve_bytes)}</span>
          </div>
          {plan?.expires_at && scenario.ready ? <div className="archiveMigrationHint">{copy.migrationPlanValidUntil}: {formatDateTime(plan.expires_at, language)}</div> : null}
        </>
      ) : null}

      {(scenario.active || terminal) ? (
        <div className={`archiveMigrationProgress archiveMigrationProgress-${scenario.status}`}>
          <div className="archiveMigrationProgressHead">
            <strong>{migrationPhaseText(scenario.phase, copy)}</strong>
            <span>{progressKnown ? `${scenario.percent}%` : copy.migrationProgressUnknown}</span>
          </div>
          <div className="archiveMigrationProgressTrack" role={progressKnown ? "progressbar" : undefined} aria-valuemin={progressKnown ? 0 : undefined} aria-valuemax={progressKnown ? 100 : undefined} aria-valuenow={progressKnown ? scenario.percent : undefined}>
            <span style={{ width: progressKnown ? `${scenario.percent}%` : "0%" }} />
          </div>
          <div className="archiveMigrationProgressFacts">
            <span>{copy.migrationFilesCompleted}: {scenario.completedCount === null || scenario.itemCount === null ? copy.migrationUnknownValue : `${scenario.completedCount} / ${scenario.itemCount}`}</span>
            <span>{copy.migrationBytesCompleted}: {scenario.completedBytes === null || scenario.totalBytes === null ? copy.migrationUnknownValue : `${formatBytes(scenario.completedBytes)} / ${formatBytes(scenario.totalBytes)}`}</span>
            {scenario.phase === "copying" ? (
              <>
                <span>{copy.migrationSpeed}: {scenario.speedBytesPerSecond === null ? copy.migrationCalculating : copy.migrationSpeedValue.replace("{value}", formatBytes(scenario.speedBytesPerSecond))}</span>
                <span>{copy.migrationEta}: {migrationEtaText(scenario.etaSeconds, copy)}</span>
              </>
            ) : null}
          </div>
          {scenario.status === "cancel_requested" ? <div className="archiveMigrationHint">{copy.migrationCancelBoundary}</div> : null}
        </div>
      ) : null}

      {scenario.ready ? (
        <div className="archiveMigrationConfirmation">
          <strong>{copy.migrationConfirmTitle}</strong>
          <ul>
            <li>{copy.migrationConfirmVerify}</li>
            <li>{copy.migrationConfirmCleanup}</li>
            <li>{copy.migrationConfirmCancel}</li>
            <li>{copy.migrationConfirmNewFiles}</li>
          </ul>
        </div>
      ) : null}

      {excluded.length ? <div className="archiveMigrationReasons"><strong>{copy.migrationExcludedTitle}</strong>{excluded.map(([code, count]) => <span key={code}>{humanBlockerReason(code, language)}: {count}</span>)}</div> : null}
      {blocked.length ? <div className="archiveMigrationReasons archiveMigrationReasons-warning"><strong>{copy.blockers}</strong>{blocked.map(([code, count]) => <span key={code}>{humanBlockerReason(code, language)}: {count}</span>)}</div> : null}
      {scenario.reason ? <div className="archiveMigrationMessage archiveMigrationMessage-warning"><strong>{copy.migrationWhatHappened}</strong><span>{scenario.reason}</span><span>{migrationNextActionText(scenario.nextAction || scenario.retryMode, copy)}</span></div> : null}
      {error ? <div className="archiveMigrationMessage archiveMigrationMessage-error">{error}</div> : null}
      {scenario.status === "completed" ? (
        <div className="archiveMigrationMessage archiveMigrationMessage-success">
          <strong>{copy.migrationCompletedSelectedPlan}</strong>
          <span>{copy.migrationCompletionScopeNote}</span>
          <span>{copy.migrationExcluded}: {scenario.excludedCount ?? copy.migrationUnknownValue}; {copy.migrationNewAfterPlan}: {scenario.newAfterHighWatermarkCount ?? copy.migrationUnknownValue}; {copy.migrationRetainedSource}: {scenario.retainedSourceCount ?? copy.migrationUnknownValue}</span>
        </div>
      ) : null}
      {scenario.cleanupPending ? <div className="archiveMigrationMessage archiveMigrationMessage-warning">{copy.migrationCleanupPending}</div> : null}
    </div>
  );

  return (
    <OperationDialog
      dialog={{
        id: `archive-migration-${scenario.operationId || scenario.planId || "draft"}`,
        title: copy.migrationDialogTitle,
        className: "archiveMigrationDialog",
        message: draft ? copy.migrationDialogIntro : archiveMigrationStatusText(scenario, roots, copy),
        content,
        tone: scenario.status === "completed" ? "success" : ["partial", "failed", "blocked", "interrupted"].includes(scenario.status) ? "error" : "warning",
        busy,
        dismissible: !busy,
        actions,
        closeLabel: copy.close,
        cancelLabel: copy.close,
        confirmLabel: scenario.canApply ? copy.migrationStart : copy.migrationPrepare,
        confirmDisabled: scenario.canApply ? !scenario.canApply : !canPrepare,
        onConfirm: hasConfirm ? (scenario.canApply ? onApply : onPrepare) : undefined,
      }}
      onClose={onClose}
    />
  );
}

export default function StorageOperationsPage() {
  return (
    <Suspense fallback={null}>
      <StorageOperationsPageContent />
    </Suspense>
  );
}

function StorageOperationsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const requestedMigrationOperationId = searchParams.get("migration");
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
  const [historyDialogOpen, setHistoryDialogOpen] = useState(false);
  const [operationHistory, setOperationHistory] = useState(null);
  const [operationHistoryLoading, setOperationHistoryLoading] = useState(false);
  const [operationHistoryError, setOperationHistoryError] = useState("");
  const [trackedActivationOperationId, setTrackedActivationOperationId] = useState(null);
  const [dismissedActivationOperationId, setDismissedActivationOperationId] = useState(null);
  const [migrationDialogOpen, setMigrationDialogOpen] = useState(false);
  const [migrationSourceRootId, setMigrationSourceRootId] = useState("");
  const [migrationTargetRootId, setMigrationTargetRootId] = useState("");
  const [migrationPlan, setMigrationPlan] = useState(null);
  const [migrationOperation, setMigrationOperation] = useState(null);
  const [migrationBusy, setMigrationBusy] = useState(false);
  const migrationPlanRequestKeyRef = useRef(null);
  const migrationApplyRequestKeyRef = useRef(null);
  const migrationRetryRequestKeyRef = useRef(null);
  const migrationTakeoverRequestKeyRef = useRef(null);
  const [rootAction, setRootAction] = useState("");
  const [integrityDialogOpen, setIntegrityDialogOpen] = useState(false);
  const [integrityScan, setIntegrityScan] = useState(null);
  const [integrityFindings, setIntegrityFindings] = useState([]);
  const [integrityNextCursor, setIntegrityNextCursor] = useState(null);
  const [integrityBusy, setIntegrityBusy] = useState(false);
  const [integrityError, setIntegrityError] = useState("");
  const [integritySelectedFinding, setIntegritySelectedFinding] = useState(null);
  const [integrityPlan, setIntegrityPlan] = useState(null);
  const [migrationMessage, setMigrationMessage] = useState("");
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
  const archiveRoots = status?.archive_roots || operations.archive_roots || [];
  const archiveRootActivation = status?.archive_root_activation || operations.archive_root_activation || {};
  const activationModel = activationProgressModel(archiveRootActivation);
  const archiveRootActivationReason = activationProgressReasonText(archiveRootActivation, copy, language);
  const archiveRootDiscoveryModel = discoveryStateModel(archiveRootDiscovery || {});
  const archiveRootDiscoveryHeader = discoveryHeaderStatusModel(archiveRootDiscovery);
  const archiveRootChoices = archiveRootDiscoveryModel.candidates;
  const cameraRows = useMemo(() => cameraStorageRows(operations.per_camera_usage), [operations.per_camera_usage]);
  const autoFreeConfigured = settings?.auto_free_space_cleanup_enabled ?? policy.auto_free_space_cleanup_enabled ?? autoCleanup.enabled;
  const autoFreeEnabled = settings?.auto_free_space_cleanup_effective ?? policy.auto_free_space_cleanup_effective ?? autoCleanup.effective_enabled ?? null;
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
  const migrationApplyPermission = {
    allowed: Boolean(manageSettingsPermission.allowed && retentionPermission.allowed),
    reason: !manageSettingsPermission.allowed ? manageSettingsPermission.reason : !retentionPermission.allowed ? retentionPermission.reason : "",
  };
  const migrationScenario = migrationScenarioModel({
    plan: migrationPlan,
    operation: migrationOperation,
    preparePermission: manageSettingsPermission,
    applyPermission: migrationApplyPermission,
    running: migrationBusy,
  }, language);
  const retentionManagement = retentionOperationPresentation(retention);
  const autoFreeManagement = autoFreeOperationPresentation({
    policy,
    cleanup: autoCleanup,
    configured: autoFreeConfigured,
    effective: autoFreeEnabled,
    acknowledgementRequired: autoFreeAcknowledgementRequired,
  });
  const integrityManagement = integrityOperationPresentation(reconciliation);
  const migrationManagement = migrationOperationPresentation(migrationScenario, archiveRoots.length);
  const currentArchivePath = archiveRootPath(currentArchiveRoot, archivePathText);
  const healthReason = healthReasonText(topHealth, recording, copy);
  const archiveRootSelectionReady = archiveRootDiscoveryModel.current && archiveRootFolderName.trim() && archiveRootChoiceId;

  useEffect(() => {
    if (!archiveRootChoiceId && archiveRootChoices.length) {
      const recommended = archiveRootChoices.find((choice) => choice.recommended) || archiveRootChoices[0];
      setArchiveRootChoiceId(recommended.id);
    }
  }, [archiveRootChoiceId, archiveRootChoices]);

  useEffect(() => {
    if (!migrationSourceRootId && archiveRoots.length) {
      setMigrationSourceRootId(String(currentArchiveRoot?.id || archiveRoots[0].id));
    }
    if (!migrationTargetRootId && archiveRoots.length > 1) {
      const target = archiveRoots.find((root) => String(root.id) !== String(migrationSourceRootId || currentArchiveRoot?.id));
      if (target) setMigrationTargetRootId(String(target.id));
    }
  }, [archiveRoots, currentArchiveRoot?.id, migrationSourceRootId, migrationTargetRootId]);

  useEffect(() => {
    if (!currentUser || !manageSettingsPermission.allowed) return undefined;
    let cancelled = false;
    const requestedOperationId = requestedMigrationOperationId;
    const restore = async () => {
      try {
        const data = requestedOperationId
          ? await apiFetch(`/storage/migration/operations/${encodeURIComponent(requestedOperationId)}`)
          : await apiFetch("/storage/migration/operations/active");
        if (cancelled) return;
        if (data?.plan) {
          setMigrationPlan(data.plan);
          setMigrationSourceRootId(String(data.plan.source_root_id || ""));
          setMigrationTargetRootId(String(data.plan.target_root_id || ""));
        }
        if (data?.operation) setMigrationOperation(data.operation);
        if (requestedOperationId) setMigrationDialogOpen(true);
      } catch (err) {
        if (!cancelled && requestedOperationId) {
          setMigrationMessage(errorDetailText(err, copy.migrationLoadFailed, language));
          setMigrationDialogOpen(true);
        }
      }
    };
    restore();
    return () => { cancelled = true; };
  }, [currentUser, manageSettingsPermission.allowed, copy.migrationLoadFailed, language, requestedMigrationOperationId]);

  useEffect(() => {
    const planId = migrationPlan?.plan_id;
    const operationId = migrationOperation?.operation_id || migrationPlan?.operation_id;
    const operationStatus = String(migrationOperation?.status || "");
    const planStatus = String(migrationPlan?.status || "");
    const pollOperation = operationId && ["queued", "running", "cancel_requested", "interrupted"].includes(operationStatus || planStatus);
    const pollPlan = !operationId && planId && planStatus === "building";
    if (!pollOperation && !pollPlan) return undefined;
    let cancelled = false;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const data = pollOperation
          ? await apiFetch(`/storage/migration/operations/${encodeURIComponent(operationId)}`)
          : { plan: await apiFetch(`/storage/migration/plans/${encodeURIComponent(planId)}`), operation: null };
        if (cancelled) return;
        if (data?.plan) setMigrationPlan(data.plan);
        if (data?.operation) setMigrationOperation(data.operation);
        setMigrationMessage("");
        const nextStatus = String(data?.operation?.status || data?.plan?.status || "");
        if (["completed", "partial", "failed", "blocked", "cancelled", "expired"].includes(nextStatus)) {
          await loadStatus({ silent: true });
        }
      } catch (err) {
        if (!cancelled) setMigrationMessage(errorDetailText(err, copy.migrationLoadFailed, language));
      } finally {
        polling = false;
      }
    };
    poll();
    const timer = window.setInterval(poll, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [copy.migrationLoadFailed, language, loadStatus, migrationOperation?.operation_id, migrationOperation?.status, migrationPlan?.operation_id, migrationPlan?.plan_id, migrationPlan?.status]);

  useEffect(() => {
    publishStorageMigrationActivity({
      active: migrationScenario.active,
      plan: migrationPlan,
      operation: migrationOperation,
    });
  }, [
    migrationOperation,
    migrationPlan,
    migrationScenario.active,
  ]);

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
    if (completed) {
      setDismissedActivationOperationId(operationId);
      try {
        window.sessionStorage.setItem(ACTIVATION_ACK_KEY, operationId);
      } catch (_) {}
      setArchiveRootDialog((current) => current?.activationOperationId === operationId ? null : current);
      setOperationToast({
        id: `activation-completed-${operationId}`,
        title: copy.activationCompletedTitle,
        message: copy.activationCompletedMessage,
        tone: "success",
      });
      return;
    }
    setArchiveRootDialog({
      id: `activation-${operationId}`,
      activationOperationId: operationId,
      presentation: recoveryRequired ? "compact-confirmation" : undefined,
      title: copy.activationProgressTitle,
      message: activationProgressStatusText(archiveRootActivation, copy),
      items: activationProgressItems(archiveRootActivation, copy),
      action: archiveRootActivationReason || copy.activationProgressHint,
      confirmLabel: recoveryRequired ? copy.activationRetryRecovery : undefined,
      cancelLabel: copy.close,
      closeLabel: copy.close,
      tone: activationProgressTone(archiveRootActivation),
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
    if (nextEnabled) {
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
        presentation: "compact-confirmation",
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
      await loadStatus({ silent: true });
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
      presentation: "compact-confirmation",
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
      presentation: "compact-confirmation",
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
          presentation: capability.canRetryNow ? "compact-confirmation" : undefined,
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

  async function openIntegrityDialog(scanId = null) {
    setIntegrityDialogOpen(true);
    setIntegrityError("");
    if (!diagnosticsPermission.allowed) return;
    setIntegrityBusy(true);
    try {
      const requestedScanId = typeof scanId === "string" && scanId ? scanId : null;
      const latest = await apiFetch(requestedScanId
        ? `/storage/integrity/scans/${encodeURIComponent(requestedScanId)}`
        : "/storage/integrity/scans/latest");
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
    if (!integrityPlan?.plan_id || !integritySelectedFinding) return;
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
    setIntegrityError("");
    if (integrityScan?.scan_id && ["completed", "partial"].includes(String(integrityScan.status || ""))) {
      try {
        await loadIntegrityFindingPage(integrityScan.scan_id);
      } catch (err) {
        setIntegrityError(errorDetailText(err, copy.integrityLoadFailed, language));
      }
    }
  }

  async function openMigrationDialog() {
    setMigrationDialogOpen(true);
    if (!manageSettingsPermission.allowed) return;
    try {
      const active = await apiFetch("/storage/migration/operations/active");
      if (active?.plan) {
        setMigrationPlan(active.plan);
        setMigrationSourceRootId(String(active.plan.source_root_id || ""));
        setMigrationTargetRootId(String(active.plan.target_root_id || ""));
      }
      if (active?.operation) setMigrationOperation(active.operation);
    } catch (_) {}
  }

  async function openOperationHistory() {
    setHistoryDialogOpen(true);
    setOperationHistoryLoading(true);
    setOperationHistoryError("");
    try {
      setOperationHistory(await apiFetch("/storage/operations/history"));
    } catch (err) {
      setOperationHistory(null);
      setOperationHistoryError(errorDetailText(err, copy.operationHistoryUnavailable, language));
    } finally {
      setOperationHistoryLoading(false);
    }
  }

  function resetMigrationDraft() {
    setMigrationPlan(null);
    setMigrationOperation(null);
    setMigrationMessage("");
    migrationPlanRequestKeyRef.current = null;
    migrationApplyRequestKeyRef.current = null;
    migrationRetryRequestKeyRef.current = null;
  }

  function selectMigrationSource(rootId) {
    const next = String(rootId || "");
    setMigrationSourceRootId(next);
    if (next === String(migrationTargetRootId)) {
      const alternative = archiveRoots.find((root) => String(root.id) !== next);
      setMigrationTargetRootId(alternative ? String(alternative.id) : "");
    }
    resetMigrationDraft();
  }

  function selectMigrationTarget(rootId) {
    setMigrationTargetRootId(String(rootId || ""));
    resetMigrationDraft();
  }

  async function prepareMigrationPlan() {
    if (!manageSettingsPermission.allowed) {
      setMigrationMessage(manageSettingsPermission.reason);
      return;
    }
    if (!migrationSourceRootId || !migrationTargetRootId || migrationSourceRootId === migrationTargetRootId) {
      setMigrationMessage(copy.migrationChooseSourceTarget);
      return;
    }
    setMigrationBusy(true);
    setMigrationMessage("");
    setMigrationOperation(null);
    const idempotencyKey = migrationPlanRequestKeyRef.current || newStorageOperationId("migration-plan");
    migrationPlanRequestKeyRef.current = idempotencyKey;
    try {
      const plan = await apiFetch("/storage/migration/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_root_id: migrationSourceRootId,
          target_root_id: migrationTargetRootId,
          idempotency_key: idempotencyKey,
        }),
      });
      setMigrationPlan(plan);
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.migrationPrepareFailed, language));
    } finally {
      setMigrationBusy(false);
    }
  }

  async function applyMigration() {
    if (!migrationScenario.canApply || !migrationPlan?.plan_id || !migrationPlan?.canonical_hash) return;
    setMigrationBusy(true);
    setMigrationMessage("");
    const idempotencyKey = migrationApplyRequestKeyRef.current || newStorageOperationId("migration-apply");
    migrationApplyRequestKeyRef.current = idempotencyKey;
    try {
      const result = await apiFetch("/storage/migration/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          plan_id: migrationPlan.plan_id,
          expected_plan_hash: migrationPlan.canonical_hash,
          idempotency_key: idempotencyKey,
          confirm: true,
        }),
      });
      setMigrationPlan(result.plan || migrationPlan);
      setMigrationOperation(result.operation || null);
      await loadStatus({ silent: true });
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.applyBlocked, language));
    } finally {
      setMigrationBusy(false);
    }
  }

  async function cancelMigration() {
    const operationId = migrationOperation?.operation_id || migrationPlan?.operation_id;
    const planId = migrationPlan?.plan_id;
    if (!operationId && !planId) return;
    setMigrationBusy(true);
    setMigrationMessage("");
    try {
      const result = operationId
        ? await apiFetch(`/storage/migration/operations/${encodeURIComponent(operationId)}/cancel`, { method: "POST" })
        : await apiFetch(`/storage/migration/plans/${encodeURIComponent(planId)}/cancel`, { method: "POST" });
      if (result?.plan) setMigrationPlan(result.plan);
      if (result?.operation) setMigrationOperation(result.operation);
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.migrationCancelFailed, language));
    } finally {
      setMigrationBusy(false);
    }
  }

  async function retryMigration() {
    const operationId = migrationOperation?.operation_id || migrationPlan?.operation_id;
    if (!operationId || !migrationScenario.canRetry) return;
    setMigrationBusy(true);
    setMigrationMessage("");
    const idempotencyKey = migrationRetryRequestKeyRef.current || newStorageOperationId("migration-retry");
    migrationRetryRequestKeyRef.current = idempotencyKey;
    try {
      const result = await apiFetch(`/storage/migration/operations/${encodeURIComponent(operationId)}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: idempotencyKey }),
      });
      if (result?.plan) setMigrationPlan(result.plan);
      if (result?.operation) setMigrationOperation(result.operation);
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.migrationRetryFailed, language));
    } finally {
      setMigrationBusy(false);
    }
  }

  async function takeoverMigrationCleanup() {
    const operationId = migrationOperation?.operation_id || migrationPlan?.operation_id;
    if (!operationId || !migrationScenario.canCleanupTakeover) return;
    setMigrationBusy(true);
    setMigrationMessage("");
    const idempotencyKey = migrationTakeoverRequestKeyRef.current || newStorageOperationId("migration-cleanup-takeover");
    migrationTakeoverRequestKeyRef.current = idempotencyKey;
    try {
      const result = await apiFetch(`/storage/migration/operations/${encodeURIComponent(operationId)}/cleanup-takeover`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: idempotencyKey, confirm: true }),
      });
      if (result?.plan) setMigrationPlan(result.plan);
      if (result?.operation) setMigrationOperation(result.operation);
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.migrationCleanupTakeoverFailed, language));
    } finally {
      setMigrationBusy(false);
    }
  }

  function openArchiveRootSetup() {
    const details = document.getElementById("storage-archive-root-add");
    if (!(details instanceof HTMLDetailsElement)) return;
    details.open = true;
    details.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => details.querySelector("select, input, button")?.focus({ preventScroll: true }), 250);
    if (!archiveRootDiscoveryModel.refreshing) loadArchiveRootDiscovery();
  }

  const lastResultText = (last) => last?.at
    ? formatDateTime(last.at, language)
    : copy.archiveManagementNeverRun;
  const archiveManagementGroups = [
    {
      id: "protection",
      title: copy.archiveManagementProtectionGroup,
      rows: [
        {
          id: "retention",
          title: copy.archiveManagementRetentionTitle,
          status: copyStatus(copy, "archiveManagementRetentionStatus", retentionManagement.status),
          tone: retentionManagement.tone,
          description: retentionManagementDescription(retentionManagement, copy, language),
          facts: [
            { label: copy.archiveManagementConfiguredCameras, value: String(retentionManagement.configuredCount) },
            { label: copy.archiveManagementLastApplication, value: lastResultText(retentionManagement.last), tone: retentionManagement.status === "needs_attention" ? "warning" : "" },
          ],
          action: manageCamerasPermission.allowed
            ? (
              <a
                className="button secondary small appIllustratedAction"
                href="/cameras"
                title={copy.configureCameras}
                aria-label={copy.configureCameras}
              >
                <img src="/assets/icons/ui/camera.png" alt="" aria-hidden="true" />
              </a>
            )
            : (
              <button
                className="button secondary small appIllustratedAction"
                type="button"
                disabled
                title={manageCamerasPermission.reason}
                aria-label={copy.configureCameras}
              >
                <img src="/assets/icons/ui/camera.png" alt="" aria-hidden="true" />
              </button>
            ),
        },
        {
          id: "auto-free",
          title: copy.archiveManagementAutoFreeTitle,
          status: copyStatus(copy, "archiveManagementAutoFreeStatus", autoFreeManagement.status),
          tone: autoFreeManagement.tone,
          description: autoFreeManagementDescription(autoFreeManagement, copy, language),
          facts: [
            {
              label: copy.archiveManagementFreeSpace,
              value: autoFreeManagement.freePercent === null ? copy.archiveManagementUnknownValue : formatPercent(autoFreeManagement.freePercent),
              tone: ["critical", "warning", "cleanup", "recovery"].includes(autoFreeManagement.status) ? "warning" : "",
            },
            { label: copy.archiveManagementLastCleanup, value: lastResultText(autoFreeManagement.last), tone: autoFreeManagement.status === "failed" ? "warning" : "" },
          ],
          action: (
            <ArchivePolicySwitch
              checked={autoFreeManagement.effective}
              busy={rootAction === "auto-free"}
              disabled={!manageSettingsPermission.allowed}
              label={autoFreeManagement.effective ? copy.disableAutoFree : copy.enableAutoFree}
              title={!manageSettingsPermission.allowed ? manageSettingsPermission.reason : autoFreeManagement.effective ? copy.disableAutoFree : copy.enableAutoFree}
              onChange={requestAutoFreeSpace}
            />
          ),
        },
      ],
    },
    {
      id: "maintenance",
      title: copy.archiveManagementMaintenanceGroup,
      rows: [
        {
          id: "integrity",
          title: copy.archiveManagementIntegrityTitle,
          status: copyStatus(copy, "archiveManagementIntegrityStatus", integrityManagement.status),
          tone: integrityManagement.tone,
          description: integrityManagementDescription(integrityManagement, copy),
          facts: [
            { label: copy.problems, value: String(integrityManagement.problemCount), tone: integrityManagement.problemCount ? "warning" : "" },
            { label: copy.lastCheck, value: integrityManagement.lastCheckedAt ? formatDateTime(integrityManagement.lastCheckedAt, language) : copy.archiveManagementNeverRun },
          ],
          action: (
            <button
              className="button secondary small appIllustratedAction"
              type="button"
              title={!diagnosticsPermission.allowed
                ? diagnosticsPermission.reason
                : ["not_run", "stale"].includes(integrityManagement.status)
                  ? copy.integrityCheckArchive
                  : copy.integrityOpenCheck}
              aria-label={["not_run", "stale"].includes(integrityManagement.status) ? copy.integrityCheckArchive : copy.integrityOpenCheck}
              onClick={() => openIntegrityDialog()}
              disabled={!!rootAction || !diagnosticsPermission.allowed}
            >
              <img src="/assets/icons/ui/open.png" alt="" aria-hidden="true" />
            </button>
          ),
        },
        {
          id: "migration",
          title: copy.archiveManagementMigrationTitle,
          status: copyStatus(copy, "archiveManagementMigrationStatus", migrationManagement.status),
          tone: migrationManagement.tone,
          description: migrationManagementDescription(migrationManagement, migrationScenario, copy),
          facts: [
            {
              label: copy.archiveManagementPlanFiles,
              value: migrationManagement.itemCount === null ? copy.archiveManagementUnknownValue : String(migrationManagement.itemCount),
            },
            {
              label: copy.archiveManagementProgress,
              value: migrationManagement.percent === null ? copy.archiveManagementNotRunning : `${migrationManagement.percent}%`,
              tone: migrationManagement.status === "needs_attention" ? "warning" : "",
            },
          ],
          action: migrationManagement.status === "needs_target"
            ? (
              <button
                className="button secondary small appIllustratedAction"
                type="button"
                onClick={openArchiveRootSetup}
                title={copy.archiveManagementAddLocation}
                aria-label={copy.archiveManagementAddLocation}
              >
                <img src="/assets/icons/ui/add-storage-location.png" alt="" aria-hidden="true" />
              </button>
            )
            : (
              <button
                className="button secondary small appIllustratedAction"
                type="button"
                title={!manageSettingsPermission.allowed
                  ? manageSettingsPermission.reason
                  : migrationScenario.active || migrationScenario.terminal
                    ? copy.archiveManagementContinue
                    : copy.migrationOpen}
                aria-label={migrationScenario.active || migrationScenario.terminal ? copy.archiveManagementContinue : copy.migrationOpen}
                onClick={openMigrationDialog}
                disabled={!!rootAction || !manageSettingsPermission.allowed}
              >
                <img src="/assets/icons/ui/open.png" alt="" aria-hidden="true" />
              </button>
            ),
        },
      ],
    },
  ];

  const historyDialog = historyDialogOpen ? {
    id: "archive-operation-history",
    title: copy.operationHistoryTitle,
    message: copy.operationHistoryIntro,
    className: "archiveOperationHistoryDialog",
    tone: "neutral",
    closeLabel: copy.close,
    content: (
      <ArchiveOperationHistoryContent
        history={operationHistory}
        loading={operationHistoryLoading}
        error={operationHistoryError}
        copy={copy}
        language={language}
        formatDateTime={formatDateTime}
        formatBytes={formatBytes}
        humanBlockerReason={humanBlockerReason}
      />
    ),
  } : null;

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
                </div>
              </div>
              <div className="storageOpsTopMetrics" aria-label={copy.firstScreenMetrics}>
                <TopMetric label={copy.archiveAccess} value={recording.label} tone={recording.tone} />
                <TopMetric label={`${copy.free} ${formatPercent(capacity.free_percent)}`} value={formatBytes(capacity.free_bytes)} tone={freeSpaceTone(capacity, policy)} />
                <TopMetric
                  label={copy.archiveProblems}
                  value={String(normalizedReconciliation.problemCount || 0)}
                  tone={normalizedReconciliation.problemCount ? "warning" : "ok"}
                  onValueClick={() => openIntegrityDialog()}
                  actionLabel={diagnosticsPermission.allowed ? copy.integrityOpenCheck : diagnosticsPermission.reason}
                  disabled={!diagnosticsPermission.allowed}
                />
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
                  id="storage-archive-root-add"
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
                      <button
                        className="button small appIllustratedAction storageOpsRootAddButton"
                        type="button"
                        onClick={addRoot}
                        disabled={!!rootAction || !archiveRootSelectionReady || !manageSettingsPermission.allowed}
                        title={rootAction === "add" ? copy.adding : copy.add}
                        aria-label={rootAction === "add" ? copy.adding : copy.add}
                        aria-busy={rootAction === "add" ? "true" : undefined}
                      >
                        <img src="/assets/icons/ui/add-storage-location.png" alt="" aria-hidden="true" />
                      </button>
                    </div>
                    <div className="storageOpsDiscoveryFeedback">
                      {archiveRootDiscoveryHeader.needsRefresh ? (
                        <button
                          className={`button secondary small appIllustratedAction ${archiveRootDiscoveryModel.refreshing ? "isRefreshing" : ""}`}
                          type="button"
                          onClick={loadArchiveRootDiscovery}
                          disabled={!!rootAction || archiveRootDiscoveryModel.refreshing}
                          title={archiveRootDiscoveryModel.refreshing ? copy.refreshing : copy.refreshDiscovery}
                          aria-label={archiveRootDiscoveryModel.refreshing ? copy.refreshing : copy.refreshDiscovery}
                          aria-busy={archiveRootDiscoveryModel.refreshing ? "true" : undefined}
                        >
                          <span className="appIllustratedActionGlyph" aria-hidden="true">↻</span>
                        </button>
                      ) : null}
                    </div>
                    {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                  </div>
                </details>
              </Section>

              <ArchiveManagementCenter
                title={copy.archiveManagementTitle}
                subtitle={refreshWarning ? copy.archiveManagementStale : copy.archiveManagementSubtitle}
                historyLabel={copy.operationHistory}
                onOpenHistory={openOperationHistory}
                groups={archiveManagementGroups}
              />
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
          copy={copy}
          language={language}
          permission={diagnosticsPermission}
          onClose={() => setIntegrityDialogOpen(false)}
          onStart={startIntegrityScan}
          onCancel={cancelIntegrityScan}
          onLoadMore={loadMoreIntegrityFindings}
          onPrepare={prepareIntegrityAction}
          onClearPlan={clearIntegrityPlan}
          onApply={applyIntegrityAction}
        />
        <ArchiveMigrationDialog
          open={migrationDialogOpen}
          roots={archiveRoots}
          sourceRootId={migrationSourceRootId}
          targetRootId={migrationTargetRootId}
          plan={migrationPlan}
          operation={migrationOperation}
          scenario={migrationScenario}
          busy={migrationBusy}
          error={migrationMessage}
          copy={copy}
          language={language}
          onClose={() => {
            setMigrationDialogOpen(false);
            if (requestedMigrationOperationId) router.replace("/storage", { scroll: false });
          }}
          onSourceChange={selectMigrationSource}
          onTargetChange={selectMigrationTarget}
          onPrepare={prepareMigrationPlan}
          onApply={applyMigration}
          onCancel={cancelMigration}
          onRetry={retryMigration}
          onCleanupTakeover={takeoverMigrationCleanup}
          onReset={resetMigrationDraft}
        />
        <OperationDialog dialog={historyDialog} onClose={() => setHistoryDialogOpen(false)} />
        <OperationDialog dialog={archiveRootDialog} onClose={closeArchiveRootDialog} />
        <OperationToast toast={operationToast} onClose={() => setOperationToast(null)} />
      </div>
    </Layout>
  );
}
