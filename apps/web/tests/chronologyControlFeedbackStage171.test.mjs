import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import test from "node:test";

const page = fs.readFileSync(new URL("../app/chronology/page.js", import.meta.url), "utf8");
const css = fs.readFileSync(new URL("../app/styles/100-chronology-workspace.css", import.meta.url), "utf8");

test("play feedback follows real playback or a pending resume, never a manual pause", () => {
  const code = page.slice(page.indexOf("  const playPending ="), page.indexOf("  const [date, setDate]"));
  for (const [isPlaying, releaseState, shouldResume, expected, busy] of [
    [false, "released", false, false, false], [true, "released", true, true, false],
    [false, "holding", true, true, true], [false, "holding", false, false, false],
    [false, "cancelled", false, false, false],
  ]) {
    const ctx = { isPlaying, playbackCoordinator: { releaseState, shouldResume } };
    vm.runInNewContext(code + "\nthis.value={playRequested,playPending,playButtonState};", ctx);
    assert.equal(ctx.value.playRequested, expected);
    assert.equal(ctx.value.playPending, busy);
    assert.equal(ctx.value.playButtonState, busy ? "isWaiting" : isPlaying ? "isPlaying" : "");
  }
});

test("each press has bounded glyph-only feedback and reduced motion skips it", () => {
  const code = page.slice(page.indexOf("  function acknowledgePlay("), page.indexOf("  function handlePlay("));
  for (const reduced of [false, true]) {
    const calls = [], glyph = { animate: (...args) => calls.push(args) };
    const ctx = { window: { matchMedia: query => { assert.equal(query, "(prefers-reduced-motion: reduce)"); return { matches: reduced }; } } };
    vm.runInNewContext(code + "\nthis.press=acknowledgePlay;", ctx);
    for (let i = 0; i < 2; i++) ctx.press({ currentTarget: { querySelector: selector => { assert.equal(selector, ".chronologyPlayGlyph"); return glyph; } } });
    assert.equal(calls.length, reduced ? 0 : 2);
    if (!reduced) {
      assert.equal(calls[0][1].duration, 260);
      assert.deepEqual(Object.keys(calls[0][0][1]).sort(), ["opacity", "transform"]);
      ctx.press(); // Non-button shortcuts do not throw or create timers.
      assert.equal(calls.length, 2);
    }
  }
});

test("both play buttons expose intent and waiting without changing playback handlers", () => {
  assert.equal((page.match(/aria-pressed=\{playRequested\}/g) || []).length, 2);
  assert.equal((page.match(/aria-busy=\{playPending\}/g) || []).length, 2);
  assert.match(page, /acknowledgePlay\(event\); handlePlay\(\);/);
  assert.match(page, /if \(isPlaying\) handlePause\(\);/);
  assert.match(page, /aria-pressed=\{speed === item.value\}/);
});

test("soft control palette, keyboard focus and reduced-motion waiting are scoped to chronology", () => {
  for (const selector of [".chronologyPrimaryButton", ".chronologySpeedButton.active"]) {
    const rule = css.slice(css.indexOf(selector + " {"));
    const body = rule.slice(0, rule.indexOf("}"));
    assert.match(body, /background: #(eff6ff|dbeafe);/);
    assert.match(body, /color: #1e40af;/);
    assert.doesNotMatch(body, /height:|width:|padding:|margin:/);
  }
  assert.match(css, /\.chronologyPlayButton:focus-visible/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)[\s\S]*?\.chronologyPlayButton.isWaiting \.chronologyPlayGlyph \{\s*animation: none;/);
});
