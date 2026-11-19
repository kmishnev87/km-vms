import test from "node:test";
import assert from "node:assert/strict";
import { liveGeometryMatches, readViewReference, viewMatchScore, viewStability } from "../lib/aiLiveView.js";

test("fixed and unverified lens facts are separate; fixed contours only need compatible geometry", () => {
  const saved = {ratio:2202,edges:Buffer.alloc(112).toString("base64url")};
  assert.equal(readViewReference(saved).mode, "ptz"); // Older NAS response.
  assert.equal(readViewReference({...saved,mode:"unknown"}).mode, "unknown");
  const fixed = readViewReference({...saved,mode:"fixed"});
  assert.equal(fixed.mode, "fixed");
  assert(liveGeometryMatches(fixed, 2048, 928));
  assert(!liveGeometryMatches(fixed, 640, 360));
});

test("view match rejects crop and a moved camera; stable return does not blink", () => {
  const luma = Uint8Array.from({ length: 240 }, (_, i) => i % 2 ? 180 : 45);
  const packed = Buffer.alloc(112);
  for (let p = 0; p < 228; p++) packed[p >> 2] |= (p % 19 % 2 === 0 ? 1 : 2) << (2 * (p & 3));
  const ref = readViewReference({ ratio: 1778, edges: packed.toString("base64url") });
  assert.equal(viewMatchScore(ref, luma, 1920, 1080), 1);
  assert.equal(viewMatchScore(ref, luma, 640, 480), 0);
  assert.equal(viewMatchScore(ref, luma.map(n => 255-n), 1920, 1080), 0);
  let state = { good: 0, bad: 0, visible: false };
  for (let i = 0; i < 3; i++) state = viewStability(state, .9);
  assert.equal(state.visible, true);
  assert.equal(viewStability(state, .65).visible, true);
  assert.equal(viewStability(state, 0).visible, false);
  assert.equal(viewStability(viewStability(state, 0), .98).visible, false);
});

test("a stationary textured view keeps its contours after contrast reversal and rescaling, not after a turn", () => {
  const luma = Uint8Array.from({ length:240 }, (_, i) => {
    const x = i % 20, y = Math.floor(i/20);
    const block = Math.floor(x/4) + 5*Math.floor(y/3);
    return 25 + ((block*59 + block*block*17) % 199);
  });
  const packed = Buffer.alloc(112);
  let index = 0;
  function sign(delta) {
    packed[index >> 2] |= (delta >= 12 ? 1 : delta <= -12 ? 2 : 0) << (2*(index & 3));
    index++;
  }
  for (let y=0;y<12;y++) for (let x=0;x<19;x++) sign(luma[y*20+x+1]-luma[y*20+x]);
  for (let i=0;i<220;i++) sign(luma[i+20]-luma[i]);
  const ref = readViewReference({ratio:2202,edges:packed.toString("base64url")});
  const infrared = luma.map(n => 218 - Math.round(n*.6));
  const mixedIr = luma.map((n,i) => i < 120 ? n : 255-n);
  assert(viewMatchScore(ref, luma, 1920, 872) >= .77);
  assert(viewMatchScore(ref, infrared, 1920, 872) >= .77);
  assert(viewMatchScore(ref, mixedIr, 1920, 872) >= .77);
  assert(viewMatchScore(ref, infrared, 640, 360) >= .77);
  const shifted = infrared.map((n,i) => i%20 < 18 ? infrared[i+2] : 35);
  assert(viewMatchScore(ref, shifted, 640, 360) < .48);
  const crop = infrared.map((n,i) => infrared[(3+Math.floor(i/40))*20 + 5 + Math.floor((i%20)/2)]);
  assert(viewMatchScore(ref, crop, 1920, 872) < .48);
  assert.equal(viewMatchScore(ref, new Uint8Array(240).fill(90), 1920, 872), 0);
});
