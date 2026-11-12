"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { newStorageOperationId } from "./storagePagePresentation";
import { useI18n } from "./i18n";

export const REVIEW_ACTIONS = ["recheck", "repair", "delete", "acknowledge", "reopen"];
export const reviewActionPermissions = { recheck: "run_diagnostics", repair: "manage_settings", delete: "delete_recordings", acknowledge: "manage_settings", reopen: "manage_settings" };
export const isReviewRunning = (batch) => ["queued", "running", "cancel_requested"].includes(batch?.state);
const isScanRunning = (scan) => ["queued", "running", "cancel_requested", "interrupted"].includes(scan?.status);
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
  const [integrityBusy, setIntegrityBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [listFailed, setListFailed] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const [batch, setBatch] = useState(null);
  const [lastBatch, setLastBatch] = useState(null);
  const [resultOpen, setResultOpen] = useState(false);
  const sequence = useRef(0);
  const openGeneration = useRef(0);
  const busyRequests = useRef(0);
  const openRef = useRef(false);
  const context = useRef(null);
  context.current = { loadStatus, requireFreshStorageStatus, setOperationToast, t, filters, integrityScan };
  openRef.current = integrityDialogOpen;
  const filtersKey = JSON.stringify(filters);

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
    const { integrityScan: scan, filters: activeFilters } = context.current;
    if (!scan?.scan_id) return;
    const next = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(scan.scan_id)}`);
    if (openRef.current) { setIntegrityScan(next); await loadPage(next.scan_id, activeFilters); }
    await refreshParent();
  }, [loadPage, refreshParent]);

  useJobPolling(integrityDialogOpen && isScanRunning(integrityScan), integrityScan?.scan_id,
    () => apiFetch(`/storage/integrity/scans/${encodeURIComponent(integrityScan.scan_id)}`),
    async (next) => { setIntegrityScan(next); if (!isScanRunning(next)) { await loadPage(next.scan_id, context.current.filters); await refreshParent(); } }, notify);
  useJobPolling(integrityDialogOpen && isReviewRunning(batch), batch?.batch_id,
    () => apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}`),
    async (next) => { setBatch(next); if (!isReviewRunning(next)) { setLastBatch(next); setSelected([]); setAllMatching(false); await refreshReview(); } }, notify);

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
    setConfirmation(null); setSelected([]); setAllMatching(false); setResultOpen(false);
    setFiltersState({ state: "active" }); setPage(emptyPage()); setLoading(false); setListFailed(false);
    if (!diagnosticsPermission.allowed) return;
    await request(async () => {
      const [scan, operation] = await Promise.all([
        apiFetch(typeof scanId === "string" && scanId ? `/storage/integrity/scans/${encodeURIComponent(scanId)}` : "/storage/integrity/scans/latest"),
        apiFetch("/storage/integrity/actions/latest"),
      ]);
      if (!openRef.current || generation !== openGeneration.current) return;
      setIntegrityScan(scan); setExcluded([]); setBatch(operation && (isReviewRunning(operation) || operation.batch_id === batch?.batch_id) ? operation : null);
      setLastBatch(operation && !isReviewRunning(operation) ? operation : null);
      if (scan.scan_id) await loadPage(scan.scan_id, { state: "active" });
    });
  }
  function setFilters(next) {
    sequence.current += 1; setSelected([]); setAllMatching(false); setExcluded([]); setConfirmation(null);
    setFiltersState((previous) => typeof next === "function" ? next(previous) : next);
  }
  function toggleSelected(id) {
    if (allMatching) {
      setExcluded((previous) => previous.includes(id) ? previous.filter((value) => value !== id) : [...previous, id]);
      return;
    }
    setSelected((previous) => previous.includes(id) ? previous.filter((value) => value !== id) : [...previous, id]);
  }
  function togglePage() {
    const ids = page.items.filter((item) => item.actions?.length).map((item) => item.finding_id);
    if (allMatching) {
      setExcluded((previous) => ids.every((id) => !previous.includes(id)) ? Array.from(new Set([...previous, ...ids])) : previous.filter((id) => !ids.includes(id)));
      return;
    }
    setSelected((previous) => ids.every((id) => previous.includes(id)) ? previous.filter((id) => !ids.includes(id)) : Array.from(new Set([...previous, ...ids])));
  }
  async function startIntegrityScan() {
    if (!diagnosticsPermission.allowed || isReviewRunning(batch)) return;
    await request(async () => {
      const next = await apiFetch("/storage/integrity/scans", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idempotency_key: newStorageOperationId("integrity-scan") }) });
      setIntegrityScan(next); setSelected([]); setAllMatching(false);
      setFiltersState({ state: "active" }); setPage(emptyPage()); await refreshParent();
    });
  }
  const cancelIntegrityScan = () => request(async () => setIntegrityScan(await apiFetch(`/storage/integrity/scans/${encodeURIComponent(integrityScan.scan_id)}/cancel`, { method: "POST" })));
  async function prepareAction(action, finding = null, overrideSelection = null) {
    if (!integrityScan?.scan_id || isReviewRunning(batch) || listFailed || !requireFreshStorageStatus()) return;
    const selection = overrideSelection || { filters: { ...filters }, ...(finding ? { ids: [finding.finding_id] } : allMatching ? { exclude_ids: [...excluded] } : { ids: [...selected] }) };
    const selectionRevision = sequence.current;
    await request(async () => {
      const preview = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(integrityScan.scan_id)}/actions/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, selection }) });
      if (selectionRevision !== sequence.current || !openRef.current) return;
      setConfirmation({ ...preview, selection, idempotency_key: newStorageOperationId("integrity-review") }); setResultOpen(false);
    });
  }
  async function applyAction() {
    if (!confirmation || integrityBusy) return;
    await request(async () => {
      const next = await apiFetch(`/storage/integrity/scans/${encodeURIComponent(confirmation.scan_id)}/actions`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: confirmation.action, selection: confirmation.selection, selection_hash: confirmation.selection_hash, idempotency_key: confirmation.idempotency_key, confirm: true }) });
      setBatch(next); setLastBatch(isReviewRunning(next) ? null : next); setConfirmation(null); setSelected([]); setAllMatching(false);
    });
  }
  const cancelBatch = () => request(async () => setBatch(await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}/cancel`, { method: "POST" })));
  const loadMoreResults = () => request(async () => {
    const next = await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}?cursor=${encodeURIComponent(batch.next_cursor)}`);
    setBatch((previous) => ({ ...next, items: [...(previous.items || []), ...(next.items || [])] }));
  });
  async function retryFailed() {
    let ids = [];
    await request(async () => {
      let next = await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}`);
      const rows = [...(next.items || [])];
      while (next.next_cursor) {
        next = await apiFetch(`/storage/integrity/actions/${encodeURIComponent(batch.batch_id)}?cursor=${encodeURIComponent(next.next_cursor)}`);
        rows.push(...(next.items || []));
      }
      ids = rows.filter((row) => ["blocked", "failed", "partial"].includes(row.state)).map((row) => row.finding_id);
    });
    if (ids.length) await prepareAction(batch.action, null, { filters: { state: "all" }, ids });
  }
  const allowedActions = REVIEW_ACTIONS.filter((action) => currentUser?.permissions?.includes(reviewActionPermissions[action]));
  return { integrityDialogOpen, setIntegrityDialogOpen: (value) => { if (!value) { sequence.current += 1; openGeneration.current += 1; openRef.current = false; } setIntegrityDialogOpen(value); }, integrityScan, integrityBusy, openIntegrityDialog,
    startIntegrityScan, cancelIntegrityScan, page, filters, setFilters, selected, allMatching,
    setAllMatching: (value) => { setAllMatching(value); setSelected([]); setExcluded([]); },
    isSelected: (id) => allMatching ? !excluded.includes(id) : selected.includes(id),
    toggleSelected, togglePage, selectedCount: allMatching ? Math.max(0, page.total_count - excluded.length) : selected.length,
    loading, listFailed, confirmation, setConfirmation, prepareAction, applyAction, allowedActions,
    batch, cancelBatch, resultOpen, setResultOpen, loadMoreResults, retryFailed, refreshReview: () => request(refreshReview),
    dismissBatch: () => { setBatch(null); setResultOpen(false); },
    lastBatch, showLastResults: () => { if (lastBatch) { setBatch(lastBatch); setResultOpen(true); } },
    loadMoreIntegrityFindings: () => loadPage(integrityScan?.scan_id, filters, { cursor: page.next_cursor, append: true }) };
}
