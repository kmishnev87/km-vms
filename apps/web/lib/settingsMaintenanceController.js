"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "./api";
import {
  BACKUP_OPERATION_PENDING_STORAGE_KEY,
  MAINTENANCE_DRY_RUN_ENDPOINTS,
  UPDATE_APPLY_PENDING_STORAGE_KEY,
  UPDATE_APPLY_POLL_INTERVAL_MS,
  backupOperationWithinAdmissionGrace,
  createBackupOperationPending,
  createUpdateApplyPending,
  formatAuditTimestamp,
  humanErrorText,
  maintenanceBackupDetailModel,
  maintenanceBackupOperationResultText,
  maintenanceBackupOverviewModel,
  maintenanceBackupValidOffset,
  maintenanceDatabaseOverviewModel,
  maintenanceOverallHealthModel,
  maintenanceStatusText,
  maintenanceWarningModel,
  reconcileUpdateApplyPending,
  restoreBackupOperationPending,
  restoreUpdateApplyPending,
  sanitizeBackupOperationPending,
  sanitizeUpdateApplyPending,
  updateApplyButtonText,
  updateApplyCandidateSnapshot,
  updateApplyErrorMessages,
  updateApplyIsRunning,
  updateApplyOperatorModel,
  updateApplyReconnectTiming,
} from "./settingsPageHelpers";

const MAINTENANCE_BACKUP_PAGE_SIZE = 5;
const MAINTENANCE_BACKUP_POLL_INTERVAL_MS = 3000;
const CURRENT_RESTORE_PENDING_STORAGE_KEY = "km_vms_current_restore_pending_v1";
const CURRENT_RESTORE_CONFIRMATION_PHRASE = "RESTORE KM VMS";
const CURRENT_RESTORE_OPERATIONAL_PHASES = [
  "preflight",
  "pre_restore_backup",
  "writers_paused",
  "restore_running",
  "services_starting",
  "post_restore_check",
];
const CURRENT_RESTORE_LEGACY_REASON_PHASES = {
  pre_restore_backup_verification_failed: "pre_restore_backup",
  restore_writer_isolation_failed: "writers_paused",
  automatic_rollback_isolation_failed: "writers_paused",
  pg_restore_failed: "restore_running",
  pre_restore_backup_missing: "restore_running",
  restore_interrupted_after_mutation: "restore_running",
  automatic_rollback_database_failed: "restore_running",
  restore_api_health_failed: "services_starting",
  automatic_rollback_api_recovery_failed: "services_starting",
  automatic_rollback_validation_failed: "post_restore_check",
  post_restore_actor_access_invalid: "post_restore_check",
  post_restore_schema_invalid: "post_restore_check",
  post_restore_metadata_invalid: "post_restore_check",
  post_restore_tables_missing: "post_restore_check",
  restore_recorder_start_failed: "post_restore_check",
  automatic_rollback_recorder_recovery_failed: "post_restore_check",
};

let settingsBodyScrollLockCount = 0;
let settingsBodyPreviousOverflow = "";

function acquireSettingsBodyScrollLock() {
  if (typeof document === "undefined") return () => {};
  if (settingsBodyScrollLockCount === 0) {
    settingsBodyPreviousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  settingsBodyScrollLockCount += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    settingsBodyScrollLockCount = Math.max(0, settingsBodyScrollLockCount - 1);
    if (settingsBodyScrollLockCount === 0) {
      document.body.style.overflow = settingsBodyPreviousOverflow;
      settingsBodyPreviousOverflow = "";
    }
  };
}
function monotonicWallNow() {
  if (typeof performance !== "undefined" && Number.isFinite(performance.timeOrigin) && typeof performance.now === "function") {
    return performance.timeOrigin + performance.now();
  }
  return Date.now();
}

function focusableElements(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(
    'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true");
}

function updateApplyRequestIsAmbiguous(error) {
  return error?.category === "network_unavailable" ||
    error?.category === "temporarily_unavailable" ||
    Number(error?.status || 0) === 0 ||
    Number(error?.status || 0) >= 500;
}

function createUpdateApplySubmissionId() {
  const browserCrypto = globalThis.crypto;
  if (typeof browserCrypto?.randomUUID === "function") {
    return browserCrypto.randomUUID();
  }
  if (typeof browserCrypto?.getRandomValues !== "function") return "";
  const bytes = browserCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

export function useSettingsMaintenanceController({
  canManageMaintenance,
  lang,
  t,
  showToast,
  clearToast,
  diagnosticChoiceOpen,
}) {
  const [maintenanceModalOpen, setMaintenanceModalOpen] = useState(false);
  const [maintenanceOverview, setMaintenanceOverview] = useState(null);
  const [maintenanceBackupDetail, setMaintenanceBackupDetail] = useState(null);
  const [maintenanceBackupDetailOpen, setMaintenanceBackupDetailOpen] = useState(false);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceError, setMaintenanceError] = useState("");
  const [maintenanceBusy, setMaintenanceBusy] = useState("");
  const [maintenanceActionResult, setMaintenanceActionResult] = useState(null);
  const [maintenanceBackupResult, setMaintenanceBackupResult] = useState(null);
  const [maintenanceConfirm, setMaintenanceConfirm] = useState(null);
  const [maintenanceBackupPending, setMaintenanceBackupPending] = useState(null);
  const [currentRestoreDialog, setCurrentRestoreDialog] = useState(null);
  const [currentRestorePending, setCurrentRestorePending] = useState(null);
  const [currentRestoreStatus, setCurrentRestoreStatus] = useState(null);
  const [updateStatus, setUpdateStatus] = useState(null);
  const [updateApplyStatus, setUpdateApplyStatus] = useState(null);
  const [updateTransportErrors, setUpdateTransportErrors] = useState({ update: null, apply: null });
  const [updateApplyReconnectSnapshot, setUpdateApplyReconnectSnapshot] = useState(null);
  const [updateApplyClockMs, setUpdateApplyClockMs] = useState(() => Date.now());
  const [updateApplyDialog, setUpdateApplyDialog] = useState(null);
  const [updateApplyPending, setUpdateApplyPending] = useState(null);

  const updatePollInFlightRef = useRef(false);
  const updateApplyPendingRef = useRef(null);
  const maintenanceBackupPendingRef = useRef(null);
  const maintenanceBackupRecoveryRef = useRef(false);
  const maintenanceBackupAdmissionRef = useRef(null);
  const maintenanceBackupPollInFlightRef = useRef(false);
  const maintenanceBackupDetailRef = useRef(null);
  const currentRestorePendingRef = useRef(null);
  const currentRestorePollInFlightRef = useRef(false);
  const currentRestoreDialogRef = useRef(null);
  const updateApplyDialogRef = useRef(null);
  const maintenanceChildDialogOpenRef = useRef(false);
  const maintenanceDialogRef = useRef(null);
  const maintenanceTriggerRef = useRef(null);
  const maintenanceBusyRef = useRef("");

  const updateApplyHasUnknownLaunch = Boolean(updateApplyPending);
  const updateApplyOperator = updateApplyOperatorModel(updateStatus, updateApplyStatus, t, lang, {
    updateError: updateTransportErrors.update,
    applyError: updateTransportErrors.apply,
    reconnectTiming: updateApplyReconnectSnapshot,
    nowMs: updateApplyClockMs,
    unresolvedSubmission: updateApplyHasUnknownLaunch,
  });
  const updateApplyRunning = updateApplyIsRunning(updateApplyStatus?.status || "") && !updateApplyOperator.stateUnknown;
  const updateApplyAllowed = Boolean(
    updateApplyOperator.canApply
    && !updateApplyPending
    && !updateApplyDialog
    && !maintenanceBusy
    && !currentRestorePending,
  );
  const updateApplyPrimaryText = updateApplyPending || updateApplyOperator.stateUnknown
      ? t.updateApplyLocked
      : updateApplyButtonText(updateApplyStatus, t);
  const updateApplyErrors = updateApplyErrorMessages(updateApplyStatus?.error, t, lang);
  const updatePeerCheckUnavailable = Boolean(
    updateTransportErrors.update && !updateTransportErrors.apply,
  );
  const maintenanceBackupOverview = useMemo(
    () => maintenanceBackupOverviewModel(maintenanceOverview, t, lang),
    [maintenanceOverview, t, lang],
  );
  const maintenanceBackupManager = useMemo(
    () => maintenanceBackupDetailModel(maintenanceBackupDetail, t, lang),
    [maintenanceBackupDetail, t, lang],
  );
  const maintenanceDatabase = useMemo(
    () => maintenanceDatabaseOverviewModel(maintenanceOverview, t),
    [maintenanceOverview, t],
  );
  const maintenanceWarnings = useMemo(() => maintenanceWarningModel(maintenanceOverview, t), [maintenanceOverview, t]);
  const maintenanceChildDialogOpen = Boolean(
    maintenanceConfirm
    || currentRestoreDialog
    || updateApplyDialog
    || diagnosticChoiceOpen
  );
  const maintenanceOverall = useMemo(
    () => maintenanceOverallHealthModel({
      overview: maintenanceOverview,
      updateOperator: updateApplyOperator,
      database: maintenanceDatabase,
      backup: maintenanceBackupOverview,
      warnings: maintenanceWarnings,
      loading: maintenanceLoading,
      loadError: Boolean(maintenanceError),
      t,
    }),
    [
      maintenanceOverview,
      updateApplyOperator,
      maintenanceDatabase,
      maintenanceBackupOverview,
      maintenanceWarnings,
      maintenanceLoading,
      maintenanceError,
      t,
    ],
  );
  const maintenanceBackupResultModel = useMemo(() => (
    maintenanceBackupResult ? maintenanceBackupOperationResultText(maintenanceBackupResult, t) : null
  ), [maintenanceBackupResult, t]);
  const maintenanceBackupProgressKind = String(
    maintenanceBackupPending?.kind || maintenanceBackupResult?.kind || "check",
  );
  const maintenanceBackupProgressText = maintenanceBackupResult?.recovering
    ? t.maintenanceBackupRecovering
    : maintenanceBackupProgressKind === "create"
      ? t.maintenanceBackupCreating
      : maintenanceBackupProgressKind === "delete"
        ? t.maintenanceBackupDeleting
        : t.maintenanceBackupChecking;
  const updateApplyLaunchNotice = updateApplyPending ? t.updateApplyLaunchUnknown : "";

  maintenanceBusyRef.current = maintenanceBusy;
  updateApplyDialogRef.current = updateApplyDialog;
  updateApplyPendingRef.current = updateApplyPending;
  maintenanceBackupPendingRef.current = maintenanceBackupPending;
  maintenanceBackupDetailRef.current = maintenanceBackupDetail;
  currentRestorePendingRef.current = currentRestorePending;
  currentRestoreDialogRef.current = currentRestoreDialog;
  maintenanceChildDialogOpenRef.current = maintenanceChildDialogOpen;

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(BACKUP_OPERATION_PENDING_STORAGE_KEY);
      const restored = restoreBackupOperationPending(raw, Date.now());
      if (restored) {
        maintenanceBackupRecoveryRef.current = true;
        maintenanceBackupPendingRef.current = restored;
        setMaintenanceBackupPending(restored);
        setMaintenanceBackupResult({
          kind: restored.kind,
          status: "running",
          state: "running",
          recovering: true,
        });
      } else if (raw !== null) {
        window.sessionStorage.removeItem(BACKUP_OPERATION_PENDING_STORAGE_KEY);
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(UPDATE_APPLY_PENDING_STORAGE_KEY);
      const restored = restoreUpdateApplyPending(raw, Date.now());
      if (restored) {
        updateApplyPendingRef.current = restored;
        setUpdateApplyPending(restored);
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      const value = raw ? JSON.parse(raw) : null;
      const valid = value
        && typeof value === "object"
        && typeof value.submissionId === "string"
        && typeof value.artifactId === "string"
        && Number.isFinite(value.createdAt)
        && Date.now() - value.createdAt < 24 * 60 * 60 * 1000;
      if (valid) {
        currentRestorePendingRef.current = value;
        setCurrentRestorePending(value);
        setCurrentRestoreDialog({
          artifact: { id: value.artifactId, createdAt: "-" },
          phrase: CURRENT_RESTORE_CONFIRMATION_PHRASE,
          preflight: { can_restore: true, reason_codes: [] },
          preflightBusy: false,
          accepted: true,
          reconnecting: true,
          error: "",
        });
      } else if (raw !== null) {
        window.sessionStorage.removeItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      }
    } catch {
      try {
        window.sessionStorage.removeItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      } catch {}
    }
  }, []);

  useEffect(() => {
    if (!canManageMaintenance || !currentRestorePending) return undefined;
    let cancelled = false;
    let timer = null;
    let delay = 1500;
    const tick = async () => {
      const result = await pollCurrentRestoreStatus();
      if (cancelled || result?.terminal_result) return;
      delay = result ? 2500 : Math.min(10000, Math.max(2500, delay * 2));
      timer = window.setTimeout(tick, delay);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [canManageMaintenance, currentRestorePending?.submissionId]);

  useEffect(() => {
    if (maintenanceModalOpen && canManageMaintenance) {
      loadMaintenanceOverview();
      loadUpdateApplySurface({ silent: true });
    }
  }, [maintenanceModalOpen, canManageMaintenance]);

  useEffect(() => {
    const active = updateApplyIsRunning(updateApplyStatus?.status || "");
    if (!canManageMaintenance || (!maintenanceModalOpen && !active && !updateApplyPending)) return undefined;
    const timer = window.setInterval(() => loadUpdateApplySurface({ silent: true }), UPDATE_APPLY_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [maintenanceModalOpen, canManageMaintenance, updateApplyStatus?.status, Boolean(updateApplyPending)]);

  useEffect(() => {
    if (!canManageMaintenance || !maintenanceBackupPending) return undefined;
    reconcilePendingBackupOperation();
    const timer = window.setInterval(
      reconcilePendingBackupOperation,
      MAINTENANCE_BACKUP_POLL_INTERVAL_MS,
    );
    return () => window.clearInterval(timer);
  }, [canManageMaintenance, maintenanceBackupPending?.submissionId]);

  useEffect(() => {
    if (!maintenanceModalOpen) return undefined;
    return acquireSettingsBodyScrollLock();
  }, [maintenanceModalOpen]);

  useEffect(() => {
    if (!maintenanceModalOpen) return undefined;
    const container = maintenanceDialogRef.current;
    const initial = focusableElements(container)[0];
    initial?.focus();
    function onKeyDown(event) {
      if (event.defaultPrevented || maintenanceChildDialogOpenRef.current) return;
      if (event.key === "Escape" && !maintenanceBusyRef.current) {
        event.preventDefault();
        closeMaintenanceModal();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableElements(container);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (!maintenanceChildDialogOpenRef.current) maintenanceTriggerRef.current?.focus();
    };
  }, [maintenanceModalOpen]);

  function openMaintenanceModal() {
    if (!canManageMaintenance) return;
    setMaintenanceModalOpen(true);
  }

  function closeMaintenanceModal() {
    if (maintenanceChildDialogOpenRef.current) return;
    setMaintenanceModalOpen(false);
    setMaintenanceBackupDetailOpen(false);
    setMaintenanceActionResult(null);
    setMaintenanceBackupResult(null);
    setMaintenanceConfirm(null);
    setMaintenanceError("");
  }

  function safeUpdateTransportError(error, fallback) {
    const category = String(error?.category || "request_failed");
    return {
      category,
      status: Number(error?.status || 0),
      message: t.updateApplyTransportErrors?.[category] || fallback,
    };
  }

  function safeUpdateLaunchError(error) {
    const code = String(error?.code || "");
    if (code === "update_already_running") return t.updateApplyLaunchConflict;
    return safeUpdateTransportError(error, t.updateApplyLaunchRejected).message;
  }

  function commitUpdateApplyPending(nextRecord) {
    const safeRecord = nextRecord
      ? sanitizeUpdateApplyPending(nextRecord, Date.now())
      : null;
    if (nextRecord && !safeRecord) return null;
    updateApplyPendingRef.current = safeRecord;
    setUpdateApplyPending(safeRecord);
    try {
      if (safeRecord) {
        window.sessionStorage.setItem(UPDATE_APPLY_PENDING_STORAGE_KEY, JSON.stringify(safeRecord));
      } else {
        window.sessionStorage.removeItem(UPDATE_APPLY_PENDING_STORAGE_KEY);
      }
    } catch {}
    return safeRecord;
  }

  function commitBackupOperationPending(nextRecord) {
    const safeRecord = nextRecord
      ? sanitizeBackupOperationPending(nextRecord, Date.now())
      : null;
    if (nextRecord && !safeRecord) return null;
    try {
      if (safeRecord) {
        window.sessionStorage.setItem(
          BACKUP_OPERATION_PENDING_STORAGE_KEY,
          JSON.stringify(safeRecord),
        );
      } else {
        window.sessionStorage.removeItem(BACKUP_OPERATION_PENDING_STORAGE_KEY);
      }
    } catch {
      if (safeRecord) return null;
    }
    maintenanceBackupPendingRef.current = safeRecord;
    setMaintenanceBackupPending(safeRecord);
    return safeRecord;
  }

  function commitCurrentRestorePending(nextRecord) {
    const safeRecord = nextRecord
      && typeof nextRecord === "object"
      && typeof nextRecord.submissionId === "string"
      && typeof nextRecord.artifactId === "string"
      && Number.isFinite(nextRecord.createdAt)
      ? {
          submissionId: nextRecord.submissionId,
          artifactId: nextRecord.artifactId,
          createdAt: nextRecord.createdAt,
        }
      : null;
    currentRestorePendingRef.current = safeRecord;
    setCurrentRestorePending(safeRecord);
    try {
      if (safeRecord) {
        window.sessionStorage.setItem(
          CURRENT_RESTORE_PENDING_STORAGE_KEY,
          JSON.stringify(safeRecord),
        );
      } else {
        window.sessionStorage.removeItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      }
    } catch {}
    return safeRecord;
  }

  function currentRestoreReasonCode(value) {
    const detail = value?.detail && typeof value.detail === "object"
      ? value.detail
      : value?.data?.detail && typeof value.data.detail === "object"
        ? value.data.detail
        : {};
    return String(
      value?.reason_code
      || value?.code
      || detail.reason_code
      || detail.code
      || detail.preflight?.reason_codes?.[0]
      || "",
    ).trim();
  }

  function currentRestoreReasonText(
    code,
    fallback = t.maintenanceCurrentRestoreRequestRejected,
  ) {
    const normalized = String(code || "").trim();
    const aliases = {
      artifact_schema_migration_required: "artifact_schema_migration_required",
      artifact_schema_newer: "artifact_schema_newer",
      schema_migration_required: "schema_migration_required",
      schema_newer_than_supported: "schema_newer_than_supported",
      compatibility_migration_required: "artifact_schema_migration_required",
      migration_required: "artifact_schema_migration_required",
      newer_than_supported: "artifact_schema_newer",
      unsupported_backend: "artifact_backend_unsupported",
      unknown: "artifact_invalid",
    };
    const key = aliases[normalized] || normalized;
    return t.maintenanceCurrentRestoreReasons?.[key]
      || fallback;
  }

  function currentRestoreFailedPhase(status) {
    const exact = String(status?.failed_phase || "");
    if (CURRENT_RESTORE_OPERATIONAL_PHASES.includes(exact)) {
      return exact;
    }
    return CURRENT_RESTORE_LEGACY_REASON_PHASES[
      String(status?.reason_code || "")
    ] || "";
  }

  function currentRestoreTerminal(status = currentRestoreStatus) {
    return ["completed", "blocked", "failed_rolled_back", "failed_recovery_required"]
      .includes(String(status?.terminal_result || ""));
  }

  function currentRestoreTerminalText(status = currentRestoreStatus) {
    const terminal = String(status?.terminal_result || "");
    if (terminal === "completed") return t.maintenanceCurrentRestoreCompleted;
    if (terminal === "failed_rolled_back") return t.maintenanceCurrentRestoreRolledBack;
    if (terminal === "failed_recovery_required") {
      return currentRestoreReasonText(
        status?.reason_code,
        t.maintenanceCurrentRestoreRecoveryRequired,
      );
    }
    if (terminal === "blocked") {
      return currentRestoreReasonText(status?.reason_code);
    }
    return "";
  }

  function closeCurrentRestoreDialog() {
    if (!currentRestoreDialog) return;
    if (currentRestoreDialog.accepted && !currentRestoreTerminal()) return;
    setCurrentRestoreDialog(null);
    if (currentRestoreTerminal()) setCurrentRestoreStatus(null);
  }

  async function pollCurrentRestoreStatus(pendingOverride = null) {
    const pending = pendingOverride || currentRestorePendingRef.current;
    if (!pending || currentRestorePollInFlightRef.current) return null;
    currentRestorePollInFlightRef.current = true;
    try {
      const result = await apiFetch("/system/restore/current/status");
      const resultSubmissionId = String(result?.submission_id || "");
      if (
        String(result?.status || "") !== "idle"
        && resultSubmissionId !== pending.submissionId
      ) {
        setCurrentRestoreDialog((current) => ({
          ...(current || {}),
          artifact: current?.artifact || {
            id: pending.artifactId,
            createdAt: "-",
          },
          accepted: true,
          reconnecting: true,
          error: "",
        }));
        return null;
      }
      if (String(result?.status || "") === "idle") {
        setCurrentRestoreDialog((current) => current ? {
          ...current,
          accepted: true,
          reconnecting: true,
          error: "",
        } : current);
        return null;
      }
      setCurrentRestoreStatus(result);
      const terminal = currentRestoreTerminal(result);
      const sourceArtifact = result?.artifact || {};
      setCurrentRestoreDialog((current) => {
        const artifact = current?.artifact || {
          id: pending.artifactId,
          createdAt: sourceArtifact.artifact_created_at
            ? formatAuditTimestamp(sourceArtifact.artifact_created_at, lang)
            : "-",
        };
        return {
          ...(current || {}),
          artifact,
          phrase: current?.phrase || CURRENT_RESTORE_CONFIRMATION_PHRASE,
          preflight: current?.preflight || { can_restore: true, reason_codes: [] },
          preflightBusy: false,
          accepted: true,
          reconnecting: false,
          error: "",
        };
      });
      if (terminal) {
        commitCurrentRestorePending(null);
        await refreshMaintenanceBackupProjections();
      }
      return result;
    } catch (error) {
      const expectedRestart = updateApplyRequestIsAmbiguous(error);
      setCurrentRestoreDialog((current) => ({
        ...(current || {}),
        artifact: current?.artifact || {
          id: pending.artifactId,
          createdAt: "-",
        },
        phrase: current?.phrase || CURRENT_RESTORE_CONFIRMATION_PHRASE,
        preflight: current?.preflight || { can_restore: true, reason_codes: [] },
        preflightBusy: false,
        accepted: true,
        reconnecting: expectedRestart,
        error: expectedRestart
          ? ""
          : currentRestoreReasonText(currentRestoreReasonCode(error)),
      }));
      return null;
    } finally {
      currentRestorePollInFlightRef.current = false;
    }
  }

  async function requestCurrentDatabaseRestore(artifact) {
    if (
      !artifact?.id
      || !artifact.canRestore
      || maintenanceBusy
      || maintenanceBackupPendingRef.current
      || currentRestorePendingRef.current
    ) return;
    setCurrentRestoreStatus(null);
    setCurrentRestoreDialog({
      artifact,
      phrase: "",
      preflight: null,
      preflightBusy: true,
      accepted: false,
      reconnecting: false,
      error: "",
    });
    try {
      const preflight = await apiFetch("/system/restore/current/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artifact_id: artifact.id }),
      });
      setCurrentRestoreDialog((current) => (
        current?.artifact?.id === artifact.id
          ? {
              ...current,
              preflight,
              preflightBusy: false,
              error: preflight?.can_restore
                ? ""
                : currentRestoreReasonText(preflight?.reason_codes?.[0]),
            }
          : current
      ));
    } catch (error) {
      const detail = error?.data?.detail;
      const preflight = detail && typeof detail === "object" ? detail.preflight : null;
      setCurrentRestoreDialog((current) => (
        current?.artifact?.id === artifact.id
          ? {
              ...current,
              preflight,
              preflightBusy: false,
              error: currentRestoreReasonText(currentRestoreReasonCode(error)),
            }
          : current
      ));
    }
  }

  async function confirmCurrentDatabaseRestore() {
    const dialog = currentRestoreDialog;
    if (
      !dialog
      || dialog.preflightBusy
      || dialog.accepted
      || dialog.preflight?.can_restore !== true
      || dialog.phrase !== CURRENT_RESTORE_CONFIRMATION_PHRASE
      || currentRestorePendingRef.current
    ) return;
    const pending = commitCurrentRestorePending({
      submissionId: createUpdateApplySubmissionId(),
      artifactId: dialog.artifact.id,
      createdAt: Date.now(),
    });
    if (!pending) return;
    setCurrentRestoreStatus({
      submission_id: pending.submissionId,
      status: "queued",
      phase: "preflight",
      terminal_result: null,
    });
    setCurrentRestoreDialog((current) => current ? {
      ...current,
      accepted: true,
      reconnecting: false,
      error: "",
    } : current);
    try {
      const receipt = await apiFetch("/system/restore/current/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          artifact_id: pending.artifactId,
          submission_id: pending.submissionId,
          confirm: true,
          confirmation_phrase: CURRENT_RESTORE_CONFIRMATION_PHRASE,
        }),
      });
      const restoreStatus = receipt?.restore_status || null;
      if (restoreStatus) setCurrentRestoreStatus(restoreStatus);
      void pollCurrentRestoreStatus(pending);
    } catch (error) {
      if (updateApplyRequestIsAmbiguous(error)) {
        setCurrentRestoreDialog((current) => current ? {
          ...current,
          accepted: true,
          reconnecting: true,
          error: "",
        } : current);
        void pollCurrentRestoreStatus(pending);
      } else {
        commitCurrentRestorePending(null);
        setCurrentRestoreStatus(null);
        setCurrentRestoreDialog((current) => current ? {
          ...current,
          accepted: false,
          reconnecting: false,
          error: currentRestoreReasonText(currentRestoreReasonCode(error)),
        } : current);
      }
    }
  }


  function backupOperationFallback(kind) {
    if (kind === "create") return t.maintenanceBackupCreateFailed;
    if (kind === "delete") return t.maintenanceBackupDeleteFailed;
    return t.maintenanceBackupCheckStatuses?.check_failed || t.maintenanceLoadError;
  }

  function backupOperationSuccess(kind) {
    if (kind === "create") return t.maintenanceBackupCreated;
    if (kind === "delete") return t.maintenanceBackupDeleted;
    return t.maintenanceBackupCheckStatuses?.validated || t.maintenanceDryRunResult;
  }

  async function acceptBackupOperationReceipt(receipt, pendingRecord, { recovered = false } = {}) {
    const state = String(receipt?.state || "");
    const kind = String(receipt?.kind || pendingRecord?.kind || "check");
    const terminal = ["completed", "failed", "interrupted"].includes(state);
    const resultStatus = receipt?.result?.status || state || "failed";
    const presentationResult = {
      kind,
      status: resultStatus,
      state,
      phase: receipt?.phase || "",
      reason: receipt?.reason_code || "",
      result: receipt?.result || null,
      recovering: recovered,
    };
    const presentation = maintenanceBackupOperationResultText(presentationResult, t);
    setMaintenanceBackupResult(presentationResult);
    if (!terminal) {
      maintenanceBackupRecoveryRef.current = recovered;
      setMaintenanceBusy(`backup-${kind}`);
      return false;
    }
    maintenanceBackupRecoveryRef.current = false;
    commitBackupOperationPending(null);
    setMaintenanceBusy((current) => current.startsWith("backup-") ? "" : current);
    if (presentation.successful) {
      showToast({
        variant: "success",
        title: presentation.title || t.maintenanceBackupOperationLabels?.[kind] || t.maintenanceBackupsTitle,
        text: presentation.text || backupOperationSuccess(kind),
      });
    } else {
      showToast({
        variant: "warning",
        title: presentation.title || t.maintenanceBackupOperationLabels?.[kind] || t.maintenanceBackupsTitle,
        text: presentation.text || backupOperationFallback(kind),
      });
    }
    await refreshMaintenanceBackupProjections({
      clampInvalid: kind === "delete",
    });
    return true;
  }

  async function reconcilePendingBackupOperation() {
    if (maintenanceBackupPollInFlightRef.current) return;
    const pending = sanitizeBackupOperationPending(
      maintenanceBackupPendingRef.current,
      Date.now(),
    );
    if (!pending) {
      if (maintenanceBackupPendingRef.current) commitBackupOperationPending(null);
      return;
    }
    if (
      maintenanceBackupAdmissionRef.current
      === pending.submissionId
    ) return;
    maintenanceBackupPollInFlightRef.current = true;
    try {
      const receipt = await apiFetch(
        `/system/backup/operations/${encodeURIComponent(pending.submissionId)}`,
      );
      await acceptBackupOperationReceipt(receipt, pending, {
        recovered: maintenanceBackupRecoveryRef.current,
      });
    } catch (err) {
      if (Number(err?.status || 0) === 404) {
        if (backupOperationWithinAdmissionGrace(pending, Date.now())) {
          setMaintenanceBusy(`backup-${pending.kind}`);
          setMaintenanceBackupResult({
            kind: pending.kind,
            status: "running",
            state: "running",
            reason: "",
            recovering: maintenanceBackupRecoveryRef.current,
          });
          return;
        }
        commitBackupOperationPending(null);
        setMaintenanceBusy((current) => current.startsWith("backup-") ? "" : current);
        setMaintenanceBackupResult({
          kind: pending.kind,
          status: "failed",
          state: "failed",
          reason: "receipt_not_found",
        });
        showToast({
          variant: "warning",
          title: t.maintenanceBackupOperationLabels?.[pending.kind] || t.maintenanceBackupsTitle,
          text: backupOperationFallback(pending.kind),
        });
      } else {
        maintenanceBackupRecoveryRef.current = true;
        setMaintenanceBusy(`backup-${pending.kind}`);
        setMaintenanceBackupResult({
          kind: pending.kind,
          status: "running",
          state: "running",
          reason: "",
          recovering: true,
        });
      }
    } finally {
      maintenanceBackupPollInFlightRef.current = false;
    }
  }

  function closeUpdateApplyDialog() {
    if (!updateApplyDialogRef.current) return;
    setUpdateApplyDialog(null);
  }

  function reconcilePendingUpdateApply(applyData, observedAtMs) {
    const current = updateApplyPendingRef.current;
    if (!current) return "none";
    const result = reconcileUpdateApplyPending(current, applyData, observedAtMs);
    if (result.outcome === "accepted") {
      commitUpdateApplyPending(null);
      setUpdateApplyDialog(null);
      setMaintenanceBusy((value) => value === "update-apply" ? "" : value);
      showToast({ variant: "success", title: t.updateApplyTitle, text: t.updateApplyQueued });
      return result.outcome;
    }
    if (result.outcome === "conflict") {
      setMaintenanceBusy((value) => value === "update-apply" ? "" : value);
      setMaintenanceActionResult({
        flowKey: "update",
        status: "blocked",
        reason: "update_launch_conflict",
        displayReason: t.updateApplyLaunchConflict,
      });
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyLaunchConflict });
      return result.outcome;
    }
    if (result.outcome === "not_accepted") {
      commitUpdateApplyPending(null);
      setMaintenanceBusy((value) => value === "update-apply" ? "" : value);
      setMaintenanceActionResult({
        flowKey: "update",
        status: "blocked",
        reason: "update_launch_not_accepted",
        displayReason: t.updateApplyLaunchNotAccepted,
      });
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyLaunchNotAccepted });
      return result.outcome;
    }
    return result.outcome;
  }

  async function refreshMaintenanceSurface() {
    const tasks = [
      loadMaintenanceOverview(),
      loadUpdateApplySurface({ silent: true }),
    ];
    if (maintenanceBackupDetailOpen || maintenanceBackupDetailRef.current) {
      tasks.push(loadMaintenanceBackupPage(
        maintenanceBackupDetailRef.current?.offset || 0,
        { allowClamp: true, silent: true },
      ));
    }
    await Promise.allSettled(tasks);
  }

  async function loadMaintenanceOverview() {
    if (!canManageMaintenance) return;
    setMaintenanceLoading(true);
    setMaintenanceError("");
    try {
      const overview = await apiFetch("/system/maintenance/overview");
      setMaintenanceOverview(overview);
    } catch (err) {
      setMaintenanceError(humanErrorText(String(err?.message || ""), t.maintenanceLoadError));
    } finally {
      setMaintenanceLoading(false);
    }
  }

  async function loadMaintenanceBackupPage(
    offset,
    { allowClamp = false, silent = false } = {},
  ) {
    if (!canManageMaintenance) return null;
    const safeOffset = Math.max(0, Number(offset || 0));
    if (!silent) setMaintenanceBusy("backup-page");
    try {
      let backupStatus = await apiFetch(
        `/system/restore/status?offset=${safeOffset}&limit=${MAINTENANCE_BACKUP_PAGE_SIZE}`,
      );
      const validOffset = maintenanceBackupValidOffset(
        backupStatus?.total_count,
        backupStatus?.limit || MAINTENANCE_BACKUP_PAGE_SIZE,
        safeOffset,
      );
      if (allowClamp && validOffset !== safeOffset) {
        backupStatus = await apiFetch(
          `/system/restore/status?offset=${validOffset}&limit=${MAINTENANCE_BACKUP_PAGE_SIZE}`,
        );
      }
      setMaintenanceBackupDetail(backupStatus);
      return backupStatus;
    } catch (err) {
      showToast({
        variant: "warning",
        title: t.maintenanceBackupsTitle,
        text: humanErrorText(String(err?.message || ""), t.maintenanceLoadError),
      });
      return null;
    } finally {
      if (!silent) setMaintenanceBusy("");
    }
  }

  async function refreshMaintenanceBackupProjections({ clampInvalid = false } = {}) {
    const currentOffset = maintenanceBackupDetailRef.current?.offset || 0;
    const tasks = [loadMaintenanceOverview()];
    if (maintenanceBackupDetailOpen || maintenanceBackupDetailRef.current) {
      tasks.push(loadMaintenanceBackupPage(currentOffset, {
        allowClamp: clampInvalid,
        silent: true,
      }));
    }
    await Promise.allSettled(tasks);
  }

  async function openMaintenanceBackupDetail() {
    if (maintenanceBusy) return;
    setMaintenanceBackupDetailOpen(true);
    await loadMaintenanceBackupPage(
      maintenanceBackupDetailRef.current?.offset || 0,
      { allowClamp: true },
    );
  }

  function closeMaintenanceBackupDetail() {
    if (maintenanceBusy || currentRestorePending) return;
    setMaintenanceBackupDetailOpen(false);
  }

  async function loadUpdateApplySurface({ silent = false } = {}) {
    if (!canManageMaintenance || updatePollInFlightRef.current) return null;
    updatePollInFlightRef.current = true;
    if (!silent) setMaintenanceBusy("update-status");
    try {
      const pending = sanitizeUpdateApplyPending(updateApplyPendingRef.current, Date.now());
      const [statusResult, applyResult] = await Promise.allSettled([
        apiFetch("/system/update/status"),
        apiFetch("/system/update/apply/status"),
      ]);
      const observedAtMs = monotonicWallNow();
      setUpdateApplyClockMs(observedAtMs);

      if (statusResult.status === "fulfilled") {
        setUpdateStatus(statusResult.value);
        setUpdateTransportErrors((current) => ({ ...current, update: null }));
      } else {
        setUpdateTransportErrors((current) => ({
          ...current,
          update: safeUpdateTransportError(statusResult.reason, t.updateApplyConnection),
        }));
      }

      if (applyResult.status === "fulfilled") {
        setUpdateApplyStatus(applyResult.value);
        setUpdateApplyReconnectSnapshot(updateApplyReconnectTiming(applyResult.value, observedAtMs));
        setUpdateTransportErrors((current) => ({ ...current, apply: null }));
        if (pending) reconcilePendingUpdateApply(applyResult.value, observedAtMs);
      } else {
        setUpdateTransportErrors((current) => ({
          ...current,
          apply: safeUpdateTransportError(applyResult.reason, t.updateApplyConnection),
        }));
      }
      return { statusResult, applyResult };
    } finally {
      updatePollInFlightRef.current = false;
      if (!silent) setMaintenanceBusy((current) => current === "update-status" ? "" : current);
    }
  }

  async function runMaintenanceDryRun(flowKey, bodyOverride = null) {
    const config = MAINTENANCE_DRY_RUN_ENDPOINTS[flowKey];
    if (!config || maintenanceBusy) return;
    setMaintenanceBusy(flowKey);
    setMaintenanceActionResult(null);
    try {
      const result = await apiFetch(config.path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bodyOverride || config.body),
      });
      setMaintenanceActionResult({ flowKey, status: result?.status || "ok", reason: result?.reason || result?.blocked_reason || "" });
      if (flowKey === "restore") {
        setMaintenanceBackupResult({ kind: "check", status: result?.status || "ok", reason: result?.reason || result?.blocked_reason || "" });
      }
      showToast({ variant: "success", title: t.maintenanceDryRunResult, text: maintenanceStatusText(result?.status, t) });
      await loadMaintenanceOverview();
    } catch (err) {
      const message = humanErrorText(String(err?.message || ""), t.maintenanceLoadError);
      setMaintenanceActionResult({ flowKey, status: "blocked", reason: message });
      showToast({ variant: "warning", title: t.maintenanceDryRun, text: message });
    } finally {
      setMaintenanceBusy("");
    }
  }

  function requestDbAdoptionApply() {
    if (maintenanceBusy || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "db-adoption-apply",
      title: t.maintenanceDbAdoptionApply,
      text: t.maintenanceDbAdoptionApplyConfirm,
      confirmLabel: t.maintenanceDbAdoptionApply,
      danger: false,
      onConfirm: performDbAdoptionApply,
    });
  }

  async function performDbAdoptionApply() {
    if (maintenanceBusy || currentRestorePending) return;
    setMaintenanceBusy("db-adoption-apply");
    setMaintenanceConfirm(null);
    setMaintenanceActionResult(null);
    try {
      const result = await apiFetch("/system/db-adoption/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      setMaintenanceActionResult({
        flowKey: "db_adoption",
        status: result?.status || "completed",
        reason: result?.reason || "",
      });
      showToast({
        variant: "success",
        title: t.maintenanceDbAdoptionApply,
        text: t.maintenanceDbAdoptionApplied,
      });
      await loadMaintenanceOverview();
    } catch {
      setMaintenanceActionResult({
        flowKey: "db_adoption",
        status: "failed",
        reason: t.maintenanceDbAdoptionApplyFailed,
      });
      showToast({
        variant: "warning",
        title: t.maintenanceDbAdoptionApply,
        text: t.maintenanceDbAdoptionApplyFailed,
      });
    } finally {
      setMaintenanceBusy("");
    }
  }

  async function createMaintenanceBackup() {
    if (maintenanceBusy || maintenanceBackupPendingRef.current || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "backup-create",
      title: t.maintenanceBackupCreate,
      text: t.maintenanceBackupCreateConfirm,
      confirmLabel: t.maintenanceBackupCreateShort,
      danger: false,
      onConfirm: performMaintenanceBackupCreate,
    });
  }

  async function performMaintenanceBackupCreate() {
    await performMaintenanceBackupOperation("create", null);
  }

  function requestCheckMaintenanceBackup(artifact) {
    if (!artifact?.id || maintenanceBusy || maintenanceBackupPendingRef.current || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "backup-check",
      title: t.maintenanceBackupCheck,
      text: t.maintenanceBackupCheckConfirm.replace("{date}", artifact.createdAt || "-"),
      confirmLabel: t.maintenanceBackupCheck,
      danger: false,
      artifact,
      onConfirm: () => performMaintenanceBackupOperation("check", artifact),
    });
  }

  function requestDeleteMaintenanceBackup(artifact) {
    if (!artifact?.id || maintenanceBusy || maintenanceBackupPendingRef.current || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "backup-delete",
      title: t.maintenanceBackupDelete,
      text: t.maintenanceBackupDeleteConfirm.replace("{date}", artifact.createdAt || "-"),
      confirmLabel: t.maintenanceBackupDelete,
      danger: true,
      artifact,
      onConfirm: () => performMaintenanceBackupOperation("delete", artifact),
    });
  }

  async function performMaintenanceBackupOperation(kind, artifact) {
    if (
      maintenanceBusy
      || maintenanceBackupPendingRef.current
      || currentRestorePending
      || !["create", "check", "delete"].includes(kind)
      || (kind !== "create" && !artifact?.id)
    ) return;
    const submissionId = createUpdateApplySubmissionId();
    const pending = createBackupOperationPending(
      kind,
      artifact?.id || "",
      submissionId,
      Date.now(),
    );
    if (!pending || !commitBackupOperationPending(pending)) {
      showToast({
        variant: "warning",
        title: t.maintenanceBackupOperationLabels?.[kind] || t.maintenanceBackupsTitle,
        text: backupOperationFallback(kind),
      });
      return;
    }
    maintenanceBackupRecoveryRef.current = false;
    setMaintenanceBusy(`backup-${kind}`);
    setMaintenanceConfirm(null);
    setMaintenanceActionResult(null);
    setMaintenanceBackupResult(null);
    let endpoint = "/system/backup/create";
    let body = { confirm: true, submission_id: pending.submissionId };
    if (kind === "check") {
      endpoint = "/system/restore/apply";
      body = { ...body, artifact_id: artifact.id };
    } else if (kind === "delete") {
      endpoint = `/system/restore/artifacts/${encodeURIComponent(artifact.id)}/delete`;
    }
    maintenanceBackupAdmissionRef.current = pending.submissionId;
    try {
      const receipt = await apiFetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (
        maintenanceBackupAdmissionRef.current
        === pending.submissionId
      ) {
        maintenanceBackupAdmissionRef.current = null;
      }
      await acceptBackupOperationReceipt(receipt, pending);
    } catch (error) {
      if (
        maintenanceBackupAdmissionRef.current
        === pending.submissionId
      ) {
        maintenanceBackupAdmissionRef.current = null;
      }
      if (!updateApplyRequestIsAmbiguous(error)) {
        commitBackupOperationPending(null);
        setMaintenanceBackupResult({
          kind,
          status: "failed",
          state: "failed",
          reason: "request_rejected",
        });
        showToast({
          variant: "warning",
          title: t.maintenanceBackupOperationLabels?.[kind]
            || t.maintenanceBackupsTitle,
          text: backupOperationFallback(kind),
        });
        return;
      }
      setMaintenanceBackupResult({
        kind,
        status: "running",
        state: "running",
        recovering: true,
      });
      maintenanceBackupRecoveryRef.current = true;
      await reconcilePendingBackupOperation();
    } finally {
      if (
        maintenanceBackupAdmissionRef.current
        === pending.submissionId
      ) {
        maintenanceBackupAdmissionRef.current = null;
      }
      if (!maintenanceBackupPendingRef.current) {
        setMaintenanceBusy((current) => current === `backup-${kind}` ? "" : current);
      }
    }
  }

  async function runUpdateCheck() {
    if (maintenanceBusy) return;
    setMaintenanceBusy("update");
    setMaintenanceActionResult(null);
    try {
      const result = await apiFetch("/system/update/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      setUpdateStatus(result);
      setUpdateTransportErrors((current) => ({ ...current, update: null }));
      await loadUpdateApplySurface({ silent: true });
      const checkStatus = String(result?.status || "").toLowerCase();
      const checkFailed = ["check_failed", "blocked", "not_configured", "failed"].includes(checkStatus);
      const checkResultKey = checkStatus === "current"
        ? "current"
        : ["update_available", "available"].includes(checkStatus)
          ? "available"
          : checkFailed
            ? "blocked"
            : "unknown";
      const checkStatusText = maintenanceStatusText(result?.status, t);
      showToast({
        variant: checkFailed ? "warning" : "success",
        title: t.updateApplyHeadlines?.[checkResultKey] || checkStatusText,
        text: t.updateApplySummaries?.[checkResultKey] || checkStatusText,
      });
    } catch (err) {
      const transportError = safeUpdateTransportError(err, t.updateApplyUnavailable);
      const message = transportError.message;
      setUpdateTransportErrors((current) => ({ ...current, update: transportError }));
      setMaintenanceActionResult({ flowKey: "update", status: "blocked", reason: message });
      showToast({ variant: "warning", title: t.updateApplyCheck, text: message });
    } finally {
      setMaintenanceBusy("");
    }
  }

  function startUpdateApply() {
    if (maintenanceBusy || updateApplyPendingRef.current) return;
    const candidate = updateApplyCandidateSnapshot(updateStatus);
    if (!candidate.version || !candidate.commit) {
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyUnavailable });
      return;
    }
    clearToast();
    setUpdateApplyDialog({ phase: "confirm", candidate, error: "", deadlineAtMs: null });
  }

  async function confirmUpdateApply() {
    const dialog = updateApplyDialogRef.current;
    if (!dialog || dialog.phase !== "confirm" || maintenanceBusy || updateApplyPendingRef.current) return;
    const submittedAtMs = Date.now();
    const submissionId = createUpdateApplySubmissionId();
    const pending = createUpdateApplyPending(
      submissionId,
      dialog.candidate,
      submittedAtMs,
    );
    if (!pending) {
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyUnavailable });
      return;
    }
    setUpdateApplyDialog(null);
    setMaintenanceBusy("update-apply");
    setMaintenanceActionResult(null);
    commitUpdateApplyPending(pending);
    try {
      const result = await apiFetch("/system/update/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirm: true,
          submission_id: pending.submissionId,
          expected_manifest_version: dialog.candidate.version,
          expected_manifest_commit: dialog.candidate.commit,
        }),
      });
      setUpdateApplyStatus(result?.apply_status || result);
      setUpdateApplyReconnectSnapshot(updateApplyReconnectTiming(result?.apply_status || result, monotonicWallNow()));
      setUpdateTransportErrors((current) => ({ ...current, apply: null }));
      reconcilePendingUpdateApply(result?.apply_status || result, monotonicWallNow());
      void loadUpdateApplySurface({ silent: true });
    } catch (err) {
      if (updateApplyPendingRef.current && updateApplyRequestIsAmbiguous(err)) {
        void loadUpdateApplySurface({ silent: true });
      } else {
        commitUpdateApplyPending(null);
        const message = safeUpdateLaunchError(err);
        setMaintenanceActionResult({
          flowKey: "update",
          status: "blocked",
          reason: String(err?.code || err?.category || "update_launch_rejected"),
          displayReason: message,
        });
        showToast({ variant: "warning", title: t.updateApplyTitle, text: message });
      }
    } finally {
      setMaintenanceBusy((current) => current === "update-apply" ? "" : current);
    }
  }
  function updateCurrentRestorePhrase(value) {
    setCurrentRestoreDialog((current) => current ? {
      ...current,
      phrase: value,
      error: "",
    } : current);
  }

  function closeMaintenanceConfirmation() {
    setMaintenanceConfirm(null);
  }

  return {
    CURRENT_RESTORE_CONFIRMATION_PHRASE,
    CURRENT_RESTORE_OPERATIONAL_PHASES,
    maintenanceTriggerRef,
    maintenanceDialogRef,
    maintenanceModalOpen,
    maintenanceOverview,
    maintenanceBackupDetail,
    maintenanceBackupDetailOpen,
    maintenanceLoading,
    maintenanceError,
    maintenanceBusy,
    maintenanceActionResult,
    maintenanceBackupResult,
    maintenanceConfirm,
    maintenanceBackupPending,
    currentRestoreDialog,
    currentRestorePending,
    currentRestoreStatus,
    updateStatus,
    updateApplyStatus,
    updateApplyDialog,
    updateApplyOperator,
    updateApplyRunning,
    updateApplyAllowed,
    updateApplyPrimaryText,
    updateApplyErrors,
    updatePeerCheckUnavailable,
    maintenanceBackupOverview,
    maintenanceBackupManager,
    maintenanceDatabase,
    maintenanceWarnings,
    maintenanceChildDialogOpen,
    maintenanceOverall,
    maintenanceBackupResultModel,
    maintenanceBackupProgressText,
    updateApplyLaunchNotice,
    openMaintenanceModal,
    closeMaintenanceModal,
    closeMaintenanceConfirmation,
    currentRestoreReasonText,
    currentRestoreFailedPhase,
    currentRestoreTerminal,
    currentRestoreTerminalText,
    closeCurrentRestoreDialog,
    requestCurrentDatabaseRestore,
    confirmCurrentDatabaseRestore,
    updateCurrentRestorePhrase,
    refreshMaintenanceSurface,
    loadMaintenanceBackupPage,
    openMaintenanceBackupDetail,
    closeMaintenanceBackupDetail,
    runMaintenanceDryRun,
    requestDbAdoptionApply,
    createMaintenanceBackup,
    requestCheckMaintenanceBackup,
    requestDeleteMaintenanceBackup,
    runUpdateCheck,
    startUpdateApply,
    confirmUpdateApply,
    closeUpdateApplyDialog,
  };
}
