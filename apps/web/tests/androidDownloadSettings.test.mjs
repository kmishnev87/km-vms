import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { canUserAccessRoute } from "../lib/routePermissions.js";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
test("APK navigation is removed from Home and placed before Security in Settings", () => {
  const home = read("../app/page.js");
  const settings = read("../app/settings/page.js");
  assert.doesNotMatch(home, /href:\s*"\/apk"/);
  assert.equal((settings.match(/<AndroidDownloadSetting\s*\/>/g) || []).length, 1);
  assert.ok(settings.indexOf("<AndroidDownloadSetting />") < settings.indexOf('src="/assets/icons/ui/security.png"'));
});
test("download reuses official metadata with bounded on-demand checks, not polling", () => {
  const component = read("../components/AndroidDownloadSetting.js");
  assert.match(component, /fetchAndroidRelease\(controller.signal\)/);
  assert.match(component, /href=\{state.release.url\}/);
  assert.match(component, /controller.abort\(\)/);
  assert.match(component, /15000/);
  assert.doesNotMatch(component, /setInterval|apiFetch|localStorage|window.open/);
  assert.doesNotMatch(component, /androidDownload.check"|setAttempt|settingsAndroidRefresh/);
  assert.match(component, /appIllustratedAction settingsUsersAddButton/);
  assert.match(component, /download-report.svg/);
  assert.match(component, /assets\/icons\/dashboard\/apk.png/);
});
test("the web APK metadata is bundled with the installed release, not checked against GitHub main", () => {
  const helper = read("../lib/androidRelease.js");
  assert.match(helper, /ANDROID_RELEASE_METADATA = "\/android-release.json"/);
  assert.doesNotMatch(helper, /raw.githubusercontent.com/);
  const snapshot = JSON.parse(read("../public/android-release.json"));
  assert.equal(snapshot.format, 1);
  assert.equal(snapshot.package_name, "ru.kmvms.mobile");
  assert.doesNotMatch(read("../app/apk/page.js"), /androidDownload.check"|setAttempt/);
});
test("moving the tile does not restrict the existing public APK link", () => {
  assert.equal(canUserAccessRoute(null, "/apk"), true);
  assert.match(read("../app/apk/page.js"), /fetchAndroidRelease/);
});
