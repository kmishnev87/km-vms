import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

function readEffectiveCss(relative) {
  const content = read(relative);
  const importMatches = [...content.matchAll(/@import\s+"\.\/([^"]+)";/g)];

  if (!importMatches.length) return content;

  return importMatches.map((match) => read(`app/${match[1]}`)).join("\n");
}

const css = readEffectiveCss("app/globals.css");
const dashboardPage = read("app/page.js");
const camerasPage = read("app/cameras/page.js");
const recordsPage = read("app/recordings/page.js");
const settingsPage = read("app/settings/page.js");
const storagePage = read("app/storage/page.js");
const systemStatusPage = read("app/system-status/page.js");

assert.match(css, /--page-working-width:\s*1400px;/);
assert.match(css, /--page-outer-padding:\s*12px;/);
assert.match(css, /\.dashboardPage\s*\{[\s\S]*?max-width:\s*var\(--page-working-width\);/);
assert.match(css, /\.standardPage\s*\{[\s\S]*?max-width:\s*var\(--page-working-width\);/);
assert.match(css, /\.settingsPage\s*\{[\s\S]*?max-width:\s*var\(--page-working-width\);/);
assert.match(css, /\.storageOpsPage\s*\{[\s\S]*?max-width:\s*var\(--page-working-width\);/);
assert.match(css, /\.systemStatusPage,\s*\.apkPage\s*\{[\s\S]*?max-width:\s*var\(--page-working-width\);/);
assert.match(css, /\.systemStatusPanel\s*\{[\s\S]*?max-width:\s*1180px;/);
assert.match(css, /html\s*\{[\s\S]*?scrollbar-gutter:\s*stable;/);
assert.match(css, /\.topNavInner\s*\{[\s\S]*?width:\s*calc\(100% - \(var\(--page-outer-padding\) \* 2\)\);[\s\S]*?max-width:\s*var\(--page-working-width\);[\s\S]*?padding:\s*0 var\(--page-x-padding\);/);
assert.match(css, /\.mainContent\s*\{[\s\S]*?padding:\s*10px var\(--page-outer-padding\) 16px;/);
assert.match(css, /@media \(max-width:\s*640px\)[\s\S]*?\.topNavInner\s*\{[\s\S]*?width:\s*100%;[\s\S]*?padding:\s*0 8px;/);

assert.equal(dashboardPage.includes('className="dashboardPage"'), true);
assert.equal(camerasPage.includes('className="standardPage"'), true);
assert.equal(recordsPage.includes('className="standardPage"'), true);
assert.equal(settingsPage.includes('className="settingsPage"'), true);
assert.equal(storagePage.includes('className="storageOpsPage"'), true);
assert.equal(systemStatusPage.includes('className="systemStatusPage"'), true);

for (const removedSelector of [
  ".liveLayout",
  ".timelineLayout",
  ".liveColumns",
  ".timelineColumns",
  ".sidePanel",
  ".workPanel",
  ".cameraListItem",
  ".videoGrid",
  ".videoTile",
  ".timelineTile",
  ".liveWorkspace {",
]) {
  assert.equal(css.includes(removedSelector), false, `${removedSelector} should stay removed as UNUSED_STATIC_PROVEN`);
}

assert.equal(read("app/globals.css").includes("@import"), true, "Stage 13 keeps globals.css as the ownership import entrypoint");
