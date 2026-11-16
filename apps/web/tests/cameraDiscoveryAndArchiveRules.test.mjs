import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { ARCHIVE_RULE_COPY, archiveRuleChanges, archiveRuleDraft, archiveRuleProblems, reducedArchiveRules } from "../lib/archiveRules.js";
import { DISCOVERY_COPY, discoveryConnection, discoveryError, discoveryPorts, discoveryProgress, discoveryRanges } from "../lib/cameraDiscovery.js";
const read = path => readFileSync(new URL(path, import.meta.url), "utf8");
const rows = [{ camera_id: 1, name: "Camera", segment_minutes: 5, retention_days: 30, storage_quota_gb: 50 }, { camera_id: 2, name: "Disabled", segment_minutes: 60, retention_days: 31, storage_quota_gb: 200 }];

test("archive draft is separate and changes include exact optimistic expected values", () => {
  const draft = archiveRuleDraft(rows);
  assert.deepEqual(archiveRuleChanges(rows, draft), []);
  draft[1].retention_days = "40";
  assert.deepEqual(archiveRuleChanges(rows, draft), [{ camera_id: 1, expected: { segment_minutes: 5, retention_days: 30, storage_quota_gb: 50 }, values: { retention_days: 40 } }]);
  assert.equal(rows[0].retention_days, 30);
});

test("invalid and reduced retention are distinct, empty values are never silently saved", () => {
  const draft = archiveRuleDraft(rows);
  for (const value of ["", "0", "-1", "1.5", "1e2", "36501"]) {
    draft[1].retention_days = value;
    assert.equal(archiveRuleProblems(rows, draft)["1:retention_days"], true);
  }
  draft[1].retention_days = "36500";
  assert.deepEqual(archiveRuleProblems(rows, draft), {});
  assert.equal(reducedArchiveRules(rows, draft), false);
  draft[2].storage_quota_gb = "100";
  assert.equal(reducedArchiveRules(rows, draft), true);
});

test("RTSP selection preserves intended protocol and nonstandard port", () => {
  const candidate = { host: "192.168.1.2", port: 2020, onvif_endpoints: [{ port: 2020, path: "/onvif" }], rtsp_ports: [8554] };
  const rtsp = discoveryConnection(candidate, "rtsp");
  assert.equal(rtsp.protocol, "rtsp"); assert.equal(rtsp.port, 8554); assert.equal(rtsp.rtsp_main_url, "");
  const onvif = discoveryConnection(candidate, "onvif");
  assert.equal(onvif.protocol, "onvif"); assert.equal(onvif.port, 2020); assert.equal(onvif.rtsp_port, 8554);
  assert.equal(onvif.onvif_path, "/onvif");
  assert.equal(discoveryConnection({ ...candidate, selected_protocol: "rtsp" }, "onvif").protocol, "rtsp");
});

test("discovery ranges and ports are normalized, progress is measured", () => {
  assert.deepEqual(discoveryRanges(["192.168.1.0/24"], "192.168.1.0/24, 192.168.2.5; 192.168.2.6"), ["192.168.1.0/24", "192.168.2.5", "192.168.2.6"]);
  assert.deepEqual(discoveryPorts("80, 2020;80"), [80, 2020]);
  for (const value of ["", "0", "65536", "abc", "1.5"]) assert.equal(discoveryPorts(value), null);
  assert.equal(discoveryProgress({ total_hosts: 254, checked_hosts: 127 }), 50);
  assert.equal(discoveryProgress({ total_hosts: 0 }), null);
});

test("unavailable discovery cannot be presented as a successful empty search", () => {
  for (const locale of ["ru", "en", "zh-CN"]) {
    const copy = DISCOVERY_COPY[locale];
    assert.notEqual(discoveryError({ status: 503, detail: { code: "discovery_unavailable" } }, copy), copy.empty);
    assert.equal(discoveryError({ detail: { code: "network_too_large" } }, copy), copy.invalid);
    assert.equal(discoveryError({ detail: { code: "discovery_busy" } }, copy), copy.busy);
  }
});

test("all new controls have complete three-language copy", () => {
  for (const copy of [ARCHIVE_RULE_COPY, DISCOVERY_COPY]) for (const locale of ["ru", "en", "zh-CN"]) {
    assert.deepEqual(Object.keys(copy[locale]).sort(), Object.keys(copy.en).sort());
    assert(Object.values(copy[locale]).every(value => typeof value === "string" && value.length > 0));
  }
});

test("discovery is visible for either method, jobs cancel on close and choosing needs no save", () => {
  const editor = read("../components/cameras/CameraEditor.js"), dialog = read("../components/cameras/CameraDiscoveryDialog.js");
  const head = editor.slice(editor.indexOf("function renderConnection()"), editor.indexOf("function renderVideo()"));
  assert.match(head, /cameraNetworkSearchButton/);
  assert.doesNotMatch(head, /protocol === "onvif" \? <button/);
  assert.match(dialog, /keepalive: true/);
  assert.match(dialog, /selected_protocol: value/);
  assert.match(dialog, /checked_hosts/);
  assert.doesNotMatch(dialog, /setInterval/);
});

test("storage rule action stays in Storage and persists one batch", () => {
  const page = read("../app/storage/page.js"), editor = read("../components/storage/ArchiveRulesDialog.js");
  const action = page.slice(page.indexOf('id: "retention"'), page.indexOf('id: "auto-free"'));
  assert.doesNotMatch(action, /href="\/cameras"/);
  assert.match(action, /setArchiveRulesOpen\(true\)/);
  assert.match(editor, /JSON.stringify\(\{ changes \}\)/);
  assert.match(editor, /selected.includes/);
});

test("two equal clip icon options are only in Recordings, top information order is stable", () => {
  const recordings = read("../app/recordings/page.js"), timeline = read("../app/chronology/page.js"), nav = read("../components/Layout.js");
  assert.match(recordings, /<ScissorsIcon \/>/); assert.match(recordings, /<FilmstripClipIcon \/>/);
  assert.doesNotMatch(timeline, /FilmstripClipIcon/);
  const controls = nav.slice(nav.indexOf('<div className="topNavRight">'));
  assert(controls.indexOf("<CameraEventsHub") < controls.indexOf("<SystemHealthIndicator"));
  assert(controls.indexOf("<SystemHealthIndicator") < controls.indexOf('href="/storage"'));
  assert(controls.indexOf('href="/storage"') < controls.indexOf('href="/settings"'));
});

test("host worker is IPC-only and carries no archive, Docker socket or idle health polling", () => {
  const compose = read("../../../docker-compose.yml");
  const worker = compose.slice(compose.indexOf("  camera-discovery:"), compose.indexOf("  api:"));
  assert.match(worker, /network_mode: host/);
  assert.match(worker, /cap_drop: \[ALL\]/);
  assert.match(worker, /read_only: true/);
  assert.doesNotMatch(worker, /ports:|docker\.sock|healthcheck:|storage\/archive|DATABASE_URL/);
});
