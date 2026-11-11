import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const code = fs.readFileSync(new URL("../lib/liveAspectLayout.js", import.meta.url), "utf8");
const { fitCameraFrames, savedFrameRatio, sameFrameLayout } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const size = { width: 1466, height: 1232 };
const camera = (id, width = 1920, height = 1080) => ({ id, stream_dimensions: { main: { width: 3840, height: 2160 }, sub: { width, height } } });
const tile = id => ({ id: `view-${id}`, cameraId: String(id), stream: "sub", xPct: 0, yPct: 0, wPct: .3, hPct: .3, z: id });
function check(result, cameras, bounds) {
  const rectangles = result.map(t => ({ x: t.xPct * bounds.width, y: t.yPct * bounds.height, w: t.wPct * bounds.width, h: t.hPct * bounds.height }));
  rectangles.forEach((r, i) => {
    assert.ok([r.x, r.y, r.w, r.h].every(Number.isFinite));
    assert.ok(r.x >= -.001 && r.y >= -.001 && r.x + r.w <= bounds.width + .001 && r.y + r.h <= bounds.height + .001);
    assert.ok(r.w > 2 && r.h > 36);
    const c = cameras.find(c => String(c.id) === result[i].cameraId);
    assert.ok(Math.abs((r.w - 2) / (r.h - 36) - savedFrameRatio(c, result[i].stream)) < .00001, "frame aspect changed");
    rectangles.slice(i + 1).forEach(b => assert.ok(r.x + r.w <= b.x + .001 || b.x + b.w <= r.x + .001 || r.y + r.h <= b.y + .001 || b.y + b.h <= r.y + .001, "overlapping tiles"));
  });
  return rectangles.reduce((sum, r) => sum + (r.w - 2) * (r.h - 36), 0);
}

test("saved dimensions distinguish Main/Sub and lenses without waiting for video", () => {
  const c = camera(1, 640, 480);
  assert.equal(savedFrameRatio(c, "main"), 16 / 9);
  assert.equal(savedFrameRatio(c, "sub"), 4 / 3);
  assert.equal(savedFrameRatio(camera(2, 4096, 1024), "sub"), 4);
  for (const stream_dimensions of [{}, {sub:{width:0,height:0}}, {sub:{width:Infinity,height:1}}])
    assert.equal(savedFrameRatio({stream_dimensions}, "sub"), 16 / 9);
});

test("four cameras keep full frames while five no longer collapse into five narrow strips", () => {
  for (const n of [4, 5]) {
    const cameras = Array.from({length:n}, (_, i) => camera(i+1)), tiles = cameras.map(c=>tile(c.id));
    const result = fitCameraFrames(tiles, cameras, size);
    const area = check(result, cameras, size);
    if (n === 5) {
      const oldFrameH = size.height / n - 36;
      assert.ok(area > n * oldFrameH * oldFrameH * (16 / 9) * 2);
      assert.ok(result.some(t=>t.wPct < .9));
    }
    assert.equal(result.length, n);
  }
});

test("mixed panoramic, portrait, square and standard frames stay uncropped and do not overlap", () => {
  const cameras = [camera(1,4096,1024), camera(2,1080,1920), camera(3,640,480), camera(4,1080,1080), camera(5)];
  const tiles = cameras.map(c=>tile(c.id));
  for (const bounds of [size, {width:1600,height:900}, {width:390,height:844}]) {
    check(fitCameraFrames(tiles, cameras, bounds), cameras, bounds);
  }
});

test("configuration changes recalculate deterministically, without mutating or replacing views", () => {
  const cameras = [camera(1),camera(2),camera(3)], tiles = cameras.map(c=>tile(c.id));
  const original = JSON.stringify(tiles), result = fitCameraFrames(tiles,cameras,size);
  assert.equal(JSON.stringify(tiles), original);
  assert.ok(sameFrameLayout(result,fitCameraFrames(result,cameras,size)));
  assert.ok(!sameFrameLayout(result,fitCameraFrames(result,cameras,{width:1600,height:700})));
  result.forEach((t,i)=> {assert.equal(t.id,tiles[i].id); assert.equal(t.stream,tiles[i].stream); assert.equal(t.z,tiles[i].z);});
  assert.equal(fitCameraFrames([],[],size).length,0);
  assert.equal(fitCameraFrames(tiles,cameras,{width:0,height:0}),tiles);
});

test("bounded larger layouts remain complete and geometrically valid", () => {
  const start = performance.now();
  for (const n of [1,2,3,8,12,16,32,64]) {
    const cameras=Array.from({length:n},(_,i)=>camera(i+1, i%3===0?640:1920,i%3===0?480:1080));
    const tiles=cameras.map(c=>tile(c.id));
    check(fitCameraFrames(tiles,cameras,{width:1600,height:900}),cameras,{width:1600,height:900});
  }
  assert.ok(performance.now()-start<4000,"layout search must be bounded");
});

test("narrow five-camera view leaves toolbar width without cropping or covering neighbours", () => {
  const cameras=Array.from({length:5},(_,i)=>camera(i+1,i===2?2048:1920,i===2?928:1080));
  const tiles=cameras.map(c=>tile(c.id)), bounds={width:357,height:718};
  const frames=fitCameraFrames(tiles,cameras,bounds);
  const padded=fitCameraFrames(tiles,cameras,bounds,{minimumWidth:220});
  padded.forEach((tile,i)=> {
    assert.ok(tile.wPct*bounds.width>=219.99);
    assert.ok(tile.xPct>=0&&tile.xPct+tile.wPct<=1.000001);
    assert.equal(tile.hPct,frames[i].hPct);
    assert.ok(tile.wPct>=frames[i].wPct);
  });
  assert.ok(sameFrameLayout(padded,fitCameraFrames(padded,cameras,bounds,{minimumWidth:220})));
});

test("the new button is separate, old Align/All remain grid based and manual editing releases the mode", () => {
  const live=fs.readFileSync(new URL("../app/live/page.js",import.meta.url),"utf8");
  assert.match(live,/data-workspace-align-button="frames"/);
  assert.match(live,/data-workspace-align-button="grid-2x2"/);
  assert.match(live,/function autoLayoutTiles\(\) \{\s*setFrameLayoutActive\(false\)/);
  assert.match(live,/return layoutTiles\(\[\.\.\.active, \.\.\.additions\]/);
  assert.match(live,/const frameLayoutKey = .*savedFrameRatio/);
  assert.match(live,/new ResizeObserver\(measure\)/);
  assert.match(live,/setFrameLayoutActive\(false\);\s*updateTile\(dragState/);
});
