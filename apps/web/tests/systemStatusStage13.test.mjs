import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");

function read(path) {
  return fs.readFileSync(resolve(root, path), "utf8");
}

function exists(path) {
  return fs.existsSync(resolve(root, path));
}

const home = read("app/page.js");
const layout = read("components/Layout.js");
const indicator = read("components/SystemHealthIndicator.js");
const systemStatus = read("app/system-status/page.js");
const apk = read("app/apk/page.js");
const api = read("lib/api.js");
const i18n = read("lib/i18n.js");
const css = read("app/globals.css");

for (const asset of [
  "public/assets/icons/dashboard/system-status-base.png",
  "public/assets/icons/dashboard/system-status-alert.png",
  "public/assets/backgrounds/dashboard-cards/system-status.svg",
  "public/assets/icons/ui/system-status-base.png",
  "public/assets/icons/ui/system-status-alert.png",
  "public/assets/icons/ui/diagnostics.svg",
  "public/assets/icons/dashboard/apk.png",
  "public/assets/backgrounds/dashboard-cards/apk.svg",
  "public/assets/backgrounds/dashboard-cards/storage.svg",
]) {
  assert.equal(exists(asset), true, `${asset} missing`);
}

assert.equal(home.includes("OperatorProblemBanners"), false);
assert.equal(home.includes('href: "/system-status"'), true);
assert.equal(home.includes('href: "/apk"'), true);
assert.equal(home.includes("system-status-base.png"), true);
assert.equal(home.includes("system-status-alert.png"), true);
assert.equal(home.includes("dashboard-cards/system-status.svg"), true);
assert.equal(home.includes("dashboard-cards/apk.svg"), true);
assert.equal(home.includes("dashboard-cards/storage.svg"), true);
assert.equal(home.includes("dashboard-cards/settings.svg"), true);
assert.equal(home.includes('href: "/storage"'), true);

const dashboardOrder = [
  'href: "/cameras"',
  'href: "/recordings"',
  'href: "/live"',
  'href: "/chronology"',
  'href: "/storage"',
  'href: "/system-status"',
  'href: "/apk"',
  'href: "/settings"',
].map((needle) => home.indexOf(needle));
assert.equal(dashboardOrder.every((index) => index >= 0), true);
assert.deepEqual([...dashboardOrder].sort((a, b) => a - b), dashboardOrder);

assert.equal(layout.includes("LanguageSelect"), false);
assert.equal(layout.includes("topLanguageSelect"), false);
assert.equal(layout.includes("topUserChip"), false);
assert.equal(layout.includes("username"), false);
assert.equal(layout.includes('href="/storage"'), true);
assert.equal(layout.includes("<SystemHealthIndicator"), true);
assert.equal(layout.indexOf('href="/storage"') < layout.indexOf("<SystemHealthIndicator"), true);
assert.equal(layout.indexOf("<SystemHealthIndicator") < layout.indexOf('href="/settings"'), true);
assert.equal(layout.indexOf('href="/settings"') < layout.indexOf("topNavButton"), true);

assert.equal(indicator.includes('apiFetch("/system/runtime/status")'), true);
assert.equal(indicator.includes("userCanReadRuntimeStatus(currentUser)"), true);
assert.equal(indicator.includes("setInterval(loadStatus, REFRESH_MS)"), true);
assert.equal(indicator.includes("system-status-base.png"), true);
assert.equal(indicator.includes("system-status-alert.png"), true);
assert.equal(css.includes("@keyframes systemHealthPulse"), true);
assert.equal(css.includes(".systemHealthNavItem.attention"), true);

assert.equal(api.includes('if (href === "/system-status") return permissions.has("run_diagnostics");'), true);
assert.equal(api.includes('if (href === "/apk") return true;'), true);

assert.equal(systemStatus.includes('apiFetch("/system/runtime/status")'), false);
assert.equal(systemStatus.includes("useSystemHealthStatus"), true);
assert.equal(systemStatus.includes("systemStatusPanel"), true);
assert.equal(systemStatus.includes("systemStatusRail"), true);
assert.equal(systemStatus.includes("systemStatusDetail"), true);
assert.equal(systemStatus.includes("systemStatus.safeHint"), true);
assert.equal(systemStatus.includes("rtsp://"), false);
assert.equal(systemStatus.includes("Authorization"), false);

assert.equal(apk.includes("Mobile Client PRO"), false);
assert.equal(apk.includes("download"), false);
assert.equal(apk.includes("href=\"/\""), true);
assert.equal(apk.includes("apkPage.text"), true);

for (const key of [
  "nav.systemHealth",
  "dashboard.systemHealthTitle",
  "dashboard.apkTitle",
  "systemStatus.title",
  "systemStatus.forbidden",
  "apkPage.title",
]) {
  const parts = key.split(".");
  assert.equal(i18n.includes(`${parts.at(-1)}:`), true, `${key} missing`);
}

assert.equal(i18n.includes("Состояние системы"), true);
assert.equal(i18n.includes("System Health"), true);
assert.equal(i18n.includes("系统状态"), true);
assert.equal(i18n.includes("Скачать APK"), true);
assert.equal(i18n.includes("Download APK"), true);
assert.equal(i18n.includes("下载 APK"), true);

for (const forbidden of [
  "/system/update/apply",
  "/system/migrations/apply",
  "/system/restore/apply",
  "/system/db-adoption/apply",
  "apk.zip",
  "application/vnd.android.package-archive",
  "download=",
]) {
  assert.equal(`${home}\n${layout}\n${systemStatus}\n${apk}`.includes(forbidden), false, `${forbidden} should be absent`);
}
