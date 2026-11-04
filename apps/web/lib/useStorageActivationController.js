"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { newStorageOperationId, blockerText, errorDetailText, archiveRootPath, activationProgressTone, activationProgressStatusText, activationProgressReasonText, activationProgressItems, ACTIVATION_ACK_KEY } from "./storagePagePresentation";
import { migrationScenarioModel, publishStorageMigrationActivity, archiveIntegrityFindingPresentation, archiveIntegrityActionContract } from "./storageOperations";

export default function useStorageActivationController({ archiveRootActivation, activationModel, archiveRootActivationReason, copy, language, loadStatus, setArchiveRootDialog, setOperationToast, setRootAction, manageSettingsPermission, requireFreshStorageStatus, archivePathText }) {
  const [trackedActivationOperationId, setTrackedActivationOperationId] = useState(null);
  const [dismissedActivationOperationId, setDismissedActivationOperationId] = useState(null);



  useEffect(() => {
    try {
      setDismissedActivationOperationId(window.sessionStorage.getItem(ACTIVATION_ACK_KEY));
    } catch (_) {}
  }, []);

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

  function requestActivateRoot(root) {
    if (!requireFreshStorageStatus()) return;
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
    if (!recovery && !requireFreshStorageStatus()) return;
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


  return { trackedActivationOperationId, setTrackedActivationOperationId, dismissedActivationOperationId, setDismissedActivationOperationId, requestActivateRoot, activateRoot };
}
