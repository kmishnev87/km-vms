"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { newStorageOperationId, blockerText, errorDetailText, archiveRootPath, activationProgressTone, activationProgressStatusText, activationProgressReasonText, activationProgressItems, ACTIVATION_ACK_KEY } from "./storagePagePresentation";
import { migrationScenarioModel, publishStorageMigrationActivity, archiveIntegrityFindingPresentation, archiveIntegrityActionContract } from "./storageOperations";

export default function useStorageMigrationController({ archiveRoots, currentArchiveRoot, currentUser, manageSettingsPermission, migrationApplyPermission, requestedMigrationOperationId, copy, language, loadStatus, requireFreshStorageStatus }) {
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
  const [migrationMessage, setMigrationMessage] = useState("");

  const migrationScenario = migrationScenarioModel({
    plan: migrationPlan,
    operation: migrationOperation,
    preparePermission: manageSettingsPermission,
    applyPermission: migrationApplyPermission,
    running: migrationBusy,
  }, language);

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


  function resetMigrationDraft() {
    setMigrationPlan(null);
    setMigrationOperation(null);
    setMigrationMessage("");
    migrationPlanRequestKeyRef.current = null;
    migrationApplyRequestKeyRef.current = null;
    migrationRetryRequestKeyRef.current = null;
    migrationTakeoverRequestKeyRef.current = null;
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
    if (!requireFreshStorageStatus()) return;
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
    if (!requireFreshStorageStatus()) return;
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
    const scope = JSON.stringify([operationId, migrationPlan?.plan_id, migrationPlan?.canonical_hash, "retry"]);
    if (migrationRetryRequestKeyRef.current?.scope !== scope) {
      migrationRetryRequestKeyRef.current = { scope, key: newStorageOperationId("migration-retry") };
    }
    const idempotencyKey = migrationRetryRequestKeyRef.current.key;
    try {
      const result = await apiFetch(`/storage/migration/operations/${encodeURIComponent(operationId)}/retry`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: idempotencyKey }),
      });
      migrationRetryRequestKeyRef.current = null;
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
    const scope = JSON.stringify([operationId, migrationPlan?.plan_id, migrationPlan?.canonical_hash, "cleanup-takeover"]);
    if (migrationTakeoverRequestKeyRef.current?.scope !== scope) {
      migrationTakeoverRequestKeyRef.current = { scope, key: newStorageOperationId("migration-cleanup-takeover") };
    }
    const idempotencyKey = migrationTakeoverRequestKeyRef.current.key;
    try {
      const result = await apiFetch(`/storage/migration/operations/${encodeURIComponent(operationId)}/cleanup-takeover`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idempotency_key: idempotencyKey, confirm: true }),
      });
      migrationTakeoverRequestKeyRef.current = null;
      if (result?.plan) setMigrationPlan(result.plan);
      if (result?.operation) setMigrationOperation(result.operation);
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.migrationCleanupTakeoverFailed, language));
    } finally {
      setMigrationBusy(false);
    }
  }


  return { migrationDialogOpen, setMigrationDialogOpen, migrationSourceRootId, setMigrationSourceRootId, migrationTargetRootId, setMigrationTargetRootId, migrationPlan, setMigrationPlan, migrationOperation, setMigrationOperation, migrationBusy, setMigrationBusy, migrationMessage, setMigrationMessage, migrationScenario, openMigrationDialog, resetMigrationDraft, selectMigrationSource, selectMigrationTarget, prepareMigrationPlan, applyMigration, cancelMigration, retryMigration, takeoverMigrationCleanup };
}
