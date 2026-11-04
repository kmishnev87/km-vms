"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { newStorageOperationId, blockerText, errorDetailText, archiveRootPath, activationProgressTone, activationProgressStatusText, activationProgressReasonText, activationProgressItems, ACTIVATION_ACK_KEY } from "./storagePagePresentation";
import { migrationScenarioModel, publishStorageMigrationActivity, archiveIntegrityFindingPresentation, archiveIntegrityActionContract } from "./storageOperations";

export default function useStorageIntegrityController({ diagnosticsPermission, copy, language, loadStatus, requireFreshStorageStatus }) {
  const [integrityDialogOpen, setIntegrityDialogOpen] = useState(false);
  const [integrityScan, setIntegrityScan] = useState(null);
  const [integrityFindings, setIntegrityFindings] = useState([]);
  const [integrityNextCursor, setIntegrityNextCursor] = useState(null);
  const [integrityBusy, setIntegrityBusy] = useState(false);
  const [integrityError, setIntegrityError] = useState("");
  const [integritySelectedFinding, setIntegritySelectedFinding] = useState(null);
  const [integrityPlan, setIntegrityPlan] = useState(null);



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
    if (!requireFreshStorageStatus()) return;
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
    if (!requireFreshStorageStatus()) return;
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


  return { integrityDialogOpen, setIntegrityDialogOpen, integrityScan, setIntegrityScan, integrityFindings, setIntegrityFindings, integrityNextCursor, setIntegrityNextCursor, integrityBusy, setIntegrityBusy, integrityError, setIntegrityError, integritySelectedFinding, setIntegritySelectedFinding, integrityPlan, setIntegrityPlan, loadIntegrityFindingPage, openIntegrityDialog, startIntegrityScan, cancelIntegrityScan, loadMoreIntegrityFindings, prepareIntegrityAction, applyIntegrityAction, clearIntegrityPlan };
}
