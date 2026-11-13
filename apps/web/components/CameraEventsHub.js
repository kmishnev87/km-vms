"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { usePathname, useRouter } from "next/navigation";
import { apiFetch, captureAuthSession } from "../lib/api";
import { useAuthSession } from "../lib/useAuthSession";
import { useI18n } from "../lib/i18n";
import { formatProductDateTime } from "../lib/timezone";
import { CAMERA_EVENT_KINDS, CAMERA_EVENT_REFRESH, CAMERA_EVENT_OPEN, eventErrorKey, eventHistoryQuery, eventLabelKey, minuteLabel, minuteValue } from "../lib/cameraEvents";
import { OperationDialog, OperationToast } from "./OperationFeedback";

let cached = { session: "", cursor: null, unread: 0 };
const headers = { "Content-Type": "application/json" };

function EventBell() {
  return <svg className="cameraEventsBell" viewBox="0 0 40 40" aria-hidden="true"><defs><linearGradient id="camera-event-bell" x2="1" y2="1"><stop stopColor="#f7fcff"/><stop offset="1" stopColor="#bad0e2"/></linearGradient></defs><path d="M11 26c3-3 2-5 3-11 1-7 12-7 13 0 1 6 0 8 3 11l-2 3H13z" fill="url(#camera-event-bell)" stroke="#54718a" strokeWidth="1.6"/><path d="M17 32c1 3 5 3 7 0M20 6V4" fill="none" stroke="#54718a" strokeWidth="2" strokeLinecap="round"/></svg>;
}

function EventActionIcon({ action }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
    {action === "live" ? <><rect x="3" y="6" width="12" height="12" rx="2"/><path d="m15 10 6-3v10l-6-3z"/></>
      : action === "archive" ? <><circle cx="12" cy="12" r="8.5"/><path d="M12 6.5V12l3.5 2M4 4v4H.5"/></>
      : action === "refresh" ? <><path d="M19.5 10A8 8 0 1 0 19 17M20 3v7h-7"/></>
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
  const [offset, setOffset] = useState(0);
  const [history, setHistory] = useState(null);
  const [cameras, setCameras] = useState([]);
  const [preference, setPreference] = useState(null);
  const [selection, setSelection] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [errorToast, setErrorToast] = useState(null);
  const [eventToast, setEventToast] = useState(null);
  const [detectorCamera, setDetectorCamera] = useState("");
  const [detectors, setDetectors] = useState(null);
  const generation = useRef(0);
  const selectPageRef = useRef(null);
  const openRef = useRef(open);
  openRef.current = open;
  const query = useMemo(() => eventHistoryQuery(filters, offset), [filters, offset]);

  function showError(error) {
    setErrorToast({ id: `camera-events-${Date.now()}`, title: t(`cameraEvents.errors.${eventErrorKey(error)}`), tone: "error" });
  }

  useEffect(() => {
    setOpen(false); setEventToast(null); setHistory(null); setPreference(null); setSelection([]);
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
          if (value.items?.length) {
            const notifications = value.items.filter(item => item.notify);
            if (notifications.length && document.visibilityState === "visible" && !openRef.current) setEventToast({ ...notifications.at(-1), timezone: value.timezone });
            setRefresh(n => n + 1);
            window.dispatchEvent(new CustomEvent(CAMERA_EVENT_REFRESH));
          } else if (value.changed) {
            setRefresh(n => n + 1);
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
    if (!eventToast) return undefined;
    const timer = window.setTimeout(() => setEventToast(null), 8000);
    return () => window.clearTimeout(timer);
  }, [eventToast]);

  useEffect(() => {
    if (!open || !session) return undefined;
    const controller = new AbortController();
    const current = captureAuthSession();
    Promise.all([apiFetch("/camera-events/cameras", { signal: controller.signal }), apiFetch("/camera-events/preferences", { signal: controller.signal })])
      .then(([cameraData, prefs]) => { if (!controller.signal.aborted && current()) { setCameras(cameraData.items || []); setPreference(prefs); } })
      .catch(error => { if (!controller.signal.aborted && current()) showError(error); });
    return () => controller.abort();
  }, [open, session]);

  useEffect(() => {
    if (!open || tab !== "history") return undefined;
    const controller = new AbortController();
    const current = captureAuthSession();
    setLoading(true);
    apiFetch(`/camera-events?${query}`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted && current()) { setHistory(value); setUnread(value.unread_count); setSelection(previous => previous.filter(id => value.items.some(item => item.id === id))); }
    }).catch(error => { if (!controller.signal.aborted && current()) showError(error); })
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
      setOpen(false); setEventToast(null);
      router.push(target.url);
      if (pathname === (mode === "live" ? "/live" : "/chronology")) window.dispatchEvent(new CustomEvent(CAMERA_EVENT_OPEN, { detail: { id: event.id, mode } }));
    });
  }

  async function readSelection(ids) {
    await act(async current => {
      await apiFetch("/camera-events/read", { method: "POST", headers, body: JSON.stringify({ ids }) });
      if (current()) { setSelection([]); setRefresh(n => n + 1); const value = await apiFetch("/camera-events/updates?wait=0"); if (current()) setUnread(value.unread_count); }
    });
  }

  async function readAll() {
    const through = history?.read_cursor;
    if (!through) return;
    await act(async current => {
      await apiFetch("/camera-events/read-all", { method: "POST", headers, body: JSON.stringify({ through }) });
      if (current()) {
        setSelection([]); setOffset(0); setRefresh(n => n + 1);
        const value = await apiFetch("/camera-events/updates?wait=0");
        if (current()) setUnread(value.unread_count);
      }
    });
  }

  function changeFilter(key, value) { setFilters(previous => ({ ...previous, [key]: value })); setOffset(0); setSelection([]); }
  const rows = history?.items || [];
  const allSelected = rows.length > 0 && rows.every(row => selection.includes(row.id));
  const bulkReadLabel = t(selection.length ? "cameraEvents.markSelectedRead" : "cameraEvents.markAllRead");
  useEffect(() => {
    if (selectPageRef.current) selectPageRef.current.indeterminate = selection.length > 0 && !allSelected;
  }, [allSelected, selection.length, rows.length]);
  function rowActions(row) {
    return <div className="cameraEventsRowActions"><button type="button" className="cameraEventsIconButton" title={t("cameraEvents.watchNow")} aria-label={`${t("cameraEvents.watchNow")}: ${row.camera_name}`} disabled={busy || !row.camera_id} onClick={() => openEvent(row, "live")}><EventActionIcon action="live"/></button>
      {archive ? <button type="button" className="cameraEventsIconButton" title={t("cameraEvents.watchEvent")} aria-label={`${t("cameraEvents.watchEvent")}: ${row.camera_name}`} disabled={busy || !row.camera_id} onClick={() => openEvent(row, "archive")}><EventActionIcon action="archive"/></button> : null}</div>;
  }

  const historyContent = <div className="cameraEventsWorkbench">
    <div className="cameraEventsFilters">
      <label>{t("cameraEvents.camera")}<select value={filters.camera} onChange={e => changeFilter("camera", e.target.value)}><option value="">{t("cameraEvents.allCameras")}</option>{cameras.map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}</select></label>
      <label>{t("cameraEvents.type")}<select value={filters.kind} onChange={e => changeFilter("kind", e.target.value)}><option value="">{t("cameraEvents.allTypes")}</option>{CAMERA_EVENT_KINDS.map(kind => <option key={kind} value={kind}>{t(`cameraEvents.kinds.${kind}`)}</option>)}</select></label>
      <label>{t("cameraEvents.period")}<select value={filters.days} onChange={e => changeFilter("days", Number(e.target.value))}>{[1, 7, 30].map(day => <option key={day} value={day}>{t(`cameraEvents.days${day}`)}</option>)}</select></label>
      <div className="cameraEventsFilterActions"><label className="cameraEventsCheck"><input type="checkbox" checked={filters.unread} onChange={e => changeFilter("unread", e.target.checked)}/>{t("cameraEvents.unreadOnly")}</label>
      <button className="cameraEventsIconButton" title={t("cameraEvents.refresh")} aria-label={t("cameraEvents.refresh")} type="button" onClick={() => setRefresh(n => n + 1)} disabled={loading}><EventActionIcon action="refresh"/></button>
      </div>
    </div>
    <div className="cameraEventsHistory">
    <div className="cameraEventsSelection"><input ref={selectPageRef} aria-label={t("cameraEvents.selectPage")} type="checkbox" checked={allSelected} disabled={!rows.length} onChange={e => setSelection(e.target.checked ? rows.map(row => row.id) : [])}/><div className="cameraEventsSelectionSummary"><span>{t("cameraEvents.selected", { count: selection.length })}</span><span className="cameraEventsTotal">{t("cameraEvents.found", { count: history?.total || 0 })}</span></div><button className="cameraEventsIconButton" type="button" title={bulkReadLabel} aria-label={bulkReadLabel} disabled={busy || (selection.length ? false : !unread || !history?.read_cursor)} onClick={() => selection.length ? readSelection(selection) : readAll()}><EventActionIcon action="read"/></button></div>
    <div className="cameraEventsList" aria-busy={loading}>
      {loading && !history ? <div className="cameraEventsEmpty">{t("cameraEvents.loading")}</div> : !rows.length ? <div className="cameraEventsEmpty">{t("cameraEvents.empty")}</div> : rows.map(row => <article className={`cameraEventsRow ${row.read ? "isRead" : "isUnread"}`} key={row.id}>
        <input aria-label={t("cameraEvents.selectEvent", { camera: row.camera_name })} type="checkbox" checked={selection.includes(row.id)} onChange={e => setSelection(previous => e.target.checked ? [...previous, row.id] : previous.filter(id => id !== row.id))}/>
        <span className="cameraEventsReadIndicator" role="img" aria-label={t(row.read ? "cameraEvents.viewed" : "cameraEvents.newEvent")} title={t(row.read ? "cameraEvents.viewed" : "cameraEvents.newEvent")}/>
        <div className="cameraEventsDescription"><strong>{row.camera_name}</strong><span>{t(eventLabelKey(row))}{row.active === true ? ` · ${t("cameraEvents.active")}` : ""}</span>{row.state_unconfirmed ? <small>{t("cameraEvents.stateUnconfirmed")}</small> : null}{row.source_ambiguous ? <small>{t("cameraEvents.ambiguous")}</small> : null}{row.clock_uncertain ? <small>{t("cameraEvents.clockUncertain")}</small> : null}</div>
        <time dateTime={row.occurred_at}>{formatProductDateTime(row.occurred_at, history?.timezone, locale)}</time>
        {rowActions(row)}
      </article>)}
    </div>
    </div>
    <div className="cameraEventsPager"><span>{t("cameraEvents.retention", { days: history?.retention_days || 30 })}</span><button className="button secondary small" type="button" disabled={loading || !offset} onClick={() => setOffset(n => Math.max(0, n - 25))}>{t("cameraEvents.previous")}</button><button className="button secondary small" type="button" disabled={loading || offset + rows.length >= (history?.total || 0)} onClick={() => setOffset(n => n + 25)}>{t("cameraEvents.next")}</button></div>
  </div>;

  function togglePreferenceList(key, value, allowed) {
    const values = preference[key] === null ? allowed : preference[key] || [];
    setPreference(previous => ({ ...previous, [key]: values.includes(value) ? values.filter(item => item !== value) : [...values, value] }));
  }
  const preferenceContent = preference ? <div className="cameraEventsPreferences">
    <p>{t("cameraEvents.notificationHelp")}</p>
    <label className="cameraEventsCheck"><input type="checkbox" checked={preference.notifications} onChange={e => setPreference(p => ({ ...p, notifications: e.target.checked }))}/>{t("cameraEvents.notifyMe")}</label>
    <fieldset><legend>{t("cameraEvents.camera")}</legend><div className="cameraEventsChoices">{cameras.map(camera => <label className="cameraEventsCheck" key={camera.id}><input type="checkbox" checked={preference.camera_ids === null || preference.camera_ids?.includes(camera.id)} onChange={() => togglePreferenceList("camera_ids", camera.id, cameras.map(c => c.id))}/>{camera.name}</label>)}</div></fieldset>
    <fieldset><legend>{t("cameraEvents.type")}</legend><div className="cameraEventsChoices">{CAMERA_EVENT_KINDS.map(kind => <label className="cameraEventsCheck" key={kind}><input type="checkbox" checked={preference.kinds === null || preference.kinds?.includes(kind)} onChange={() => togglePreferenceList("kinds", kind, CAMERA_EVENT_KINDS)}/>{t(`cameraEvents.kinds.${kind}`)}</label>)}</div></fieldset>
    <div className="cameraEventsSchedule"><label className="cameraEventsCheck"><input type="checkbox" checked={preference.hours_enabled} onChange={e => setPreference(p => ({ ...p, hours_enabled: e.target.checked }))}/>{t("cameraEvents.schedule")}</label><label>{t("cameraEvents.from")}<input type="time" value={minuteLabel(preference.start_minute)} disabled={!preference.hours_enabled} onChange={e => setPreference(p => ({ ...p, start_minute: minuteValue(e.target.value) }))}/></label><label>{t("cameraEvents.to")}<input type="time" value={minuteLabel(preference.end_minute)} disabled={!preference.hours_enabled} onChange={e => setPreference(p => ({ ...p, end_minute: minuteValue(e.target.value) }))}/></label></div>
    <small>{t("cameraEvents.scheduleHelp")}</small>
    <button type="button" className="button secondary" disabled={busy} onClick={() => act(async current => { const value = await apiFetch("/camera-events/preferences", { method: "PUT", headers, body: JSON.stringify(preference) }); if (current()) { setPreference(value); setErrorToast({ id: `event-saved-${Date.now()}`, title: t("cameraEvents.saved"), tone: "info" }); } })}>{t("cameraEvents.save")}</button>
  </div> : <div className="cameraEventsEmpty">{t("cameraEvents.loading")}</div>;

  async function loadDetectors() {
    setDetectors(null);
    await act(async current => { const value = await apiFetch(`/camera-events/cameras/${detectorCamera}/detectors`); if (current()) setDetectors(value); });
  }
  const detectorContent = <div className="cameraEventsDetectors">
    <p>{t("cameraEvents.detectorHelp")}</p>
    <div className="cameraEventsDetectorChooser"><label>{t("cameraEvents.camera")}<select value={detectorCamera} disabled={busy} onChange={e => { setDetectorCamera(e.target.value); setDetectors(null); }}><option value="">{t("cameraEvents.chooseCamera")}</option>{cameras.map(camera => <option key={camera.id} value={camera.id}>{camera.name}</option>)}</select></label><button type="button" className="button secondary small" disabled={busy || !detectorCamera} onClick={loadDetectors}>{busy ? t("cameraEvents.loading") : t("cameraEvents.readSettings")}</button></div>
    <div className="cameraEventsReceiverList">{cameras.map(camera => <div key={camera.id}><strong>{camera.name}</strong><span>{t(`cameraEvents.receiver.${camera.receiver?.status || "pending"}`)}</span></div>)}</div>
    {detectors ? !detectors.supported || !detectors.rules?.length ? <div className="cameraEventsEmpty">{t("cameraEvents.noDetectorControl")}</div> : <div className="cameraEventsDetectorRules">{detectors.rules.map(rule => <div key={rule.id}><div><strong>{t(`cameraEvents.kinds.${rule.kind}`)}</strong><small>{rule.name}</small></div>{rule.can_change ? <label className="cameraEventsCheck"><input type="checkbox" disabled={busy} checked={rule.enabled === true} onChange={e => { const enabledValue = e.target.checked; act(async current => { const value = await apiFetch(`/camera-events/cameras/${detectorCamera}/detectors/${rule.id}`, { method: "PUT", headers, body: JSON.stringify({ enabled: enabledValue, revision: rule.revision }) }); if (current()) setDetectors(value); }); }}/>{rule.enabled ? t("cameraEvents.enabled") : t("cameraEvents.disabled")}</label> : <span>{t("cameraEvents.noDetectorControl")}</span>}</div>)}</div> : null}
  </div>;

  if (!enabled) return null;
  return <>
    <button className={`topNavItem topNavButton cameraEventsNav ${open ? "active" : ""}`} type="button" title={t("cameraEvents.title")} aria-label={t("cameraEvents.title")} onClick={() => { setOpen(true); setEventToast(null); }}><EventBell/>{unread > 0 ? <span className="cameraEventsBadge">{unread > 99 ? "99+" : unread}</span> : null}</button>
    {typeof document !== "undefined" ? createPortal(<>
    <OperationToast toast={errorToast} onClose={() => setErrorToast(null)}/>
    {eventToast ? <aside className="cameraEventArrival" role="status"><button className="cameraEventArrivalClose" type="button" aria-label={t("cameraEvents.close")} onClick={() => setEventToast(null)}>×</button><strong>{eventToast.camera_name}</strong><span>{t(eventLabelKey(eventToast))}</span><time dateTime={eventToast.occurred_at}>{formatProductDateTime(eventToast.occurred_at, eventToast.timezone, locale)}</time>{rowActions(eventToast)}</aside> : null}
    <OperationDialog dialog={open ? { id: "camera-events", title: `${t("cameraEvents.title")}${unread ? ` · ${unread}` : ""}`, tone: "neutral", className: "cameraEventsDialog", closeLabel: t("cameraEvents.close"), showFooterClose: false,
      content: <div className="cameraEventsPanel"><div className="cameraEventsTabs" role="tablist" aria-label={t("cameraEvents.title")}>{["history", "notifications", ...(manage ? ["detection"] : [])].map(key => <button role="tab" aria-selected={tab === key} type="button" className={tab === key ? "isActive" : ""} key={key} onClick={() => setTab(key)}>{t(`cameraEvents.${key}`)}</button>)}</div>{tab === "history" ? historyContent : tab === "notifications" ? preferenceContent : detectorContent}</div>
    } : null} onClose={() => setOpen(false)}/>
    </>, document.body) : null}
  </>;
}
