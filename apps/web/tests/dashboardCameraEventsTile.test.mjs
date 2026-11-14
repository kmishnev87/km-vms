import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const read = path => fs.readFileSync(new URL(path, import.meta.url), "utf8");

test("camera events replaces APK in row two and APK moves to ninth, keeping permissions", () => {
  const source = read("../app/page.js");
  const definition = source.match(/const DASHBOARD_ITEMS = (\[[\s\S]*?\n\]);/)[1];
  const items = vm.runInNewContext(definition);
  assert.equal(items.length, 9);
  assert.deepEqual(Array.from(items, item => item.href), ["/cameras", "/recordings", "/live", "/chronology", "/storage", "/system-status", "/?camera_events=1", "/settings", "/apk"]);
  assert.equal(items[7].href, "/settings");
  assert.equal(items[6].href, "/?camera_events=1");
  assert.equal(items[6].permission, "view_live");
  assert.equal(items[6].iconSrc, "/assets/icons/dashboard/events-v2.png");
  assert.equal(items[6].backgroundSrc, "/assets/backgrounds/dashboard-cards/events.svg");
  assert.equal(items[8].kind, "placeholder");
  assert.match(source, /currentUser\.permissions\?\.includes\(item\.permission\)/);
  assert.match(source, /event\.preventDefault\(\)/);
  assert.match(source, /dispatchEvent\(new CustomEvent\(CAMERA_EVENT_HUB_OPEN\)\)/);
});

test("the dashboard opens the existing event dialog, with no fake route or video side effect", () => {
  const hub = read("../components/CameraEventsHub.js");
  assert.match(hub, /if \(!enabled \|\| !session\) return undefined;\s*const show = \(\) => \{ setTab\("history"\); setOpen\(true\); \}/);
  assert.match(hub, /addEventListener\(CAMERA_EVENT_HUB_OPEN, show\)/);
  assert.match(hub, /removeEventListener\(CAMERA_EVENT_HUB_OPEN, show\)/);
  assert.match(hub, /get\("camera_events"\) === "1"/);
});

test("event art uses the same SVG design grammar and all three translations", () => {
  const svg = read("../public/assets/backgrounds/dashboard-cards/events.svg");
  assert.match(svg, /viewBox="0 0 720 260"/);
  assert.match(svg, /linearGradient id="wash"/);
  assert.match(svg, /<g opacity="0.18"/);
  assert.doesNotMatch(svg, /<image|<script|https?:\/\/(?!www\.w3\.org)/);
  for (const locale of ["ru", "en", "zhCN"]) assert.match(read(`../lib/i18n/${locale}.js`), /"dashboardText": ".+"/);
  assert.ok(fs.statSync(new URL("../public/assets/icons/dashboard/events-v2.png", import.meta.url)).size > 0);
});

test("the header uses the approved bell in the same small PNG format and CSS sizing as its neighbours", () => {
  const hub = read("../components/CameraEventsHub.js");
  const css = read("../app/styles/35-camera-events.css");
  assert.match(hub, /<img className="topNavIconImage cameraEventsBell" src="\/assets\/icons\/ui\/events-v2\.png" width="128" height="128" alt="" aria-hidden="true" draggable="false"/);
  assert.doesNotMatch(hub, /camera-event-bell|<svg className="cameraEventsBell"/);
  assert.match(hub, /<EventBell\/>\{unread > 0 \? <span className="cameraEventsBadge">/);
  assert.match(css, /\.cameraEventsBell \{ flex-shrink: 0; \}/);
  assert.doesNotMatch(css, /\.cameraEventsBell\s*\{[^}]*\b(?:width|height):/);
  for (const [path, dimension] of [["dashboard/events-v2.png", 1254], ["ui/events-v2.png", 128]]) {
    const png = fs.readFileSync(new URL(`../public/assets/icons/${path}`, import.meta.url));
    assert.equal(png.subarray(1, 4).toString(), "PNG");
    assert.equal(png.readUInt32BE(16), dimension);
    assert.equal(png.readUInt32BE(20), dimension);
  }
});
