"use client";

import { useEffect, useRef, useState } from "react";
import { CheckIcon } from "./CompactActionIcons";
import { RecordingUiIcon } from "./RecordingUiIcons";
import { aiRuleCount } from "../lib/aiRules";
import {
  canUseAI, canUseCameraEvents, draftForCamera, eventKinds, inlinePolicyControls,
  inlineSaveProblem, profilesFor, statusView, updateLinkedSource, updateMode, updateProfile, updateSource,
} from "../lib/recordingCenter";

const Blank = ({hint}) => <span className="recCellBlank" title={hint || undefined}>—</span>;
const cell = (name, label, child) => <div className={"recPolicyCell recCell-" + name} data-cell={name}>
  <span className="recMobileLabel">{label}</span>{child}</div>;

function ConditionMenu({ row, draft, change, t, r, disabled }) {
  const wrapper = useRef(null);
  const menuButton = useRef(null);
  const [open, setOpen] = useState(false);
  const kinds = eventKinds(draft);
  const count = draft.event_kinds.length;
  useEffect(() => {
    if (!open) return;
    const dismiss = (event) => { if (!wrapper.current?.contains(event.target)) setOpen(false); };
    const keys = (event) => {
      if (event.key === "Escape") { setOpen(false); menuButton.current?.focus(); }
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", keys);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", keys); };
  }, [open]);
  return <div ref={wrapper} className="recConditionMenu">
    <button type="button" ref={menuButton} className="recCellChoice recConditionButton" aria-expanded={open}
      aria-controls={"event-types-" + row.camera_id} aria-label={r("event_types") + " — " + row.name} disabled={disabled}
      onClick={() => setOpen(!open)}>{count === kinds.length ? r("event_all") : r("event_count", {n:count})}<span aria-hidden="true" /></button>
    {open && !disabled ? <div id={"event-types-" + row.camera_id} className="recConditionPanel" role="group" aria-label={r("event_types")}>
      <label><input type="checkbox" checked={count === kinds.length}
        onChange={e => change({...draft, event_kinds:e.target.checked ? [...kinds] : []})}/>{r("event_all")}</label>
      {kinds.map(kind => <label key={kind}><input type="checkbox" checked={draft.event_kinds.includes(kind)}
        onChange={e => change({...draft,event_kinds:e.target.checked ? [...draft.event_kinds,kind] : draft.event_kinds.filter(x=>x!==kind)})}/>
        {t("cameraEvents.kinds." + kind)}</label>)}
    </div> : null}
  </div>;
}

const durations = [1,2,3,4,5];
function DurationSelect({ id, label, value, change, disabled, hint }) {
  const wrapper=useRef(null), trigger=useRef(null), options=useRef([]);
  const [open,setOpen]=useState(false);
  const close=(restoreFocus=false)=>{setOpen(false);if(restoreFocus)trigger.current?.focus()};
  useEffect(()=>{
    if(!open||disabled)return;
    options.current[value-1]?.focus();
    const outside=e=>{if(!wrapper.current?.contains(e.target))setOpen(false)};
    document.addEventListener("pointerdown",outside);
    return ()=>document.removeEventListener("pointerdown",outside);
  },[open,disabled,value]);
  return <div className="recDuration" ref={wrapper} onBlur={e=>{
    if(e.relatedTarget && !e.currentTarget.contains(e.relatedTarget))close();
  }}>
    <button type="button" className="recCellChoice recDurationButton" ref={trigger} aria-label={`${label}: ${value}`} title={hint}
      aria-haspopup="listbox" aria-controls={id} aria-expanded={open&&!disabled} disabled={disabled}
      onClick={()=>setOpen(!open)} onKeyDown={e=>{
        if(["ArrowDown","ArrowUp"].includes(e.key)){e.preventDefault();setOpen(true);}
      }}><span>{value}</span></button>
    {open&&!disabled ? <div className="recDurationOptions" id={id} role="listbox" aria-label={label}>
      {durations.map((n,i)=><button key={n} type="button" role="option" aria-selected={value===n}
        ref={element=>{options.current[i]=element;}} tabIndex={-1}
        onClick={()=>{change(n);close(true)}} onKeyDown={e=>{
          const next={ArrowDown:Math.min(4,i+1),ArrowUp:Math.max(0,i-1),Home:0,End:4}[e.key];
          if(next!==undefined){e.preventDefault();options.current[next]?.focus();}
          if(e.key==="Escape"){e.preventDefault();close(true);}
        }}>{n}</button>)}
    </div>:null}
  </div>;
}

export function RecordingPolicyRow({ row, rows, entry, problem, busy, saving, change, reset, save, openRules, r, t }) {
  const id = row.camera_id;
  const draft = entry?.draft || draftForCamera(row);
  const controls = inlinePolicyControls(rows, row, draft);
  const { linked, events, candidates, constantSource, profiled, preRoll, sources, aiEditor, linkReady, ownEventSource, cameraConditions, alertProfile } = controls;
  const invalid = entry ? inlineSaveProblem(rows, row, entry) : "";
  const feedback = problem || invalid;
  const state = statusView(row);
  const runtime = row.runtime || {};
  const profile = draft.profile;
  const label = (key) => r(key) + " — " + row.name;
  const eventDisabled = !canUseCameraEvents(row) && !canUseAI(row) && !events;
  const srcHint = linked ? r(!linkReady ? "row_pick_camera" : constantSource ? "source_constant" : "inherit_duration")
    : draft.source === "camera" && !canUseCameraEvents(row) ? r(row.protocol === "onvif" ? "event_requires" : "event_unsupported") : undefined;
  const showState = r(state.key);
  const preRollValue = draft.prebuffer_seconds;
  const actualPreRoll = preRoll && row.policy.profile === profile && runtime.prebuffer_status === "ready"
    ? r("prehistory_ready", {n: runtime.prebuffer_seconds_available ?? 0}) : undefined;
  return <article id={`camera-${id}`} className="recordingCenterRow cameraHealthTarget" data-camera={id} data-pending={Boolean(entry)}>
    <strong className="recCell-camera" title={row.name}>{row.name}</strong>
    {cell("mode", r("scenario"), <select aria-label={label("scenario")} className="recCellChoice" value={draft.mode}
      disabled={busy} onChange={e => change(updateMode(draft,e.target.value,row))}>
      {["continuous","events","linked"].map(mode => <option key={mode} value={mode} disabled={mode==="events" && eventDisabled}>{r("row_mode_"+mode)}</option>)}
    </select>)}
    {cell("source", r("row_source"), linked ? <select className="recCellChoice" aria-label={label("row_source")}
      value={draft.linked_camera_id ?? ""} title={srcHint} disabled={busy}
      onChange={e => change(updateLinkedSource(rows,row,draft,Number(e.target.value)||null))}>
      <option value="">{r("row_pick_camera")}</option>
      {draft.linked_camera_id && !candidates.some(x=>x.camera_id===draft.linked_camera_id) ?
        <option disabled value={draft.linked_camera_id}>{r("source_missing")}</option> : null}
      {candidates.map(candidate=><option key={candidate.camera_id} value={candidate.camera_id}>{candidate.name}</option>)}
    </select> : sources.length ? <select className="recCellChoice" aria-label={label("row_source")} value={draft.source}
      title={srcHint} disabled={busy} onChange={e => change(updateSource(draft,e.target.value))}>
      {!sources.includes(draft.source) ? <option disabled value={draft.source}>{r("source_choose")}</option> : null}
      {sources.map(source => <option key={source} value={source} disabled={source==="camera" ? !canUseCameraEvents(row) : source==="ai" && !canUseAI(row)}>{r("row_source_"+source)}</option>)}
    </select> : <Blank hint={srcHint} />)}
    {cell("profile", r("profile"), profiled ? <select aria-label={label("profile")} className="recCellChoice" value={profile}
      disabled={busy} title={r("hint_"+profile)} onChange={e => change(updateProfile(draft,e.target.value))}>
      {profilesFor(draft).map(p => <option key={p} value={p}>{r("row_profile_"+p)}</option>)}
    </select> : alertProfile ? <span className="recLinkNote" title={r("row_alert_gap_help")}>{r("row_profile_alerts")}</span>
      : constantSource ? <span className="recLinkNote" title={r("source_constant")}>{r("row_profile_follows")}</span>
      : <Blank hint={linked&&!linkReady ? srcHint : undefined} />)}
    {cell("before", r("row_before"), preRoll ? <input type="number" aria-label={label("row_before")} className="recCellChoice recCellNumber"
      title={actualPreRoll} min="0" max="120" inputMode="numeric" disabled={busy} value={preRollValue ?? ""}
      onChange={e => change({...draft,prebuffer_seconds:e.target.value === "" ? null : Number(e.target.value)})} /> : <Blank />)}
    {cell("after", r("row_after"), ownEventSource || alertProfile ? <DurationSelect id={`rec-duration-${id}`}
      label={label(alertProfile ? "row_alert_gap" : "row_after")} hint={alertProfile ? r("row_alert_gap_help") : undefined}
      disabled={busy} value={alertProfile ? draft.alert_quiet_minutes : draft.postrecord_minutes}
      change={n=>change({...draft,[alertProfile ? "alert_quiet_minutes" : "postrecord_minutes"]:n})} />
      : <Blank hint={linkReady ? r("inherit_duration") : undefined} />)}
    {cell("conditions", r("row_conditions"), cameraConditions ? <ConditionMenu row={row} draft={draft} disabled={busy}
      change={change} t={t} r={r} /> : aiEditor ? <span className="recAiSummary" title={r("rule_edit")}>
        {draft.ai_rules ? r("rule_count", {n:aiRuleCount(draft.ai_rules)}) : r("row_full_frame")}
      </span> : <Blank />)}
    <div className={"recCell-state recordingCenterState recordingCenterState-" + state.tone} title={showState}>
      <i aria-hidden="true" /><span>{showState}</span>
    </div>
    <div className="recRowActions">
      <div className="recActionSlots">
        <button className="recIconButton recRowReset" type="button" disabled={!entry || busy}
          aria-label={label("row_reset")} title={r("row_reset")} onClick={reset}><RecordingUiIcon name="cancel" /></button>
        <button className="recIconButton recRowCommit" type="button" disabled={!entry || busy || Boolean(invalid)}
          aria-label={label("row_save")} title={feedback ? r(feedback) : r("row_save")} aria-busy={saving} onClick={save}><CheckIcon /></button>
        <button className="recIconButton" type="button" disabled={!aiEditor || busy || invalid==="row_changed"}
          aria-label={label("rule_edit")} title={r("rule_edit")} onClick={openRules}><RecordingUiIcon name="gear" /></button>
      </div>
      <span className="recRowFeedback" role={feedback ? "status" : undefined} title={feedback ? r(feedback) : undefined}
        data-error={Boolean(feedback)}>{saving ? r("row_saving") : feedback ? r("row_review") : entry ? r("row_pending") : ""}</span>
      {feedback ? <span className="recSrOnly">{r(feedback)}</span> : null}
    </div>
  </article>;
}
