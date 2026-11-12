"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { newStorageOperationId } from "./storagePagePresentation";
import { useI18n } from "./i18n";

export const REVIEW_ACTIONS = ["recheck", "repair", "delete", "acknowledge", "reopen"];
export const reviewActionPermissions = { recheck: "run_diagnostics", repair: "manage_settings", delete: "delete_recordings", acknowledge: "manage_settings", reopen: "manage_settings" };
export const isReviewRunning = (batch) => ["queued", "running", "cancel_requested"].includes(batch?.state);
const isScanRunning = (scan) => ["queued", "running", "cancel_requested", "interrupted"].includes(scan?.status);
const isScanReviewable = (scan) => Boolean(scan?.scan_id && !scan.stale && ["completed", "partial"].includes(scan.status));
const emptyPage = () => ({ items: [], total_count: 0, review_counts: {}, cameras: [], roots: [], categories: [] });

export function reviewFailureText(error, t) {
  let reason = typeof error === "string" ? error : error?.detail?.reason_code || error?.reason_code || "";
  if (!reason && error?.message) {
    try { reason = JSON.parse(error.message)?.reason_code || ""; } catch { reason = error.message; }
  }
  const key = `archiveReview.reasons.${String(reason).trim()}`;
  const translated = t(key);
  return translated !== key ? translated : t("archiveReview.requestFailedText");
}

function useJobPolling(enabled, key, read, onData, onError) {
  const callbacks = useRef(null);
  callbacks.current = { read, onData, onError };
  useEffect(() => {
    if (!enabled || !key) return undefined;
    let cancelled = false;
    let inFlight = false;
    let errorShown = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const data = await callbacks.current.read();
        if (!cancelled) { errorShown = false; await callbacks.current.onData(data); }
      } catch (error) {
        if (!cancelled && !errorShown) { errorShown = true; callbacks.current.onError(error); }
      } finally { inFlight = false; }
    };
    const timer = window.setInterval(poll, 1500);
    poll();
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [enabled, key]);
}

export default function useStorageIntegrityController({ diagnosticsPermission, currentUser, loadStatus, requireFreshStorageStatus, setOperationToast }) {
  const { t } = useI18n();
  const [integrityDialogOpen, setIntegrityDialogOpen] = useState(false);
  const [integrityScan, setIntegrityScan] = useState(null);
  const [page, setPage] = useState(emptyPage);
  const [filters, setFiltersState] = useState({ state: "active" });
  const [selected, setSelected] = useState([]);
  const [allMatching, setAllMatching] = useState(false);
  const [excluded, setExcluded] = useState([]);
  const [bulkAction, setBulkActionState] = useState("recheck");
  const [rowActions, setRowActions] = useState({});
  const [integrityBusy, setIntegrityBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [listFailed, setListFailed] = useState(false);
  const [opening, setOpening] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const [batch, setBatch] = useState(null);
  const [lastBatch, setLastBatch] = useState(null);
  const [resultOpen, setResultOpen] = useState(false);
  const sequence = useRef(0);
  const draftRevision = useRef(0);
  const scanStarting = useRef(false);
  const openGeneration = useRef(0);
  const busyRequests = useRef(0);
  const openRef = useRef(false);
  const context = useRef(null);
  context.current = { loadStatus, requireFreshStorageStatus, setOperationToast, t, filters, integrityScan };
  openRef.current = integrityDialogOpen;
  const filtersKey = JSON.stringify(filters);

  const invalidateDraft = useCallback(() => {
    draftRevision.current += 1;
    setConfirmation(null);
  }, []);
  const clearSelection = useCallback(() => {
    invalidateDraft(); setSelected([]); setAllMatching(false); setExcluded([]); setRowActions({});
  }, [invalidateDraft]);

  const notify = useCallback((error) => {
    const { t, setOperationToast } = context.current;
    setOperationToast?.({ id: `integrity-review-${Date.now()}`, title: t("archiveReview.requestFailed"), message: reviewFailureText(error, t), tone: "error" });
  }, []);
  const refreshParent = useCallback(async () => {
    await context.current.loadStatus({ silent: true });
    window.dispatchEvent(new Event("kmvms:storage-runtime-changed"));
  }, []);
  const loadPage = useCallback(async (scanId, nextFilters, { cursor = null, append = false } = {}) => {
    if (!scanId) return;
    const request = ++sequence.current;
    setLoading(true);
    setListFailed(false);
    const query = new URLSearchParams({ limit: "50", ...Object.fromEntries(Object.entries(nextFilters).filter(([, value]) => value !== "" && value != null)) });
    if (cursor) query.set("cursor", cursor);
    try {
      const next = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(scanId)}/findings?${query}`);
      if (request === sequence.current && openRef.current) setPage((previous) => ({ ...emptyPage(), ...next, items: append ? [...previous.items, ...next.items] : next.items || [] }));
    } catch (error) {
      if (request === sequence.current && openRef.current) {
        if (!append) { setPage((previous) => ({ ...previous, items: [], total_count: 0 })); setListFailed(true); }
        notify(error);
      }
    }
    finally { if (request === sequence.current) setLoading(false); }
  }, [notify]);

  useEffect(() => {
    if (!integrityDialogOpen || !integrityScan?.scan_id) return undefined;
    loadPage(integrityScan.scan_id, filters);
    return () => { sequence.current += 1; };
  }, [integrityDialogOpen, integrityScan?.scan_id, filtersKey, loadPage]);

  const refreshReview = useCallback(async () => {
    const generation = openGeneration.current;
    const { filters: activeFilters } = context.current;
    clearSelection();
    const next = await apiFetch("/storage/integrity/scans/latest");
    if (openRef.current && generation === openGeneration.current) {
      setIntegrityScan(next);
      if (next.scan_id) await loadPage(next.scan_id, activeFilters);
      else { setPage(emptyPage()); setListFailed(false); }
    }
    await refreshParent();
  }, [loadPage, refreshParent, clearSelection]);

  useJobPolling(integrityDialogOpen && isScanRunning(integrityScan), integrityScan?.scan_id,
    () => apiFetch(`/storage/integrity/scans/${encodeURIComponent(integrityScan.scan_id)}`),
    async (next) => { invalidateDraft(); setIntegrityScan(next); if (!isScanRunning(next)) { await loadPage(next.scan_id, context.current.filters); await refreshParent(); } }, notify);
  useJobPolling(integrityDialogOpen && isReviewRunning(batch), batch?.batch_id,
    () => apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}`),
    async (next) => { setBatch(next); if (!isReviewRunning(next)) { setLastBatch(next); clearSelection(); await refreshReview(); } }, notify);

  const request = async (work) => {
    busyRequests.current += 1;
    setIntegrityBusy(true);
    try { return await work(); } catch (error) { notify(error); return null; }
    finally { busyRequests.current -= 1; setIntegrityBusy(busyRequests.current > 0); }
  };
  async function openIntegrityDialog(scanId = null) {
    const generation = ++openGeneration.current;
    sequence.current += 1;
    setIntegrityDialogOpen(true); openRef.current = true;
    clearSelection(); setResultOpen(false); setIntegrityScan(null); setBatch(null); setLastBatch(null);
    setBulkActionState("recheck"); setOpening(diagnosticsPermission.allowed);
    setFiltersState({ state: "active" }); setPage(emptyPage()); setLoading(false); setListFailed(false);
    if (!diagnosticsPermission.allowed) return;
    await request(async () => {
      try {
        const [scan, operation] = await Promise.all([
          apiFetch(typeof scanId === "string" && scanId ? `/storage/integrity/scans/${encodeURIComponent(scanId)}` : "/storage/integrity/scans/latest"),
          apiFetch("/storage/integrity/actions/latest"),
        ]);
        if (!openRef.current || generation !== openGeneration.current) return;
        setLoading(Boolean(scan.scan_id));
        setIntegrityScan(scan); setBatch(operation && isReviewRunning(operation) ? operation : null);
        setLastBatch(operation && !isReviewRunning(operation) ? operation : null);
      } catch (error) {
        if (openRef.current && generation === openGeneration.current) setListFailed(true);
        throw error;
      } finally {
        if (generation === openGeneration.current) setOpening(false);
      }
    });
  }
  function setFilters(next) {
    sequence.current += 1; clearSelection();
    setFiltersState((previous) => typeof next === "function" ? next(previous) : next);
  }
  function toggleSelected(id) {
    invalidateDraft();
    setRowActions((previous) => { const next = { ...previous }; delete next[id]; return next; });
    if (allMatching) {
      setExcluded((previous) => previous.includes(id) ? previous.filter((value) => value !== id) : [...previous, id]);
      return;
    }
    setSelected((previous) => previous.includes(id) ? previous.filter((value) => value !== id) : [...previous, id]);
  }
  function togglePage() {
    invalidateDraft();
    const ids = page.items.filter((item) => item.actions?.length).map((item) => item.finding_id);
    const removing = ids.every((id) => allMatching ? !excluded.includes(id) : selected.includes(id));
    if (removing) setRowActions((previous) => Object.fromEntries(Object.entries(previous).filter(([id]) => !ids.includes(id))));
    if (allMatching) {
      setExcluded((previous) => ids.every((id) => !previous.includes(id)) ? Array.from(new Set([...previous, ...ids])) : previous.filter((id) => !ids.includes(id)));
      return;
    }
    setSelected((previous) => ids.every((id) => previous.includes(id)) ? previous.filter((id) => !ids.includes(id)) : Array.from(new Set([...previous, ...ids])));
  }
  async function startIntegrityScan() {
    if (!diagnosticsPermission.allowed || isReviewRunning(batch) || isScanRunning(integrityScan) || scanStarting.current) return;
    const generation = openGeneration.current;
    scanStarting.current = true;
    sequence.current += 1; clearSelection(); setPage(emptyPage()); setLoading(false);
    setBatch(null); setResultOpen(false);
    await request(async () => {
      try {
        const next = await apiFetch("/storage/integrity/scans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idempotency_key: newStorageOperationId("integrity-scan") }) });
        if (openRef.current && generation === openGeneration.current) {
          setIntegrityScan(next); setFiltersState({ state: "active" });
        }
        await refreshParent();
      } catch (error) {
        if (openRef.current && generation === openGeneration.current) setListFailed(true);
        throw error;
      } finally { scanStarting.current = false; }
    });
  }
  const cancelIntegrityScan = () => request(async () => setIntegrityScan(await apiFetch(`/storage/integrity/scans/${encodeURIComponent(integrityScan.scan_id)}/cancel`, { method: "POST" })));
  function setBulkAction(action) {
    if (!REVIEW_ACTIONS.includes(action)) return;
    invalidateDraft(); setBulkActionState(action); setRowActions({});
  }
  function stageRowAction(id, action) {
    const finding = page.items.find((item) => item.finding_id === id);
    if (!isScanReviewable(integrityScan) || isReviewRunning(batch) || (action && !finding?.actions?.includes(action))) return;
    invalidateDraft(); setBatch(null); setResultOpen(false);
    setRowActions((previous) => { const next = { ...previous }; if (action) next[id] = action; else delete next[id]; return next; });
    if (allMatching) setExcluded((previous) => action ? previous.filter((value) => value !== id) : Array.from(new Set([...previous, id])));
    else setSelected((previous) => action ? Array.from(new Set([...previous, id])) : previous.filter((value) => value !== id));
  }
  async function prepareAction(action = bulkAction, overrideSelection = null, overrideActions = null) {
    if (!isScanReviewable(integrityScan) || scanStarting.current || opening || loading || integrityBusy || isReviewRunning(batch) || listFailed || !openRef.current || !requireFreshStorageStatus()) return;
    const selection = overrideSelection || { filters: { ...filters }, ...(allMatching ? { exclude_ids: [...excluded] } : { ids: [...selected] }) };
    const action_overrides = overrideActions || Object.fromEntries(Object.entries(rowActions).filter(([id]) => allMatching ? !excluded.includes(id) : selected.includes(id)));
    const revision = draftRevision.current;
    const scanId = integrityScan.scan_id;
    await request(async () => {
      const preview = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(scanId)}/actions/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, selection, action_overrides }) });
      if (revision !== draftRevision.current || !openRef.current || scanStarting.current || context.current.integrityScan?.scan_id !== scanId || !isScanReviewable(context.current.integrityScan)) return;
      setConfirmation({ ...preview, requested_action: action, selection, action_overrides, revision, idempotency_key: newStorageOperationId("integrity-review") }); setResultOpen(false);
    });
  }
  async function applyAction() {
    if (!confirmation || integrityBusy || busyRequests.current || loading || listFailed || scanStarting.current || !openRef.current || !isScanReviewable(integrityScan) || confirmation.scan_id !== integrityScan.scan_id || confirmation.revision !== draftRevision.current || !requireFreshStorageStatus()) return;
    await request(async () => {
      const next = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(confirmation.scan_id)}/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: confirmation.requested_action, action_overrides: confirmation.action_overrides, selection: confirmation.selection, selection_hash: confirmation.selection_hash, idempotency_key: confirmation.idempotency_key, confirm: true }) });
      if (openRef.current) { setBatch(next); setLastBatch(isReviewRunning(next) ? null : next); clearSelection(); }
    });
  }
  const cancelBatch = () => request(async () => setBatch(await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}/cancel`, { method: "POST" })));
  const loadMoreResults = () => request(async () => {
    const next = await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}?cursor=${encodeURIComponent(batch.next_cursor)}`);
    setBatch((previous) => ({ ...next, items: [...(previous.items || []), ...(next.items || [])] }));
  });
  async function retryFailed() {
    let ids = [];
    let actions = {};
    const revision = draftRevision.current;
    await request(async () => {
      let next = await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}`);
      const rows = [...(next.items || [])];
      while (next.next_cursor) {
        next = await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}?cursor=${encodeURIComponent(next.next_cursor)}`);
        rows.push(...(next.items || []));
      }
      const failed = rows.filter((row) => ["blocked", "failed", "partial"].includes(row.state));
      ids = failed.map((row) => row.finding_id);
      actions = Object.fromEntries(failed.map((row) => [row.finding_id, row.action || batch.requested_action || batch.action]));
    });
    if (ids.length && revision === draftRevision.current && openRef.current) await prepareAction(batch.requested_action || batch.action, { filters: { state: "all" }, ids }, actions);
  }
  const allowedActions = REVIEW_ACTIONS.filter((action) => currentUser?.permissions?.includes(reviewActionPermissions[action]));
  return { integrityDialogOpen, setIntegrityDialogOpen: (value) => { if (!value) { sequence.current += 1; openGeneration.current += 1; openRef.current = false; invalidateDraft(); } setIntegrityDialogOpen(value); }, integrityScan, integrityBusy, openIntegrityDialog,
    startIntegrityScan, cancelIntegrityScan, page, filters, setFilters, selected, allMatching,
    setAllMatching: (value) => { clearSelection(); setAllMatching(value); },
    isSelected: (id) => allMatching ? !excluded.includes(id) : selected.includes(id),
    rowAction: (id) => (allMatching ? !excluded.includes(id) : selected.includes(id)) ? rowActions[id] || bulkAction : "",
    bulkAction, setBulkAction, stageRowAction, hasMixedActions: Object.values(rowActions).some((action) => action !== bulkAction),
    toggleSelected, togglePage, selectedCount: allMatching ? Math.max(0, page.total_count - excluded.length) : selected.length,
    loading, opening, scanStarting: scanStarting.current, listFailed, reviewable: isScanReviewable(integrityScan), confirmation, setConfirmation: invalidateDraft, prepareAction, applyAction, allowedActions,
    batch, cancelBatch, resultOpen, setResultOpen, loadMoreResults, retryFailed, refreshReview: () => request(refreshReview),
    dismissBatch: () => { setBatch(null); setResultOpen(false); },
    lastBatch, showLastResults: () => { if (lastBatch) { setBatch(lastBatch); setResultOpen(true); } },
    loadMoreIntegrityFindings: () => loadPage(integrityScan?.scan_id, filters, { cursor: page.next_cursor, append: true }) };
}
