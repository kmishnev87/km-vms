import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source = fs.readFileSync(new URL("../lib/cameraPtzController.js", import.meta.url), "utf8");
const { createPtzController } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const settle = async () => { for (let n = 0; n < 30; n++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };

function harness(respond) {
  const calls = [], errors = [], timers = new Map();
  let time = 0, serial = 0, timerId = 0, current = true;
  const controller = createPtzController({
    send: async payload => {
      calls.push(payload);
      return respond ? respond(payload) : payload.action === "stop" ? { camera_stopped: true }
        : { executed: true, movement_mode: "continuous", lease_seconds: 1.5 };
    },
    makeId: () => `gesture-${++serial}`, now: () => time, isCurrent: () => current,
    onError: error => errors.push(error),
    schedule: (run, delay) => { const id = ++timerId; timers.set(id, { run, due: time + delay }); return id; },
    unschedule: id => timers.delete(id),
  });
  const advance = async ms => {
    const end = time + ms;
    for (let guard = 0; guard < 100; guard++) {
      const next = [...timers].filter(([, t]) => t.due <= end).sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      time = next[1].due; timers.delete(next[0]); next[1].run(); await settle();
    }
    time = end; await settle();
  };
  const tap = direction => { assert.equal(controller.press("move", direction, .08), true); controller.release(); };
  return { controller, calls, errors, timers, advance, tap, changeSession: () => { current = false; } };
}

test("three fast taps execute three distinct steps in FIFO order with no first-press delay", async () => {
  const h = harness(); h.tap("left"); h.tap("left"); h.tap("left");
  assert.equal(h.calls.length, 1, "the first move is sent on pointerdown, without a tap-classification timer");
  await settle(); await h.advance(300);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop", "move", "stop", "move", "stop"]);
  assert.deepEqual(h.calls.filter(p => p.action === "move").map(p => p.control_id), ["gesture-1", "gesture-2", "gesture-3"]);
  assert.equal(h.timers.size, 0); assert.equal(h.errors.length, 0);
});

test("a quick release waits for its move acknowledgement instead of cancelling a queued tap", async () => {
  const first = deferred();
  const h = harness(p => p.action === "stop" ? { camera_stopped: true }
    : p.control_id === "gesture-1" ? first.promise : { executed: true, movement_mode: "continuous" });
  h.tap("left"); h.tap("up"); await h.advance(50);
  assert.deepEqual(h.calls.map(p => p.action), ["move"]);
  first.resolve({ executed: true, movement_mode: "continuous" }); await settle(); await h.advance(200);
  assert.deepEqual(h.calls.filter(p => p.action === "move").map(p => p.direction), ["left", "up"]);
  assert.equal(h.timers.size, 0);
});

test("the next step cannot overtake the previous Stop acknowledgement", async () => {
  const stop = deferred(); let stops = 0;
  const h = harness(p => p.action === "stop" ? (++stops === 1 ? stop.promise : { camera_stopped: true }) : { executed: true });
  h.tap("left"); h.tap("right"); await settle(); await h.advance(100);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop"]);
  stop.resolve({ camera_stopped: true }); await settle(); await h.advance(100);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop", "move", "stop"]);
});

test("holds still renew continuously and release without waiting for the first reply", async () => {
  const first = deferred();
  const h = harness(p => p.action === "stop" ? { camera_stopped: true } : first.promise);
  h.controller.press("move", "right", .08); await h.advance(300); h.controller.release(); await settle();
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop"]);
  first.resolve({ executed: true, movement_mode: "continuous" }); await settle();
  assert.equal(h.timers.size, 0);
  const normal = harness(); normal.controller.press("zoom", "in", .12); await settle(); await normal.advance(1300);
  assert.equal(normal.calls.filter(p => p.action === "zoom").length, 3);
  normal.controller.release(); await settle(); assert.equal(normal.calls.at(-1).action, "stop");
  assert.equal(normal.timers.size, 0);
});

test("emergency Stop clears queued taps immediately and late replies cannot restart them", async () => {
  const first = deferred(); const h = harness(p => p.action === "stop" ? { camera_stopped: true } : first.promise);
  h.tap("left"); h.tap("up"); h.tap("right"); await h.controller.stop(true);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop"]);
  first.resolve({ executed: true }); await settle(); await h.advance(5000);
  assert.equal(h.calls.length, 2); assert.equal(h.timers.size, 0);
});

test("closing controls clears a running pulse and every queued direction", async () => {
  const h = harness(); h.tap("left"); h.tap("down"); await settle(); await h.controller.dispose();
  await h.advance(5000); assert.deepEqual(h.calls.map(p => p.action), ["move", "stop"]);
  assert.equal(h.timers.size, 0);
  h.controller.activate(); h.tap("right"); await settle(); await h.advance(100);
  assert.deepEqual(h.calls.filter(p => p.action === "move").map(p => p.direction), ["left", "right"]);
});

test("replaced sessions cannot dispatch a queued step", async () => {
  const h = harness(); h.tap("left"); h.tap("right"); await settle(); h.changeSession();
  await h.controller.dispose(); await h.advance(5000);
  assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 0);
});

test("a failed source command stops and discards the queue instead of replaying it after reconnect", async () => {
  const h = harness(p => { if (p.action === "stop") return { camera_stopped: true }; throw new Error("offline"); });
  h.tap("left"); h.tap("right"); await settle(); await h.advance(5000);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop"]);
  assert.equal(h.errors.length, 1); assert.equal(h.timers.size, 0);
});

test("a failed Stop blocks the remaining queue without replaying accepted movements", async () => {
  const h = harness(p => { if (p.action === "stop") throw new Error("Stop lost"); return { executed: true }; });
  h.tap("left"); h.tap("right"); await settle(); await h.advance(5000);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop"]);
  assert.equal(h.errors.length, 1); assert.equal(h.timers.size, 0);
});

test("a released press waiting behind another command becomes one finite step, not a late hold", async () => {
  const first = deferred();
  const h = harness(p => p.action === "stop" ? { camera_stopped: true }
    : p.control_id === "gesture-1" ? first.promise : { executed: true });
  h.tap("left"); h.controller.press("move", "right", .08); await h.advance(1000); h.controller.release();
  first.resolve({ executed: true }); await settle(); await h.advance(200);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop", "move", "stop"]);
  assert.equal(h.timers.size, 0);
});

test("goto preset cancels queued directions before sending its discrete command", async () => {
  const h = harness(); h.tap("left"); h.tap("right"); await settle();
  assert.equal(await h.controller.gotoPreset("home"), true); await h.advance(5000);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop", "goto_preset"]);
  assert.equal(h.timers.size, 0);
});

test("an idle or fully drained queue has no polling or renewal timer", async () => {
  const h = harness(); await h.advance(60000); assert.equal(h.calls.length, 0); assert.equal(h.timers.size, 0);
  h.tap("left"); await settle(); await h.advance(60000);
  assert.deepEqual(h.calls.map(p => p.action), ["move", "stop"]); assert.equal(h.timers.size, 0);
});
