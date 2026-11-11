import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = path => fs.readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const load = path => import(`data:text/javascript;base64,${Buffer.from(read(path)).toString("base64")}`);
const { ptzSpeedLevel, clampPtzPanel, ptzImagePoint } = await load("lib/ptzUiGeometry.js");
const { createPtzController } = await load("lib/cameraPtzController.js");

test("speed drag reaches each discrete level and clamps outside the track", () => {
  assert.deepEqual([-100,8,33,58,83,108,500].map(x => ptzSpeedLevel(x,0,116)), [1,1,2,3,4,5,5]);
});
test("floating controls stay reachable after dragging or a viewport resize", () => {
  assert.deepEqual(clampPtzPanel({x:900,y:900},{width:800,height:600},{width:186,height:350}), {x:602,y:238});
  assert.deepEqual(clampPtzPanel({x:-100,y:-100},{width:100,height:100},{width:186,height:350}), {x:12,y:12});
});
test("point positioning uses the actual image, not its letterbox", () => {
  const bounds={left:100,top:200,width:400,height:400}, dimensions={width:1920,height:1080};
  assert.deepEqual(ptzImagePoint(300,400,bounds,dimensions), {x:.5,y:.5});
  assert.equal(ptzImagePoint(300,220,bounds,dimensions), null);
  assert.equal(ptzImagePoint(50,400,bounds,dimensions), null);
  assert.equal(ptzImagePoint(300,400,bounds,{width:0,height:0}), null);
});
test("point is a single command with no timer or zoom and rejects invalid coordinates", async () => {
  const calls=[];
  const controller=createPtzController({send:async p=>{calls.push(p);return {executed:true};},makeId:()=>"unit-gesture"});
  assert.equal(await controller.point({x:.25,y:.75}),true);
  assert.deepEqual(calls,[{action:"point",x:.25,y:.75,dry_run:false}]);
  assert.equal(await controller.point({x:Infinity,y:0}),false);
  assert.equal(await controller.point({x:-.1,y:0}),false);
  assert.equal(calls.length,1);
  await controller.dispose();
});
test("only the selected PTZ camera changes transport and ordinary HLS remains unchanged", () => {
  const live=read("app/live/page.js"), player=read("components/TilePlayer.js");
  assert.match(live,/lowLatency=\{ptzCameraId === camera.id\}/);
  assert.match(live,/speed=\{ptzPreferences\[camera.id\]\?\.speed\}/);
  assert.match(player,/liveSyncDurationCount: 2/);
  assert.match(player,/\[cameraId, stream, lowLatency\]/);
  assert.match(player,/mseRef.current\?\.destroy\(\)/);
  assert.match(player,/data-live-transport=\{transport\}/);
});
test("interactive controls cannot accidentally start camera or panel dragging", () => {
  const controls=read("components/cameras/CameraPtz.js"), panel=read("components/cameras/FloatingCameraPtz.js");
  assert.match(controls,/speedPointer.current = event.pointerId/);
  assert.match(controls,/event.preventDefault\(\); event.stopPropagation\(\); event.currentTarget.focus\(\)/);
  assert.match(controls,/className="ptzSpeedTicks"/);
  assert.match(panel,/button,input,select,textarea,a,.ptzSpeed/);
  assert.match(panel,/setPointerCapture/);
  assert.match(panel,/ResizeObserver/);
});
