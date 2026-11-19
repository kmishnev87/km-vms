import test from "node:test";
import assert from "node:assert/strict";
import { contourAt, frameFit, framePoint, movePoint, pickedVertex, shiftContour } from "../lib/aiCanvasGeometry.js";
import { aiRuleCount, defaultRule } from "../lib/aiRules.js";
import { AI_RULES_COPY } from "../lib/i18n/aiRulesCopy.js";
import {
  draftForCamera, dropRecordingDraft, hasPolicyChanges, inlinePolicyControls, inlineSaveProblem,
  recordingPolicyUpdate, stageRecordingDraft, updateLinkedSource, updateMode, updateProfile, updateSource,
} from "../lib/recordingCenter.js";

const row = (id, policy={}, meta={}) => ({
  camera_id:id,name:"Camera "+id,protocol:"onvif",events_enabled:true,
  policy:{revision:7,mode:"continuous",source:"none",...policy},
  runtime:{ai_available:true,legacy_recording_mode:"always"},...meta,
});

test("inline source and profile only expose valid options and open AI by its actual selection",()=>{
  const one=row(1), rtsp=row(2,{}, {protocol:"rtsp",events_enabled:false});
  const rows=[one,rtsp];
  const current=draftForCamera(one);
  assert.deepEqual(inlinePolicyControls(rows,one,current).sources,["none","camera","ai"]);
  assert.equal(inlinePolicyControls(rows,one,current).aiEditor,false);
  const event=updateMode(draftForCamera(rtsp),"events",rtsp);
  assert.equal(event.source,"ai");
  assert.deepEqual(inlinePolicyControls(rows,rtsp,event).sources,["ai"]);
  assert.equal(event.profile,"balanced");
  assert.equal(inlinePolicyControls(rows,rtsp,event).preRoll,true);
  assert.equal(inlinePolicyControls(rows,rtsp,event).aiEditor,true);
  const economy=updateProfile(updateMode(current,"events",one),"economical");
  assert.equal(inlinePolicyControls(rows,one,economy).preRoll,false);
  const link={...updateMode(economy,"linked",one),linked_camera_id:2};
  assert.deepEqual(inlinePolicyControls(rows,one,link).candidates.map(source=>source.camera_id),[2]);
  assert.equal(inlinePolicyControls(rows,one,link).profiled,false);
  assert.equal(inlinePolicyControls(rows,one,link).aiEditor,false);
  assert.equal(updateMode(event,"continuous",rtsp).source,"ai");
});

test("linked profile waits for a valid source, then uses that source's configured mode",()=>{
  const one=row(1), two=row(2), three=row(3,{mode:"events",source:"camera"});
  const rows=[one,two,three];
  const link=updateProfile(updateMode(draftForCamera(one),"linked",one),"full_quality");
  assert.equal(inlinePolicyControls(rows,one,link).profiled,false);
  assert.equal(inlinePolicyControls(rows,one,link).preRoll,false);
  assert.equal(inlinePolicyControls(rows,one,{...link,linked_camera_id:4}).profiled,false);
  const continuous=updateLinkedSource(rows,one,{...link,prebuffer_seconds:null},2);
  assert.equal(continuous.prebuffer_seconds,0);
  const control=inlinePolicyControls(rows,one,continuous);
  assert.equal(control.linkReady,true);
  assert.equal(control.constantSource,true);
  assert.equal(control.profiled,false);
  const event=inlinePolicyControls(rows,one,updateLinkedSource(rows,one,link,3));
  assert.equal(event.profiled,true);
  assert.equal(event.preRoll,true);
});

test("exclusions count alongside trigger rules and each condition has contextual help",()=>{
  const exclusions=[[[200,200],[700,200],[500,700]]];
  assert.equal(aiRuleCount({rules:[defaultRule("one"),defaultRule("two")],exclusions}),3);
  assert.equal(aiRuleCount(null),0);
  for (const copy of Object.values(AI_RULES_COPY)) for (const kind of ["presence","enter","exit","crossing","dwell","scene"])
    assert.ok(copy[`rule_hint_${kind}`]?.trim().length > 10);
});

test("draft revisions survive polling, an external edit disables saving, and resetting restores only its row",()=>{
  const one=row(1), two=row(2);
  const draft=updateSource(draftForCamera(one),"ai");
  let entries=stageRecordingDraft({},one,draft);
  entries=stageRecordingDraft(entries,two,updateMode(draftForCamera(two),"events",two));
  assert.equal(entries[1].revision,7);
  assert.equal(inlineSaveProblem([one,two],one,entries[1]),"");
  assert.equal(recordingPolicyUpdate(one,entries[1].draft).expected_revision,7);
  const changed=row(1,{revision:8,source:"camera"});
  entries=stageRecordingDraft(entries,changed,updateSource(draft,"none"));
  assert.equal(entries[1].revision,7);
  assert.equal(inlineSaveProblem([changed,two],changed,entries[1]),"row_changed");
  assert.deepEqual(Object.keys(dropRecordingDraft(entries,1)),["2"]);
  assert.equal(hasPolicyChanges(one,{...draftForCamera(one),ai_rules:null}),false);
});

test("image fit preserves different aspect ratios; pointer normalization is based on the frame, not the viewport",()=>{
  assert.deepEqual(frameFit(1200,600,640,360),{width:640*(600/360),height:600});
  assert.deepEqual(frameFit(360,440,800,400),{width:360,height:180});
  assert.deepEqual(frameFit(1000,700,450,800),{width:393.75,height:700});
  assert.equal(frameFit(0,200,800,400),null);
  const rect={left:40,top:125,width:360,height:180};
  assert.deepEqual(framePoint(rect,220,170),[500,250]);
  assert.deepEqual(framePoint(rect,-100,800),[0,1000]);
});

test("editing one vertex and translating a whole contour respect frame bounds",()=>{
  const polygon=[[200,300],[870,300],[870,970],[200,970]];
  assert.deepEqual(movePoint(polygon,1,[1060,90]),[[200,300],[1000,90],[870,970],[200,970]]);
  const result=shiftContour(polygon,260,220);
  assert.deepEqual(result,[[330,330],[1000,330],[1000,1000],[330,1000]]);
  assert.deepEqual(shiftContour(result,-900,-900),[[0,0],[670,0],[670,670],[0,670]]);
  const rect={width:1000,height:400};
  assert.equal(pickedVertex(polygon,[206,311],rect,10),0);
  assert.equal(pickedVertex(polygon,[226,311],rect,10),-1);
  assert.equal(contourAt(polygon,[500,600],rect,10,true),true);
  const line=polygon.slice(0,2);
  assert.equal(contourAt(line,[500,315],rect,10,false),true);
  assert.equal(contourAt(line,[960,300],rect,10,false),false);
});
