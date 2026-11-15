import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import {createArchiveBufferPolicy,requiredArchiveBuffer} from "../lib/archiveBufferPolicy.js";
import {archiveTargetReady,archiveBufferedAhead} from "../lib/archiveGroupPlayback.js";
import {createPlaybackCoordinator} from "../lib/chronologyPlaybackCoordinator.js";

const video=(ahead)=>({readyState:3,seeking:false,currentTime:10,duration:110,buffered:{length:1,start:()=>10,end:()=>10+ahead}});
test("normal fast startup does not inherit a long refill wait",()=>{
  assert.equal(archiveTargetReady(video(.6),10,{resume:true}),true);
  const policy=createArchiveBufferPolicy();assert.equal(policy.reserve(),.5);
});
test("refill requires continuous playable seconds, including speed and the actual end",()=>{
  assert.equal(archiveTargetReady(video(1),10,{resume:true,reserve:8}),false);
  assert.equal(archiveTargetReady(video(8),10,{resume:true,reserve:8}),true);
  assert.equal(archiveTargetReady(video(8),10,{resume:true,reserve:8,speed:2}),false);
  assert.equal(archiveTargetReady({...video(2),duration:12},10,{resume:true,reserve:8}),true);
  const fragmented=video(1);fragmented.buffered={length:2,start:i=>i?20:10,end:i=>i?50:11};
  assert.equal(archiveBufferedAhead(fragmented),1);
  assert.equal(archiveTargetReady(fragmented,10,{resume:true,reserve:8}),false);
  assert.equal(requiredArchiveBuffer({reserve:18,speed:4,capacity:5}),5);
});
test("starvation adapts only within its cap and decays after stable playback",()=>{
  let now=0;const policy=createArchiveBufferPolicy(()=>now);
  for(const value of [8,12,18,18]){policy.starved();assert.equal(policy.reserve(),value);policy.resumed();}
  now=60_001;policy.progress();assert.equal(policy.reserve(),12);
  policy.held();now+=300_000;policy.progress();assert.equal(policy.reserve(),12);
  policy.reset();assert.equal(policy.reserve(),.5);
});
test("the entire group refills, not only the camera which originally stalled",()=>{
  const resumes=[],pauses=[];
  const store=createPlaybackCoordinator({onResume:()=>resumes.push(true)});
  for(const id of ["a","b","c","empty"])store.registerTile(id,{pause:()=>pauses.push(id),sample:()=>({positionMs:10_000})});
  store.start({targetMs:10_000,shouldResume:true,expectedTileIds:["a","b","c","empty"]});
  const ready=(id,ahead)=>store.updateTile(id,store.getSnapshot().id,"ready",{bufferedAheadSeconds:ahead,speed:1});
  ready("a",1);ready("b",1);ready("c",1);store.updateTile("empty",store.getSnapshot().id,"empty");
  assert.equal(resumes.length,1);
  store.updateTile("a",store.getSnapshot().id,"buffering");
  assert.equal(store.getSnapshot().bufferReserveSeconds,8);
  ready("a",10);ready("b",2);ready("c",5);store.updateTile("empty",store.getSnapshot().id,"empty");
  assert.equal(resumes.length,1);assert.equal(store.getSnapshot().releaseState,"holding");
  ready("b",9);assert.equal(resumes.length,1);
  ready("c",8);assert.equal(resumes.length,2);
  assert.equal(pauses.length,8);
  store.updateTile("a",store.getSnapshot().id,"buffering");
  assert.equal(store.getSnapshot().bufferReserveSeconds,12);
  store.cancel("paused");const operation=store.getSnapshot().id;
  for(const id of ["a","b","c"])store.updateTile(id,operation,"ready",{bufferedAheadSeconds:60});
  assert.equal(resumes.length,2);store.dispose();
});
test("archive players keep preload enabled and do not destroy their source on an ordinary hold",()=>{
  const player=fs.readFileSync(new URL("../components/ArchiveTilePlayer.js",import.meta.url),"utf8");
  assert.match(player,/preload="auto"/);
  assert.match(player,/poll=setInterval\(complete,250\)/);
  const registration=player.slice(player.indexOf("return registerPlaybackTile"),player.indexOf("function applyNaturalResolution"));
  assert.match(registration,/videoRef\.current\?\.pause\(\)/);
  assert.doesNotMatch(registration,/\.load\(|removeAttribute|abort\(/);
});
test("the Records filter wrapper owns the width, so its button cannot overlap the date",()=>{
  const css=fs.readFileSync(new URL("../app/styles/40-09-storage-migration-integrity.css",import.meta.url),"utf8");
  assert.match(css,/\.archiveCameraSelect\s*\{[^}]*flex: 0 0 280px/);
  assert.match(css,/\.archiveCameraTrigger\.recordingsFilterSelect\s*\{[^}]*width: 100%; min-width: 0/);
});
