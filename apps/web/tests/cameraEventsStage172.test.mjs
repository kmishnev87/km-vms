import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { cameraEventId, eventErrorKey, eventHistoryQuery, minuteLabel, minuteValue } from "../lib/cameraEvents.js";
const read=path=>readFileSync(new URL(path,import.meta.url),"utf8");

test("event handoff only accepts a UUID, never an arbitrary URL or camera secret",()=>{
 const id="12345678-1234-1234-1234-123456789012";
 assert.equal(cameraEventId(`?camera_event=${id}`),id);
 for(const input of ["?camera_event=https://evil.invalid","?camera_event=1","?camera_id=1",""])assert.equal(cameraEventId(input),null);
});
test("filters preserve explicit unread and bounded pages",()=>{
 const params=new URLSearchParams(eventHistoryQuery({camera:"42",kind:"person",unread:true,days:7},25));
 assert.equal(params.get("camera_id"),"42");assert.equal(params.get("kind"),"person");assert.equal(params.get("unread"),"true");assert.equal(params.get("offset"),"25");
 assert.equal(new URLSearchParams(eventHistoryQuery({kind:"unknown"},-10)).get("kind"),null);
});
test("notification hours round-trip without changing the NAS time zone",()=>{
 for(const minute of [0,1,600,1439])assert.equal(minuteValue(minuteLabel(minute)),minute);
 assert.equal(eventErrorKey({code:"untrusted_raw_error"}),"request_failed");
 assert.equal(eventErrorKey({code:"event_archive_unavailable"}),"event_archive_unavailable");
});
test("hub uses one cancellable long-poll, body portals and fixed-height scrolling workbench",()=>{
 const hub=read("../components/CameraEventsHub.js"), css=read("../app/styles/35-camera-events.css");
 assert.match(hub,/wait: cached\.cursor \? "25" : "0"/);assert.match(hub,/controller\.abort\(\)/);assert.match(hub,/createPortal/);assert.match(hub,/document\.body/);
 assert.match(css,/\.cameraEventsList\s*\{[^}]*flex:\s*1;[^}]*min-height:\s*0;[^}]*overflow:\s*auto;/);
 assert.doesNotMatch(hub,/localStorage\.setItem|setInterval/);
 assert.match(hub,/setSelection\(previous => previous\.filter/);
 assert.match(hub,/time dateTime=\{eventToast\.occurred_at\}/);
});
test("temporary event navigation never replaces either saved workspace",()=>{
 for(const name of ["live","chronology"]){
  const source=read(`../app/${name}/page.js`);
  assert.match(source,/eventViewRef\.current = Boolean\(cameraEventId\(window\.location\.search\)\)/);
  assert.match(source,/if \(eventViewRef\.current\) return; \/\/ Temporary event view/);
  assert.match(source,/if \(eventViewRef\.current\) return;\s*const ownerSession/);
  assert.match(source,/useCameraEventHandoff/);
 }
});
test("timeline marker requests follow hour buckets rather than playback ticks",()=>{
 const source=read("../lib/useTimelineCameraEvents.js");
 assert.match(source,/Math\.floor\(centerMs \/ 3600000\)/);assert.match(source,/\[key, hour, hours, revision\]/);assert.doesNotMatch(source,/setInterval/);
});
test("standard pages share edges without stretching Settings forms or video workspaces",()=>{
 const system=read("../app/styles/30-system-status-apk.css"),settings=read("../app/styles/20-01-settings-forms-users.css");
 assert.match(system,/\.systemStatusPanel\s*\{\s*width:\s*100%;/);assert.doesNotMatch(system,/max-width:\s*1180px/);
 assert.match(system,/clamp\(318px, 26%, 360px\)/);assert.match(settings,/clip-path:\s*inset\(0 var\(--settings-background-inset\) round 14px\)/);
 assert.match(settings,/\.settingsWorkspace\s*\{[^}]*max-width:\s*920px;/);
});
