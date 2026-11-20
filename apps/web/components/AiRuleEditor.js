"use client";

import { useEffect, useId, useRef, useState } from "react";
import { apiFetch, captureAuthSession } from "../lib/api";
import { DAYS, EMPTY_RULES, RULE_CLASSES, RULE_KINDS, aiRuleCount, defaultRule, timeLabel, timeMinute } from "../lib/aiRules";
import { eventKinds } from "../lib/recordingCenter";
import AiCanvasStage from "./AiCanvasStage";
import { TrashIcon } from "./CompactActionIcons";
import { RecordingUiIcon } from "./RecordingUiIcons";
import { useModalBodyScrollLock } from "./OperationFeedback";

const newId = () => "rule-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2,9);
const keyScheme = (kind) => "rule_kind_" + kind;
const contourColors = ["#60dfef","#96dfa4","#bfb6fa","#82bafa","#ffd181","#ef9bb5","#b5dddd","#72c5b7"];
const validFrame = (data) => data && data.width>0 && data.width<=1920 && data.height>0 && data.height<=1080
  && typeof data.image==="string" && data.image.length>0 && data.image.length<=1398104 && typeof data.reference==="string";

function ShapeList({ items, active, choose, overview, clear, remove, hasFrame, r }) {
  return <div className="aiShapeList" aria-label={r("work_shapes")}>
    <button type="button" className="aiShapeChoice" aria-pressed={!active} onClick={overview}
      data-shape-choice="overview"><RecordingUiIcon name="frame"/><span>{r("work_overview")}</span></button>
    {items.map(({id,name,hint,color,mark,points,mask})=>{
      const clearLabel=r("rule_clear"), removeLabel=r(mask?"rule_remove_mask":"rule_remove");
      return <div key={id} className="aiShapeRow" data-active={active===id}>
        <button type="button" className="aiShapeChoice" style={{"--shape-ink":color}} title={name}
          aria-pressed={active===id} data-shape-choice={id} onClick={()=>choose(id)}>
          <b>{mark}</b><span>{name}{hint ? <small>{hint}</small>:null}</span>
        </button>
        <div className="aiShapeActions">
          <button type="button" title={clearLabel} aria-label={`${clearLabel}: ${name}`}
            disabled={!hasFrame||!points.length} onClick={()=>clear(id)}><RecordingUiIcon name="eraser"/></button>
          <button type="button" title={removeLabel} aria-label={`${removeLabel}: ${name}`}
            className="aiShapeDelete" onClick={()=>remove(id)}><TrashIcon/></button>
        </div>
      </div>;
    })}
  </div>;
}

function Tool({icon,children,pressed,disabled,onClick,compact=false}) {
  return <button type="button" className={compact?"aiWorkTool is-icon":"aiWorkTool"} title={children} aria-label={children}
    aria-pressed={pressed} disabled={disabled} onClick={onClick}>
    <RecordingUiIcon name={icon} />{compact?null:<span>{children}</span>}
  </button>;
}

function Properties({ rule, patch, t, r, locale }) {
  const typeId=useId();
  const helpId=useId();
  const usesObjects=rule.kind!=="scene"||Boolean(rule.scene_objects);
  const hasDirection=rule.kind==="crossing";
  const hasDwell=rule.kind==="dwell";
  const zoneTransition=rule.kind==="enter"||rule.kind==="exit";
  const number = (name, label, min, max, help=label+"_help", disabled=false) => <div className="aiTuningField">
    <label htmlFor={`${rule.id}-${name}`}>{r(label)}</label>
    <div className="aiTuningValue"><input id={`${rule.id}-${name}`} className="recCellChoice recCellNumber" type="number" min={min} max={max}
      aria-describedby={`${rule.id}-${name}-help`} inputMode="numeric" value={rule[name] ?? ""} disabled={disabled}
      onChange={e=>patch({[name]:e.target.value===""?null:Number(e.target.value)})}/></div>
    <small id={`${rule.id}-${name}-help`}>{r(help)}</small>
  </div>;
  return <>
    <div className="aiHintGroup">
      <label className="aiHintLabel" htmlFor={typeId}>{r("rule_type")}</label>
      <select id={typeId} className="recCellChoice" aria-describedby={helpId} value={rule.kind} onChange={e=>{
        const kind=e.target.value;
        const polygon=![kind,rule.kind].some(k=>k==="crossing"||k==="scene");
        patch({kind,points:polygon?rule.points:[],direction:kind===rule.kind?rule.direction:"both",
          scene_objects:kind==="scene"&&Boolean(rule.scene_objects)});
      }}>{RULE_KINDS.map(kind=><option key={kind} value={kind}>{r(keyScheme(kind))}</option>)}</select>
      <p className="aiHintBox aiConditionHint" id={helpId}><span>{r("rule_hint_"+rule.kind)}</span></p>
    </div>
    <div className="aiRuleFields">
      <label className="aiRuleName">{r("rule_name")}<input className="recCellChoice" value={rule.name} maxLength={48}
        onChange={e=>patch({name:e.target.value})}/></label>
    </div>
    {rule.kind==="scene" ? <label className="aiRuleCheck">
      <input type="checkbox" checked={Boolean(rule.scene_objects)} onChange={e=>patch({scene_objects:e.target.checked})}/>
      {r("rule_scene_objects")}
    </label> : null}
    <div className="aiObjectChips" data-applicable={usesObjects} aria-hidden={!usesObjects || undefined}>
      {RULE_CLASSES.map(kind=><label key={kind}>
        <input type="checkbox" checked={rule.classes.includes(kind)} disabled={!usesObjects} onChange={e=>patch({classes:e.target.checked
          ?[...rule.classes,kind]:rule.classes.filter(x=>x!==kind)})}/>
        <span>{t("cameraEvents.kinds."+kind)}</span>
      </label>)}
    </div>
    <div className="aiConditionExtra">
      <label className="aiPropertyField" data-applicable={hasDirection} aria-hidden={!hasDirection || undefined}>{r("rule_direction")}
        <select className="recCellChoice" aria-describedby={helpId} value={rule.direction} disabled={!hasDirection}
          onChange={e=>patch({direction:e.target.value})}>
          {["both","forward","reverse"].map(value=><option key={value} value={value}>{r("rule_direction_"+value)}</option>)}
        </select>
      </label>
      <div data-applicable={hasDwell} aria-hidden={!hasDwell || undefined}>
        {number("dwell_seconds","rule_dwell",3,300,"rule_dwell_help",!hasDwell)}
      </div>
    </div>
    <details className="aiRuleFold" data-applicable={usesObjects} aria-hidden={!usesObjects || undefined}>
      <summary tabIndex={usesObjects ? undefined : -1}>{r("rule_tuning")}</summary>
      {usesObjects ? <div className="aiTuningFields">{number("confidence","rule_confidence",35,95)}
          {number("confirm_seconds","rule_confirm",0,20,zoneTransition?"rule_confirm_zone_help":"rule_confirm_help")}
          {!hasDirection ? number("release_seconds",zoneTransition?"rule_outside":"rule_release",0,15,
            zoneTransition?"rule_outside_"+rule.kind+"_help":"rule_release_help") : null}</div> : null}
    </details>
    <details className="aiRuleFold"><summary>{r("rule_schedule")}</summary>
      <label className="aiRuleCheck"><input type="checkbox" checked={Boolean(rule.schedule)}
        onChange={e=>patch({schedule:e.target.checked?{days:[...DAYS],start:0,end:0}:null})}/>{r("rule_custom_time")}</label>
      {rule.schedule ? <>
        <div className="aiRuleDays">{DAYS.map(day=>{
          const weekday=new Intl.DateTimeFormat(locale,{weekday:"short",timeZone:"UTC"}).format(new Date(Date.UTC(2024,0,day)));
          return <button key={day} type="button" className="aiWorkTool" aria-pressed={rule.schedule.days.includes(day)}
            onClick={()=>patch({schedule:{...rule.schedule,days:rule.schedule.days.includes(day)
              ?rule.schedule.days.filter(value=>value!==day):[...rule.schedule.days,day]}})}>{weekday}</button>;
        })}</div>
        <div className="aiRuleFields aiTimeFields">{["start","end"].map(key=><label key={key}>{r("rule_"+key)}
          <input className="recCellChoice" type="time" value={timeLabel(rule.schedule[key])}
            onChange={e=>e.target.value&&patch({schedule:{...rule.schedule,[key]:timeMinute(e.target.value)}})}/>
        </label>)}</div>
        <small>{r("rule_local_time")}</small>
      </> : null}
    </details>
  </>;
}

export default function AiRuleEditor({ camera,draft,update,issue,dirty,saving,onBack,onSave,r,t,locale }) {
  const config=draft.ai_rules || EMPTY_RULES;
  const [selected,select]=useState(null);
  const [pane,setPane]=useState("rules");
  const [mask,selectMask]=useState(null);
  const [tool,setTool]=useState("points");
  const [picked,pick]=useState(null);
  const [frame,setFrame]=useState(null);
  const [expanded,setExpanded]=useState(false);
  const [loading,setLoading]=useState(false);
  const [restoring,setRestoring]=useState(Boolean(camera.policy?.ai_rules?.reference));
  const [captureError,setCaptureError]=useState("");
  const [history,setHistory]=useState({back:[],forward:[]});
  const request=useRef(null);
  const initialDraftReference=useRef(config.reference);
  const workspace=useRef(null);
  const expandButton=useRef(null);
  const native=useRef(false);
  const rule=config.rules.find(item=>item.id===selected);
  const active=pane==="masks" ? mask!==null && config.exclusions[mask] && "mask:"+mask : rule && "rule:"+rule.id;
  const points=pane==="masks" ? config.exclusions[mask]||[] : rule?.points||[];
  const ruleShapes=config.rules.map((item,i)=>({id:"rule:"+item.id,points:item.points,line:item.kind==="crossing",
    direction:item.direction,color:contourColors[i],mark:i+1,name:item.name||r(keyScheme(item.kind)),
    hint:item.name?r(keyScheme(item.kind)):null}));
  const maskShapes=config.exclusions.map((points,i)=>({id:"mask:"+i,points,mask:true,color:"#edab76",mark:"×"+(i+1),
    name:r("rule_mask_n",{n:i+1})}));
  const shapes=[...ruleShapes,...maskShapes];
  const canEdit=Boolean(frame&&active&&(pane==="masks"||rule?.kind!=="scene")&&!saving&&!loading);
  const maximum=pane==="rules"&&rule?.kind==="crossing"?2:10;

  useModalBodyScrollLock(expanded);
  useEffect(()=>()=>{
    request.current?.abort();
  },[]);
  const cameraId=camera.camera_id;
  const committedReference=camera.policy?.ai_rules?.reference;
  useEffect(()=>{
    if(!committedReference || initialDraftReference.current && initialDraftReference.current!==committedReference){
      setRestoring(false);return;
    }
    const own=captureAuthSession();
    const controller=new AbortController();
    request.current=controller;
    const current=()=>own()&&!controller.signal.aborted;
    apiFetch(`/ai-recording/cameras/${cameraId}/rule-frame/saved`,{signal:controller.signal}).then(data=>{
      if(!current())return;
      if(!validFrame(data)||data.reference!==committedReference)throw new Error();
      setFrame(data);
    }).catch(error=>{
      if(current()&&error?.status!==404)setCaptureError("rule_saved_frame_error");
    }).finally(()=>{if(current())setRestoring(false)});
    return ()=>controller.abort();
  },[cameraId,committedReference]);
  useEffect(()=>{
    const shell=workspace.current;
    const resize=()=>{
      if(document.fullscreenElement===shell) native.current=true;
      else if(native.current){
        native.current=false;
        setExpanded(false);
        requestAnimationFrame(()=>expandButton.current?.focus());
      }
    };
    document.addEventListener("fullscreenchange",resize);
    return ()=>document.removeEventListener("fullscreenchange",resize);
  },[]);

  function apply(next) {
    setHistory(old=>({back:[...old.back,draft].slice(-32),forward:[]}));
    update(next);
  }
  function applyConfig(value) {apply({...draft,ai_rules:value});}
  function undo() {
    if(!history.back.length||saving||loading) return;
    update(history.back.at(-1));
    setHistory({back:history.back.slice(0,-1),forward:[draft,...history.forward]});
    pick(null);
  }
  function redo() {
    if(!history.forward.length||saving||loading) return;
    update(history.forward[0]);
    setHistory({back:[...history.back,draft],forward:history.forward.slice(1)});
    pick(null);
  }

  function patchRule(fields) {
    if(!rule) return;
    if("kind" in fields && fields.kind!==rule.kind) {
      pick(null);
      setTool(fields.points.length?"points":"add");
    }
    applyConfig({...config,rules:config.rules.map(item=>item.id===rule.id?{...item,...fields}:item)});
  }
  function addRule() {
    if(config.rules.length>=8) return;
    const added=defaultRule(newId());
    applyConfig({...config,reference:frame?.matching_saved!==false?frame?.reference||config.reference:config.reference,
      rules:[...config.rules,added]});
    select(added.id);setTool("add");pick(null);
  }
  function addMask() {
    if(!frame||!config.rules.length||config.exclusions.length>=4)return;
    applyConfig({...config,reference:frame.reference,exclusions:[...config.exclusions,[]]});
    selectMask(config.exclusions.length);setTool("add");pick(null);
  }
  function clearShape(id) {
    const shape=shapes.find(item=>item.id===id);
    if(!frame||!shape?.points.length)return;
    applyConfig(shape.mask ? {...config,exclusions:config.exclusions.map((item,i)=>i===Number(id.slice(5))?[]:item)}
      : {...config,rules:config.rules.map(item=>"rule:"+item.id===id?{...item,points:[]}:item)});
    if(id===active){setTool("add");pick(null);}
  }
  function removeShape(id) {
    const shape=shapes.find(item=>item.id===id);
    if(!shape)return;
    if(shape.mask){
      const index=Number(id.slice(5));
      applyConfig({...config,exclusions:config.exclusions.filter((_,i)=>i!==index)});
      if(mask===index)selectMask(null);
      else if(mask!==null&&mask>index)selectMask(mask-1);
    } else {
      const items=config.rules.filter(item=>"rule:"+item.id!==id);
      applyConfig(items.length?{...config,rules:items}:null);
      if(id===active)select(null);
      if(!items.length)selectMask(null);
    }
    if(id===active){setTool("points");pick(null);}
  }
  function changePoints(next) {
    if(!canEdit) return;
    const nextConfig={...config,reference:frame.reference};
    if(pane==="masks") nextConfig.exclusions=config.exclusions.map((item,i)=>i===mask?next:item);
    else nextConfig.rules=config.rules.map(item=>item.id===rule.id?{...item,points:next}:item);
    applyConfig(nextConfig);
    if(next.length===maximum&&tool==="add")setTool("points");
  }

  async function capture() {
    if(loading||saving) return;
    const own=captureAuthSession();
    const controller=new AbortController();
    request.current?.abort();
    request.current=controller;
    setRestoring(false);setLoading(true);setCaptureError("");
    try {
      const profile=draft.mode==="continuous"?"none":draft.profile;
      const data=await apiFetch(`/ai-recording/cameras/${camera.camera_id}/rule-frame?profile=${profile}`,
        {method:"POST",signal:controller.signal});
      if(!own()||controller.signal.aborted)return;
      if(!validFrame(data))throw new Error();
      setFrame(data);
      if(config.rules.length && data.matching_saved!==false) applyConfig({...config,reference:data.reference});
    } catch(error) {
      if(own()&&!controller.signal.aborted)setCaptureError(error?.code==="ai_frame_busy"?"rule_frame_busy"
        :error?.code==="ai_frame_storage_unavailable"?"rule_frame_storage_error":"rule_frame_error");
    } finally {
      if(own()&&!controller.signal.aborted)setLoading(false);
    }
  }

  function expand() {
    if(!frame)return;
    setExpanded(true);
    try {const result=workspace.current?.requestFullscreen?.(); result?.catch(()=>{});} catch (_) {}
  }
  function shrink() {
    setExpanded(false);
    if(document.fullscreenElement===workspace.current)void document.exitFullscreen().catch(()=>{});
    requestAnimationFrame(()=>expandButton.current?.focus());
  }
  function keys(event) {
    if(event.key==="Escape"&&expanded){
      if(document.fullscreenElement!==workspace.current){
        event.preventDefault();shrink();
      }
      return;
    }
    if(event.target?.matches?.("input,select,textarea,[contenteditable]"))return;
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="z") {
      event.preventDefault();
      if(event.shiftKey)redo();else undo();
    } else if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==="y") {
      event.preventDefault();redo();
    }
  }

  const note=captureError||frame?.matching_saved===false&&"rule_frame_replaced";
  const toolNote=canEdit ? tool==="move"?"work_move_hint":tool==="add"&&maximum===2?"rule_line_hint":
    tool==="add"?"rule_draw_help":"work_points_hint" : "";
  const openPane=(name)=>{setPane(name);setTool("points");pick(null);};
  function overview() {select(null);selectMask(null);setTool("points");pick(null);}
  function choose(id) {
    const shape=shapes.find(item=>item.id===id);
    if(!shape)return;
    if(shape.mask){setPane("masks");selectMask(Number(id.slice(5)));}
    else {setPane("rules");select(id.slice(5));}
    setTool(shape.points.length?"points":"add");
    pick(null);
  }
  const controls=eventKinds(draft);
  const canSave=dirty && !issue && !saving && !loading;
  const fullLabel=r(expanded?"work_exit_full":"work_full");
  const frameStatus=frame ? r(frame.role==="live"?"rule_frame_live":"rule_frame_record",{
    track:frame.track==="sub"?"Sub":"Main",width:frame.width,height:frame.height,
  }) : r(restoring?"rule_saved_frame_loading":"rule_picture");
  return <section className={"aiWorkspace"+(expanded?" is-expanded":"")} ref={workspace} onKeyDown={keys}
    aria-label={r("rule_edit")} data-expanded={expanded}>
    <header className="aiWorkHeader">
      {!expanded ? <button type="button" className="aiWorkBack" onClick={onBack} disabled={saving}
        aria-label={r("work_back")} title={r("work_back")}><RecordingUiIcon name="back"/></button>:null}
      <div className="aiWorkTitle"><small>{r("work_heading")}{aiRuleCount(config)?" · "+r("rule_total",{n:aiRuleCount(config)}):""}</small>
        <h1 title={camera.name}>{camera.name}</h1></div>
      <div className="aiWorkActions">
        <button type="button" className="aiWorkSave" disabled={!canSave}
          onClick={onSave}>{saving?r("row_saving"):t("common.save")}</button>
      </div>
    </header>

    <div className="aiWorkSurface">
      <div className="aiStageColumn">
        <div className="aiFrameBar">
          <span className="aiFrameSource" title={frameStatus}>{frameStatus}</span>
          <div className="aiFrameActions">
            <Tool icon="frame" disabled={loading||saving} onClick={capture}>
              {r(loading?"rule_frame_loading":frame?"rule_frame_refresh":captureError?"work_retry_frame":"rule_capture")}</Tool>
            <button type="button" ref={expandButton} className="aiWorkTool" disabled={!frame} onClick={expanded?shrink:expand}
              aria-label={fullLabel} title={fullLabel}><RecordingUiIcon name={expanded?"collapse":"expand"}/><span>{fullLabel}</span></button>
          </div>
        </div>
        <AiCanvasStage frame={frame} shapes={shapes} active={active} choose={saving?null:choose} tool={tool} canEdit={canEdit} pointLimit={maximum}
          picked={picked} pick={pick} change={changePoints} toggleFull={expanded?shrink:expand} r={r}
          centre={<><RecordingUiIcon name="frame"/><strong>{r(loading?"rule_frame_loading":restoring?"rule_saved_frame_loading":"rule_picture")}</strong>
            <span>{r(captureError||(loading||restoring?"rule_frame_wait":config.reference?"rule_frame_no_saved":"rule_frame_hint"))}</span></>}/>
        <div className="aiStageStrip">
          <div className="aiToolRow">
            <div className="aiToolGroup">
              <Tool icon="point" pressed={tool==="add"} disabled={!canEdit||points.length>=maximum}
                onClick={()=>{setTool(tool==="add"?"points":"add");pick(null);}}>{r("work_add_points")}</Tool>
              <Tool icon="move" pressed={tool==="move"} disabled={!canEdit||points.length<2}
                onClick={()=>{setTool(tool==="move"?"points":"move");pick(null);}}>{r("work_move_contour")}</Tool>
            </div>
            <div className="aiToolGroup">
              <Tool compact icon="undo" disabled={!history.back.length||saving||loading} onClick={undo}>{r("work_undo")}</Tool>
              <Tool compact icon="redo" disabled={!history.forward.length||saving||loading} onClick={redo}>{r("work_redo")}</Tool>
              <Tool compact icon="cut" disabled={!canEdit||!points.length} onClick={()=>{
                const index=picked??points.length-1;
                changePoints(points.filter((_,i)=>i!==index));pick(null);
              }}>{r("rule_undo")}</Tool>
            </div>
          </div>
          <div className="aiToolNote" role={note?"status":undefined} data-warning={Boolean(note)}>
            <span title={note?r(note):toolNote?r(toolNote):undefined}>{note?r(note):toolNote?r(toolNote):shapes.length?r("work_overview_hint"):"\u00a0"}</span>
            {canEdit ? <span>{r("work_points_count",{n:points.length,max:maximum})}</span>:null}
          </div>
        </div>
      </div>

      <aside className="aiRuleSidebar" aria-label={r("work_heading")}>
        <div className="aiSideTabs" role="group" aria-label={r("work_heading")}>
          <button type="button" aria-pressed={pane==="rules"} onClick={()=>openPane("rules")}>{r("rule_items")}
            {config.rules.length?<span>{config.rules.length}</span>:null}</button>
          <button type="button" aria-pressed={pane==="masks"} onClick={()=>openPane("masks")}>{r("rule_exclusions")}
            {config.exclusions.length?<span>{config.exclusions.length}</span>:null}</button>
        </div>
        <fieldset className="aiRuleBody" disabled={saving||loading}>
          {pane==="rules" ? <>
            <div className="aiSideSection">
              <small>{r("work_list_limit",{n:config.rules.length,max:8})}</small>
              <button type="button" className="aiWorkTool" disabled={config.rules.length>=8}
                onClick={addRule}><span aria-hidden="true">+</span>{r("rule_add")}</button>
            </div>
            <ShapeList items={ruleShapes} active={active} choose={choose} overview={overview}
              clear={clearShape} remove={removeShape} hasFrame={Boolean(frame)} r={r}/>
            {rule ? <Properties rule={rule} patch={patchRule} r={r} t={t} locale={locale}/>
              :config.rules.length?<small>{r("work_overview_hint")}</small>:<>
              <small>{r("rule_default")}</small>
              <div className="aiBasicKinds">
                {controls.map(kind=><label key={kind} className="aiRuleCheck"><input type="checkbox" checked={draft.event_kinds.includes(kind)}
                  onChange={e=>apply({...draft,event_kinds:e.target.checked
                    ?[...draft.event_kinds,kind]:draft.event_kinds.filter(item=>item!==kind)})}/>
                  {kind==="motion"?r("ai_scene"):t("cameraEvents.kinds."+kind)}</label>)}
              </div>
            </>}
          </>:<>
            <div className="aiSideSection">
              <small>{r("work_list_limit",{n:config.exclusions.length,max:4})}</small>
              <button type="button" className="aiWorkTool" disabled={!frame||!config.rules.length||config.exclusions.length>=4}
                onClick={addMask}><span aria-hidden="true">+</span>{r("rule_add_mask")}</button>
            </div>
            <small>{r(config.rules.length?"rule_exclusion_note":"work_mask_first")}</small>
            <ShapeList items={maskShapes} active={active} choose={choose} overview={overview}
              clear={clearShape} remove={removeShape} hasFrame={Boolean(frame)} r={r}/>
          </>}
        </fieldset>
        <div className="aiSideStatus" role="status" data-warning={Boolean(issue)}>{issue?r(issue):dirty?r("row_pending"):"\u00a0"}</div>
      </aside>
    </div>
  </section>;
}
