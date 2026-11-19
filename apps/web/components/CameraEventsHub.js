"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname, useRouter } from "next/navigation";
import { apiFetch, captureAuthSession } from "../lib/api";
import { useAuthSession } from "../lib/useAuthSession";
import { useI18n } from "../lib/i18n";
import { formatProductDateTime } from "../lib/timezone";
import { AI_EVENT_CONDITIONS, CAMERA_EVENT_KINDS, CAMERA_EVENT_REFRESH, CAMERA_EVENT_OPEN, CAMERA_EVENT_HUB_OPEN, eventErrorKey, eventHistoryQuery, eventLabelKey, eventObjectLabelKeys } from "../lib/cameraEvents";
import AiEventDetails from "./AiEventDetails";
import CameraEventPreferences from "./CameraEventPreferences";
import { mergeEventPreferenceDraft } from "../lib/cameraEventPreferences";
import { OperationDialog, OperationToast } from "./OperationFeedback";
import { eventIsSelected, filteredSelectionBody, selectedEventCount, snapshotHistoryQuery, toggleSnapshotEvent } from "../lib/cameraEventSelection";
import { silenceWatchedCameraEvent } from "../lib/cameraEventAttention";

let cached = { session: "", cursor: null, unread: 0 };
const headers = { "Content-Type": "application/json" };

function EventBell() {
  return <img className="topNavIconImage cameraEventsBell" src="/assets/icons/ui/events-v2.png" width="128" height="128" alt="" aria-hidden="true" draggable="false" />;
}

function EventActionIcon({ action }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" data-event-action={action}>
    {action === "live" ? <g strokeWidth="1.7"><rect x="2.5" y="3" width="19" height="13" rx="1.5"/><path d="M12 16V20M7.5 20h9"/></g>
      : action === "archive" ? <g transform="translate(-.75 -.71875) scale(1.0625)"><circle cx="10.5" cy="11.5" r="8"/><path d="M10.5 7v4.5l3.5 2M17 5.5h4M18.5 9h3M18.5 12.5h3M17.5 16h4"/></g>
      : action === "refresh" ? <><path d="M19.5 10A8 8 0 1 0 19 17M20 3v7h-7"/></>
      : action === "delete" ? <><path d="M4.2 6.8h15.6M8.9 6.8V4.5h6.2v2.3M6.7 7.2 7.6 19c.1 1.05 1 1.9 2.05 1.9h4.7c1.05 0 1.95-.85 2.05-1.9l.9-11.8M10.1 10.7v6.6M13.9 10.7v6.6"/></>
      : <><path d="M2 11.5S5.5 6 11 6s9 5.5 9 5.5S16.5 17 11 17s-9-5.5-9-5.5Z"/><circle cx="11" cy="11.5" r="2.5"/><path d="m15.5 19 2 2 4-5"/></>}
  </svg>;
}

export default function CameraEventsHub({ currentUser }) {
  const { t, locale } = useI18n();
  const session = useAuthSession();
  const router = useRouter();
  const pathname = usePathname();
  const enabled = Boolean(currentUser?.permissions?.includes("view_live"));
  const manage = Boolean(currentUser?.permissions?.includes("manage_cameras"));
  const archive = Boolean(currentUser?.permissions?.includes("view_timeline"));
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState("history");
  const [unread, setUnread] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [filters, setFilters] = useState({ camera: "", kind: "", days: 7, unread: false });
  const [aiFiltersOpen, setAiFiltersOpen] = useState(false);
  const [ruleDraft, setRuleDraft] = useState("");
  const [detail, setDetail] = useState(null);
  const [offset, setOffset] = useState(0);
  const [history, setHistory] = useState(null);
  const [cameras, setCameras] = useState([]);
  const [preference, setPreference] = useState(null);
  const [selection, setSelection] = useState([]);
  const [selectionSnapshot, setSelectionSnapshot] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [errorToast, setErrorToast] = useState(null);
  const [eventToast, setEventToast] = useState(null);
  const preferenceServerRef = useRef(null);
  const generation = useRef(0);
  const selectionGeneration = useRef(0);
  const selectPageRef = useRef(null);
  const eventListRef = useRef(null);
  const [scrollbarWidth, setScrollbarWidth] = useState(0);
  const openRef = useRef(open);
  openRef.current = open;
  const query = useMemo(() => snapshotHistoryQuery(eventHistoryQuery(filters, offset), offset, selectionSnapshot),
    [filters, offset, selectionSnapshot?.id]);

  function clearSelection() { selectionGeneration.current++; setSelection([]); setSelectionSnapshot(null); }

  function receivePreferences(value, force = false) {
    const previous = preferenceServerRef.current;
    preferenceServerRef.current = value;
    // Refresh another client's saved settings without erasing an unsaved draft.
    setPreference(draft => mergeEventPreferenceDraft(draft, previous, value, force));
  }

  function showError(error) {
    setErrorToast({ id: `camera-events-${Date.now()}`, title: t(`cameraEvents.errors.${eventErrorKey(error)}`), tone: "error" });
  }

  useEffect(() => {
    setOpen(false); setDetail(null); setEventToast(null); setHistory(null); setPreference(null); clearSelection(); setDeleteTarget(null); setBusy(false);
    preferenceServerRef.current = null;
    const owner = ++generation.current;
    if (!enabled || !session) { setUnread(0); return undefined; }
    if (cached.session !== session) cached = { session, cursor: null, unread: 0 };
    setUnread(cached.unread);
    const controller = new AbortController();
    const current = captureAuthSession();
    let timer;
    async function watch() {
      while (!controller.signal.aborted && current() && generation.current === owner) {
        try {
          const params = new URLSearchParams({ wait: cached.cursor ? "25" : "0" });
          if (cached.cursor) params.set("after", cached.cursor);
          const value = await apiFetch(`/camera-events/updates?${params}`, { signal: controller.signal });
          if (controller.signal.aborted || !current() || generation.current !== owner) return;
          cached = { session, cursor: value.cursor, unread: value.unread_count };
          setUnread(value.unread_count);
          if (value.preferences) receivePreferences(value.preferences);
          if (value.items?.length) {
            const notifications = value.items.filter(item => item.notify && !silenceWatchedCameraEvent(item, document));
            if (notifications.length && document.visibilityState === "visible" && !openRef.current) setEventToast({ ...notifications.at(-1), timezone: value.timezone });
            setRefresh(n => n + 1);
            window.dispatchEvent(new CustomEvent(CAMERA_EVENT_REFRESH));
          } else if (value.changed) {
            setRefresh(n => n + 1);
            window.dispatchEvent(new CustomEvent(CAMERA_EVENT_REFRESH));
          }
        } catch (error) {
          if (controller.signal.aborted || !current()) return;
          await new Promise(resolve => { timer = window.setTimeout(resolve, 30000); });
        }
      }
    }
    watch();
    return () => { generation.current++; controller.abort(); window.clearTimeout(timer); };
  }, [enabled, session]);

  useEffect(() => {
    if (!enabled || !session) return undefined;
    const show = () => { setTab("history"); setOpen(true); };
    window.addEventListener(CAMERA_EVENT_HUB_OPEN, show);
    // Run after the authentication effect resets state, including on a new tab.
    if (new URLSearchParams(window.location.search).get("camera_events") === "1") show();
    return () => window.removeEventListener(CAMERA_EVENT_HUB_OPEN, show);
  }, [enabled, session]);

  useEffect(() => {
    if (!eventToast) return undefined;
    const timer = window.setTimeout(() => setEventToast(null), 8000);
    return () => window.clearTimeout(timer);
  }, [eventToast]);

  useEffect(() => {
    if (!open || !session) return undefined;
    const controller = new AbortController();
    const current = captureAuthSession();
    Promise.all([apiFetch("/camera-events/cameras", { signal: controller.signal }), apiFetch("/camera-events/preferences", { signal: controller.signal })])
      .then(([cameraData, prefs]) => { if (!controller.signal.aborted && current()) { setCameras(cameraData.items || []); receivePreferences(prefs); } })
      .catch(error => { if (!controller.signal.aborted && current()) showError(error); });
    return () => controller.abort();
  }, [open, session]);

  useEffect(() => {
    if (!open || tab !== "history") return undefined;
    const controller = new AbortController();
    const current = captureAuthSession();
    setLoading(true);
    apiFetch(`/camera-events?${query}`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted && current()) {
        setHistory(value); setUnread(value.unread_count); setSelection(previous => previous.filter(id => value.items.some(item => item.id === id)));
        setSelectionSnapshot(previous => previous && value.selection?.id === previous.id
          ? { ...previous, visible_ids: value.selection.visible_ids } : previous);
        if (offset && offset >= value.total) setOffset(Math.max(0, Math.floor((value.total - 1) / 25) * 25));
      }
    }).catch(error => { if (!controller.signal.aborted && current()) {
      if (eventErrorKey(error) === "event_selection_expired") clearSelection();
      showError(error);
    } })
      .finally(() => { if (!controller.signal.aborted && current()) setLoading(false); });
    return () => controller.abort();
  }, [open, tab, query, refresh, session]);

  async function act(action) {
    if (busy) return;
    const current = captureAuthSession();
    setBusy(true);
    try { await action(current); } catch (error) { if (current()) showError(error); }
    finally { if (current()) setBusy(false); }
  }

  async function openEvent(event, mode) {
    await act(async current => {
      const target = await apiFetch(`/camera-events/${event.id}/target?mode=${mode}`);
      if (!current()) return;
      // Read bookkeeping must not prevent opening an already authorized video.
      try { await apiFetch("/camera-events/read", { method: "POST", headers, body: JSON.stringify({ ids: [event.id] }) }); }
      catch (error) { if (current()) showError(error); }
      if (!current()) return;
      setOpen(false); setDetail(null); setEventToast(null);
      router.push(target.url);
      if (pathname === (mode === "live" ? "/live" : "/chronology")) window.dispatchEvent(new CustomEvent(CAMERA_EVENT_OPEN, { detail: { id: event.id, mode } }));
    });
  }

  async function readSelection(ids) {
    const snapshot = selectionSnapshot;
    await act(async current => {
      await apiFetch(snapshot ? `/camera-events/selections/${snapshot.id}/read` : "/camera-events/read",
        { method: "POST", headers, body: JSON.stringify(snapshot ? { excluded_ids: snapshot.excluded_ids } : { ids }) });
      if (current()) { clearSelection(); setRefresh(n => n + 1); const value = await apiFetch("/camera-events/updates?wait=0"); if (current()) setUnread(value.unread_count); }
    });
  }

  async function readAll() {
    const through = history?.read_cursor;
    if (!through) return;
    await act(async current => {
      await apiFetch("/camera-events/read-all", { method: "POST", headers, body: JSON.stringify({ through }) });
      if (current()) {
        clearSelection(); setOffset(0); setRefresh(n => n + 1);
        const value = await apiFetch("/camera-events/updates?wait=0");
        if (current()) setUnread(value.unread_count);
      }
    });
  }

  function requestDelete() {
    if (busy || loading || !manage) return;
    if (selectionSnapshot) setDeleteTarget({ snapshot: { id: selectionSnapshot.id, excluded_ids: [...selectionSnapshot.excluded_ids] }, count: selectedCount });
    else if (selection.length) setDeleteTarget({ ids: [...selection], count: selection.length });
    else if (history?.read_cursor && history.history_count) setDeleteTarget({ through: history.read_cursor, count: history.history_count });
  }

  async function deleteEvents() {
    const target = deleteTarget;
    if (!target || busy) return;
    await act(async current => {
      const value = await apiFetch(target.snapshot ? `/camera-events/selections/${target.snapshot.id}/delete` : target.ids ? "/camera-events/delete" : "/camera-events/delete-all", {
        method: "POST", headers, body: JSON.stringify(target.snapshot ? { excluded_ids: target.snapshot.excluded_ids } : target.ids ? { ids: target.ids } : { through: target.through }),
      });
      if (!current()) return;
      setDeleteTarget(null); clearSelection(); setRefresh(n => n + 1);
      window.dispatchEvent(new CustomEvent(CAMERA_EVENT_REFRESH));
      setErrorToast({ id: `event-deleted-${Date.now()}`, title: t("cameraEvents.deleted", { count: value.deleted }), tone: "info" });
    });
  }

  function changeFilter(key, value) { clearSelection();
    setFilters(previous => ({
      ...previous, [key]: value,
      ...(key === "source" && value !== "ai" ? { rule: "", condition: "" } : {}),
    }));
    if (key === "source" && value !== "ai") setRuleDraft("");
    setOffset(0);
  }
  const rows = history?.items || [];
  const selectedCount = selectedEventCount(selection, selectionSnapshot);
  const selectedHere = id => eventIsSelected(id, selection, selectionSnapshot);
  const allSelected = rows.length > 0 && rows.every(row => selectedHere(row.id));
  const canSelectFiltered = !selectionSnapshot && allSelected && history?.total > rows.length && history?.read_cursor;
  const bulkReadLabel = t(selectedCount ? "cameraEvents.markSelectedRead" : "cameraEvents.markAllRead");
  const bulkDeleteLabel = t(selectedCount ? "cameraEvents.deleteSelected" : "cameraEvents.deleteAll");
  async function selectFiltered() {
    const through = history?.read_cursor, capturedQuery = query, owner = selectionGeneration.current;
    if (!canSelectFiltered || !through) return;
    await act(async current => {
      const value = await apiFetch("/camera-events/selections", { method: "POST", headers,
        body: JSON.stringify(filteredSelectionBody(capturedQuery, through, rows)) });
      if (current() && owner === selectionGeneration.current) {
        setSelection([]); setSelectionSnapshot({ ...value, query: capturedQuery, excluded_ids: [] });
      }
    });
  }
  function togglePage(checked) { clearSelection(); if (checked) setSelection(rows.map(row => row.id)); }
  function toggleRow(id, checked) {
    if (selectionSnapshot) setSelectionSnapshot(previous => {
      const next = toggleSnapshotEvent(previous, id, checked);
      return selectedEventCount([], next) ? next : null;
    });
    else setSelection(previous => checked ? [...new Set([...previous, id])] : previous.filter(value => value !== id));
  }
  useEffect(() => {
    if (selectPageRef.current) selectPageRef.current.indeterminate = selectedCount > 0 && !allSelected;
  }, [allSelected, selectedCount, rows.length]);
  useEffect(() => {
    const node = eventListRef.current;
    if (!node) return undefined;
    const measure = () => setScrollbarWidth(Math.max(0, node.offsetWidth - node.clientWidth));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [open, tab]);
  function rowActions(row) {
    return <div className="cameraEventsRowActions"><button type="button" className="cameraEventsIconButton" title={t("cameraEvents.watchNow")} aria-label={`${t("cameraEvents.watchNow")}: ${row.camera_name}`} disabled={busy || !row.camera_id} onClick={() => openEvent(row, "live")}><EventActionIcon action="live"/></button>
      {archive ? <button type="button" className="cameraEventsIconButton" title={t("cameraEvents.watchEvent")} aria-label={`${t("cameraEvents.watchEvent")}: ${row.camera_name}`} disabled={busy || !row.camera_id} onClick={() => openEvent(row, "archive")}><EventActionIcon action="archive"/></button> : null}</div>;
  }

  const historyContent = <div className="cameraEventsWorkbench" style={{ "--camera-events-scrollbar": `${scrollbarWidth}px` }}>
    <div className="cameraEventsFilters">
      <label>{t("cameraEvents.camera")}<select disabled={busy} value={filters.camera} onChange={e => changeFilter("camera", e.target.value)}><option value="">{t("cameraEvents.allCameras")}</option>{cameras.map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}</select></label>
      <div className="cameraEventsTypeFilter"><label>{t("cameraEvents.type")}<select disabled={busy} value={filters.kind} onChange={e => changeFilter("kind", e.target.value)}><option value="">{t("cameraEvents.allTypes")}</option>{CAMERA_EVENT_KINDS.map(kind => <option key={kind} value={kind}>{t(`cameraEvents.kinds.${kind}`)}</option>)}</select></label><button className="cameraEventsAiTag" type="button" aria-controls="camera-ai-filters" aria-expanded={aiFiltersOpen} data-active={Boolean(filters.source)} onClick={() => setAiFiltersOpen(value => !value)} title={t("cameraEvents.ai.filters")} aria-label={t("cameraEvents.ai.filters")}>AI</button></div>
      <label>{t("cameraEvents.period")}<select disabled={busy} value={filters.days} onChange={e => changeFilter("days", Number(e.target.value))}>{[1, 7, 30].map(day => <option key={day} value={day}>{t(`cameraEvents.days${day}`)}</option>)}</select></label>
      <div className="cameraEventsFilterActions"><label className="cameraEventsCheck"><input type="checkbox" disabled={busy} checked={filters.unread} onChange={e => changeFilter("unread", e.target.checked)}/><span>{t("cameraEvents.unreadOnly")}</span></label>
      <button className="cameraEventsIconButton" title={t("cameraEvents.refresh")} aria-label={t("cameraEvents.refresh")} type="button" onClick={() => setRefresh(n => n + 1)} disabled={loading || busy}><EventActionIcon action="refresh"/></button>
      </div>
    </div>
    {aiFiltersOpen ? <div className="cameraEventsAiFilters" id="camera-ai-filters">
      <label>{t("cameraEvents.ai.source")}<select disabled={busy} value={filters.source || ""} onChange={event => changeFilter("source", event.target.value)}>
        <option value="">{t("cameraEvents.ai.both")}</option><option value="ai">{t("cameraEvents.ai.fromAi")}</option><option value="camera">{t("cameraEvents.ai.fromCamera")}</option>
      </select></label>
      <label>{t("cameraEvents.ai.condition")}<select disabled={busy || filters.source !== "ai"} value={filters.condition || ""} onChange={event => changeFilter("condition", event.target.value)}>
        <option value="">{t("cameraEvents.ai.anyCondition")}</option>{AI_EVENT_CONDITIONS.map(key => <option key={key} value={key}>{t(`cameraEvents.ai.conditions.${key}`)}</option>)}
      </select></label>
      <form onSubmit={event => { event.preventDefault(); changeFilter("rule", ruleDraft.trim()); }}>
        <label>{t("cameraEvents.ai.rule")}<input type="search" value={ruleDraft} maxLength={48} placeholder={t("cameraEvents.ai.ruleSearch")} disabled={busy || filters.source !== "ai"} onChange={event => setRuleDraft(event.target.value)}/></label>
        <button className="button secondary small" type="submit" disabled={busy || filters.source !== "ai" || ruleDraft.trim() === (filters.rule || "")}>{t("cameraEvents.ai.search")}</button>
      </form>
    </div> : null}
    <div className="cameraEventsHistory">
    <div className="cameraEventsSelection">
      <input ref={selectPageRef} aria-label={t("cameraEvents.selectPage")} type="checkbox" checked={allSelected} disabled={busy || loading || !rows.length} onChange={e => togglePage(e.target.checked)}/>
      <div className="cameraEventsSelectionSummary">
        <span>{t("cameraEvents.selected", { count: selectedCount })}</span>
        <button className="cameraEventsSelectFiltered" type="button" style={{ visibility: selectedCount ? "visible" : "hidden" }}
          disabled={busy || loading || !selectedCount} aria-hidden={!selectedCount} tabIndex={selectedCount ? 0 : -1}
          title={selectionSnapshot ? t("cameraEvents.selectionFrozen") : undefined}
          onClick={() => canSelectFiltered ? selectFiltered() : clearSelection()}>
          {canSelectFiltered ? t("cameraEvents.selectFiltered", { count: history.total }) : t("cameraEvents.clearSelection")}
        </button>
      </div>
      <span className="cameraEventsTotal">{t("cameraEvents.found", { count: history?.total || 0 })}</span>
      <div className="cameraEventsBulkActions"><button className="cameraEventsIconButton" type="button" title={bulkReadLabel} aria-label={bulkReadLabel} disabled={busy || loading || (selectedCount ? false : !unread || !history?.read_cursor)} onClick={() => selectedCount ? readSelection(selection) : readAll()}><EventActionIcon action="read"/></button>{manage ? <button className="cameraEventsIconButton" type="button" title={bulkDeleteLabel} aria-label={bulkDeleteLabel} disabled={busy || loading || !(selectedCount || history?.history_count)} onClick={requestDelete}><EventActionIcon action="delete"/></button> : null}</div>
    </div>
    <div className="cameraEventsList" ref={eventListRef} aria-busy={loading}>
      {loading && !history ? <div className="cameraEventsEmpty">{t("cameraEvents.loading")}</div> : !rows.length ? <div className="cameraEventsEmpty">{t("cameraEvents.empty")}</div> : rows.map(row => <article className={`cameraEventsRow ${row.read ? "isRead" : "isUnread"}`} key={row.id}>
        <input aria-label={t("cameraEvents.selectEvent", { camera: row.camera_name })} type="checkbox" checked={selectedHere(row.id)} disabled={busy || loading || Boolean(selectionSnapshot && !selectionSnapshot.visible_ids.includes(row.id))} title={selectionSnapshot && !selectionSnapshot.visible_ids.includes(row.id) ? t("cameraEvents.selectionFrozen") : undefined} onChange={e => toggleRow(row.id, e.target.checked)}/>
        <span className="cameraEventsReadIndicator" role="img" aria-label={t(row.read ? "cameraEvents.viewed" : "cameraEvents.newEvent")} title={t(row.read ? "cameraEvents.viewed" : "cameraEvents.newEvent")}/>
        <div className="cameraEventsDescription"><strong>{row.camera_name}</strong><div className="cameraEventsKindLine"><span>{t(eventLabelKey(row))}{eventObjectLabelKeys(row).map(key => ` · ${t(key)}`).join("")}</span>{row.ai ? <button className="cameraEventsAiTag" type="button" disabled={busy} onClick={() => setDetail(row)} title={t("cameraEvents.ai.details")} aria-label={t("cameraEvents.ai.details")}>AI</button> : null}</div>{row.ai?.rule_name ? <small>{row.ai.rule_name}{row.ai.false_positive ? ` · ${t("cameraEvents.ai.falsePositive")}` : ""}</small> : row.ai?.false_positive ? <small>{t("cameraEvents.ai.falsePositive")}</small> : null}{row.state_unconfirmed ? <small>{t("cameraEvents.stateUnconfirmed")}</small> : null}{row.source_ambiguous ? <small>{t("cameraEvents.ambiguous")}</small> : null}{row.clock_uncertain ? <small>{t("cameraEvents.clockUncertain")}</small> : null}</div>
        <time dateTime={row.occurred_at}>{formatProductDateTime(row.occurred_at, history?.timezone, locale)}</time>
        {rowActions(row)}
      </article>)}
    </div>
    </div>
    <div className="cameraEventsPager"><span>{t("cameraEvents.retention", { days: history?.retention_days || 30 })}</span><button className="button secondary small" type="button" disabled={busy || loading || !offset} onClick={() => setOffset(n => Math.max(0, n - 25))}>{t("cameraEvents.previous")}</button><button className="button secondary small" type="button" disabled={busy || loading || offset + rows.length >= (history?.total || 0)} onClick={() => setOffset(n => n + 25)}>{t("cameraEvents.next")}</button></div>
  </div>;

  const preferenceContent = <CameraEventPreferences preference={preference} cameras={cameras} busy={busy} onChange={setPreference}
    onSave={() => act(async current => {
      const value = await apiFetch("/camera-events/preferences", { method: "PUT", headers, body: JSON.stringify(preference) });
      if (current()) { receivePreferences(value, true); setErrorToast({ id: `event-saved-${Date.now()}`, title: t("cameraEvents.saved"), tone: "info" }); }
    })}/>;

  if (!enabled) return null;
  return <>
    <button className={`topNavItem topNavButton cameraEventsNav ${open ? "active" : ""}`} type="button" title={t("cameraEvents.title")} aria-label={t("cameraEvents.title")} onClick={() => { setOpen(true); setEventToast(null); }}><EventBell/>{unread > 0 ? <span className="cameraEventsBadge">{unread > 99 ? "99+" : unread}</span> : null}</button>
    {typeof document !== "undefined" ? createPortal(<>
    <OperationToast toast={errorToast} onClose={() => setErrorToast(null)}/>
    {eventToast ? <aside className="cameraEventArrival" role="status"><button className="cameraEventArrivalClose" type="button" aria-label={t("cameraEvents.close")} onClick={() => setEventToast(null)}>×</button><strong>{eventToast.camera_name}</strong><span>{t(eventLabelKey(eventToast))}{eventObjectLabelKeys(eventToast).map(key => ` · ${t(key)}`).join("")}</span><time dateTime={eventToast.occurred_at}>{formatProductDateTime(eventToast.occurred_at, eventToast.timezone, locale)}</time>{rowActions(eventToast)}</aside> : null}
    <OperationDialog dialog={open && !detail ? { id: "camera-events", title: `${t("cameraEvents.title")}${unread ? ` · ${unread}` : ""}`, tone: "neutral", className: "cameraEventsDialog", closeLabel: t("cameraEvents.close"), showFooterClose: false,
      content: <div className="cameraEventsPanel"><div className="cameraEventsTabs" role="tablist" aria-label={t("cameraEvents.title")}>{["history", "notifications"].map(key => <button role="tab" aria-selected={tab === key} type="button" className={tab === key ? "isActive" : ""} key={key} onClick={() => setTab(key)}>{t(`cameraEvents.${key}`)}</button>)}</div>{tab === "history" ? historyContent : preferenceContent}</div>
    } : null} onClose={() => setOpen(false)}/>
    {open && detail ? <AiEventDetails event={detail} timezone={history?.timezone || "UTC"} canReview={manage}
      onClose={() => setDetail(null)} onChanged={() => setRefresh(n => n + 1)} busy={busy}
      onOpen={mode => openEvent(detail, mode)} /> : null}
    <OperationDialog dialog={deleteTarget ? {
      id: "camera-events-delete", title: t("cameraEvents.deleteConfirmTitle"), tone: "warning", presentation: "compact-confirmation",
      message: t(deleteTarget.ids || deleteTarget.snapshot ? "cameraEvents.deleteSelectedConfirm" : "cameraEvents.deleteAllConfirm", { count: deleteTarget.count }),
      detail: t("cameraEvents.deleteScopeHelp"), busy, cancelLabel: t("cameraEvents.cancel"), closeLabel: t("cameraEvents.cancel"),
      confirmLabel: t("cameraEvents.deleteConfirm"), confirmTone: "danger", onConfirm: deleteEvents,
    } : null} onClose={() => setDeleteTarget(null)}/>
    </>, document.body) : null}
  </>;
}
