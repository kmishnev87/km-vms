import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");
const exists = (relative) => fs.existsSync(resolve(root, relative));

const globals = read("app/globals.css");
const expectedImports = [
  "styles/00-base-layout-nav.css",
  "styles/10-dashboard-auth-setup.css",
  "styles/20-settings-maintenance.css",
  "styles/30-system-status-apk.css",
  "styles/40-storage-records-shared.css",
  "styles/50-cameras-shared-modals.css",
  "styles/60-responsive-shared.css",
  "styles/70-live.css",
  "styles/80-chronology-timeline.css",
  "styles/90-live-workspace.css",
  "styles/100-chronology-workspace.css",
];

const actualImports = [...globals.matchAll(/@import\s+"\.\/([^"]+)";/g)].map((match) => match[1]);
assert.deepEqual(actualImports, expectedImports);
assert.equal(globals.match(/@import/g)?.length, expectedImports.length);
assert.equal(globals.includes("*.module.css"), false);

const cssByFile = new Map();
for (const file of expectedImports) {
  assert.equal(exists(`app/${file}`), true, `${file} missing`);
  const css = read(`app/${file}`);
  assert.ok(css.trim().length > 120, `${file} should not be an empty placeholder`);
  assert.equal(css.includes("@import"), false, `${file} must not create nested import order`);
  cssByFile.set(file, css);
}

const effectiveCss = expectedImports.map((file) => cssByFile.get(file)).join("\n");

for (const selector of [
  ":root",
  ".topNavInner",
  ".mainContent",
  ".standardPage",
  ".dashboardPage",
  ".settingsPage",
  ".systemStatusPage",
  ".apkPage",
  ".storageOpsPage",
  ".recordingsFilterCard",
  ".cameraSettingSlot.editable",
  ".liveWorkspaceShell",
  ".liveWorkspaceCanvas.isEditing",
  ".chronologyShell.systemFullscreen",
  ".chronologyWorkspace.isEditing",
]) {
  assert.equal(effectiveCss.includes(selector), true, `${selector} missing from split CSS`);
}

assert.match(cssByFile.get("styles/00-base-layout-nav.css"), /--page-working-width:\s*1400px;/);
assert.match(cssByFile.get("styles/00-base-layout-nav.css"), /\.topNavInner\s*\{[\s\S]*?width:\s*calc\(100% - \(var\(--page-outer-padding\) \* 2\)\);/);
assert.match(cssByFile.get("styles/60-responsive-shared.css"), /@media \(max-width:\s*640px\)[\s\S]*?\.topNavInner\s*\{[\s\S]*?width:\s*100%;/);

const dynamicClassSources = [
  read("app/cameras/page.js"),
  read("app/live/page.js"),
  read("app/chronology/page.js"),
  read("app/recordings/page.js"),
  read("app/storage/page.js"),
  read("app/system-status/page.js"),
  read("app/settings/page.js"),
].join("\n");

for (const dynamicSelector of [
  "cameraSettingSlot.editable",
  "cameraProfileCard.selected",
  "liveWorkspaceCanvas.isEditing",
  "liveWorkspaceTile.active",
  "chronologyShell.systemFullscreen",
  "chronologyShell.sidebarOpen",
  "chronologyWorkspace.isEditing",
  "recordingsSortButton.active",
  "storageOpsBadge.ok",
  "systemStatusIncident.severity-error",
  "settingsToast.success",
]) {
  const className = dynamicSelector.split(".").join(" ");
  assert.equal(
    dynamicClassSources.includes(dynamicSelector) || effectiveCss.includes(`.${dynamicSelector}`) || dynamicClassSources.includes(className.split(" ")[0]),
    true,
    `${dynamicSelector} dynamic/composed class coverage missing`
  );
}

for (const forbidden of [
  ".module.css",
  "rtsp://",
  "Authorization",
  ".env",
]) {
  assert.equal(`${globals}\n${effectiveCss}`.includes(forbidden), false, `${forbidden} must stay out of CSS sources`);
}
