import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(here, "..", file), "utf8");
const modelSource = [read("lib/cameraStage16.js"), read("lib/cameraEditor.js"), read("lib/cameraOnvifConfig.js")].join("\n")
  .replace(/^import .*;$/gm, "").replaceAll("export function ", "function ").replaceAll("export const ", "const ");
const context = { URL, Date, Set, console };
vm.runInNewContext(`${modelSource}\nthis.model = { cameraEditorDraft, patchCameraEditor, cameraEditorPayload, cameraEditorChanges, cameraConnectionChanged, cameraSavePayload, cameraManagementKey, cameraStreamKey, cameraCheckIsCurrent, cameraChecksReady, cameraStreamRoles, cameraDraftWithProfiles, assignCameraProfile, cameraEditorProblem, cameraEditorError, cameraDraftForSource, cameraSelectedSource, cameraSourceConflict, cameraAssignedProfile, assignCameraPurposeProfile, cameraPreviewIdentity, cameraProfileHasMetadata };`, context);
const m = context.model;
const plain = (value) => JSON.parse(JSON.stringify(value));
let form = m.cameraEditorDraft();
assert.equal(form.protocol, "onvif");
assert.equal(form.port, 80);
assert.equal(form.rtsp_port, 554);
form = m.patchCameraEditor(form, "host", "camera.example.test");
assert.equal(form.rtsp_host, "camera.example.test");
form = m.patchCameraEditor(form, "rtsp_host", "video.example.test");
form = m.patchCameraEditor(form, "host", "new.example.test");
assert.equal(form.rtsp_host, "video.example.test", "explicit NAT endpoint must survive management address edits");
const switched = m.patchCameraEditor({ ...form, protocol: "rtsp", port: 8554 }, "protocol", "onvif");
assert.equal(switched.port, 80);
assert.equal(switched.rtsp_port, 8554);
assert.equal(switched.onvif_profile_token, "");

const profiles = { profiles: [{ token: "high", name: "High", stream_path: "/high" }, { token: "low", name: "Low", stream_path: "/low" }], suggested_main_profile_token: "high", suggested_sub_profile_token: "low" };
const recommended = m.cameraDraftWithProfiles(form, profiles);
assert.equal(recommended.onvif_profile_token, "high");
assert.equal(recommended.onvif_sub_profile_token, "low");
assert.equal(recommended.default_live_stream, "sub");
const manual = { ...recommended, rtsp_main_url: "/manual", onvif_profile_token: "manual", rtsp_sub_url: "", onvif_sub_profile_token: "" };
assert.deepEqual(plain(m.cameraDraftWithProfiles(manual, profiles)), plain(manual), "rechecking may not replace choices or reintroduce deliberately removed Sub");
const single = m.cameraDraftWithProfiles(form, { profiles: profiles.profiles.slice(0, 1), suggested_main_profile_token: "high" });
assert.deepEqual(plain(m.cameraStreamRoles(single)), ["main"]);
assert.equal(single.default_live_stream, "main");
assert.equal(m.assignCameraProfile(recommended, "sub", null).default_live_stream, "main");

const dual = {
  profiles: [
    { token: "one-main", source_id: "channel:1", stream_path: "/channel1/main" },
    { token: "one-sub", source_id: "channel:1", stream_path: "/channel1/sub" },
    { token: "two-main", source_id: "channel:2", stream_path: "/channel2/main" },
    { token: "two-sub", source_id: "channel:2", stream_path: "/channel2/sub" },
  ],
  source_groups: [
    { id: "channel:1", channel: 1, main_profile_token: "one-main", sub_profile_token: "one-sub", profile_tokens: ["one-main", "one-sub"] },
    { id: "channel:2", channel: 2, main_profile_token: "two-main", sub_profile_token: "two-sub", profile_tokens: ["two-main", "two-sub"] },
  ],
};
const firstLens = m.cameraDraftWithProfiles(form, dual);
assert.equal(m.cameraSelectedSource(firstLens, dual).id, "channel:1");
const secondLens = m.cameraDraftForSource(firstLens, dual, "channel:2");
assert.equal(secondLens.onvif_channel_id, "2");
assert.equal(secondLens.onvif_profile_token, "two-main");
assert.equal(secondLens.onvif_sub_profile_token, "two-sub");
assert.equal(m.cameraSourceConflict(secondLens, dual), false);
assert.equal(m.cameraSourceConflict({ ...secondLens, onvif_sub_profile_token: "one-sub" }, dual), true);
const legacySub = { ...firstLens, onvif_sub_profile_token: "" };
assert.equal(m.cameraAssignedProfile(legacySub, dual, "sub").token, "one-sub", "saved Sub path must not appear as 'No sub stream' when the old token is null");
let purposeDraft = m.assignCameraPurposeProfile(firstLens, dual, "live", "one-main");
assert.equal(purposeDraft.default_live_stream, purposeDraft.default_record_stream, "one real stream may serve both purposes");
assert.equal(m.cameraStreamRoles(purposeDraft).length, 1, "unused roles must not block a single-stream save");
purposeDraft = m.assignCameraPurposeProfile(purposeDraft, dual, "live", "one-sub");
assert.equal(m.cameraAssignedProfile(purposeDraft, dual, purposeDraft.default_live_stream).token, "one-sub");
assert.equal(m.cameraAssignedProfile(purposeDraft, dual, purposeDraft.default_record_stream).token, "one-main", "changing Live cannot overwrite recording");
assert.equal(m.cameraPreviewIdentity(firstLens, dual), m.cameraPreviewIdentity(purposeDraft, dual));
assert.notEqual(m.cameraPreviewIdentity(firstLens, dual), m.cameraPreviewIdentity(secondLens, dual));

const stored = { id: 8, name: "Saved", host: "camera.example.test", port: 8080, protocol: "onvif", rtsp_reachable_host: "video.example.test", rtsp_reachable_port: 8554, rtsp_main_url: "/main?channel=1", rtsp_sub_url: "/sub", onvif_profile_token: "p1", onvif_sub_profile_token: "p2", username: "operator", segment_minutes: 23, retention_days: 45, storage_quota_gb: 1234, enabled: false };
const baseline = m.cameraEditorDraft(stored);
assert.equal(baseline.password, "");
assert.equal(baseline.rtsp_host, "video.example.test");
assert.equal(baseline.retention_days, 45);
assert.equal(m.cameraConnectionChanged(baseline, baseline, stored.id), false);
const edited = m.patchCameraEditor(baseline, "retention_days", "46");
assert.deepEqual(plain(m.cameraSavePayload(edited, baseline, stored.id)), { retention_days: 46 });
assert.equal(m.cameraConnectionChanged(edited, baseline, stored.id), false, "offline policy edits must not require a probe");
assert.deepEqual(plain(m.cameraEditorChanges({ ...baseline, port: "8080" }, baseline)), {});
const changedPassword = m.patchCameraEditor(baseline, "password", "synthetic-password");
assert.equal(m.cameraConnectionChanged(changedPassword, baseline, stored.id), true);
assert.equal(m.cameraSavePayload(changedPassword, baseline, stored.id).password, "synthetic-password");

const now = Date.now();
const checks = Object.fromEntries(["main", "sub"].map((role) => [role, { key: m.cameraStreamKey(baseline, role), token: `proof-${role}`, at: now }]));
checks.management = { key: m.cameraManagementKey(baseline), token: "proof-management", at: now };
assert.equal(m.cameraChecksReady(baseline, checks, now), true);
for (const key of ["host", "port", "password", "username", "rtsp_host", "rtsp_port", "onvif_path", "rtsp_transport", "onvif_profile_token", "rtsp_sub_url"]) {
  const changed = m.patchCameraEditor(baseline, key, key.endsWith("port") ? 3333 : "changed-value");
  assert.equal(m.cameraChecksReady(changed, checks, now), false, `${key} must invalidate the applicable check`);
}
assert.equal(m.cameraChecksReady(edited, checks, now), true, "retention changes cannot invalidate a connection check");
assert.equal(m.cameraChecksReady(baseline, checks, now + 15 * 60 * 1000), false);
assert.equal(m.cameraChecksReady(baseline, { ...checks, sub: null }, now), false);
assert.equal(m.cameraCheckIsCurrent(null, "anything"), false);
assert.equal(m.cameraEditorProblem({ ...baseline, storage_quota_gb: 0 }, { editorRecordingInvalid: "invalid" }), "invalid");
assert.equal(m.cameraEditorError({ code: "wrong_credentials" }, { authError: "auth" }), "auth");

// Exercise the actual owner hook with deterministic HTTP replies and lifecycle cleanup.
function hookHarness(camera = null) {
  const slots = [], cleanups = [], requests = [];
  let index = 0, saved = null;
  const scope = {
    ...context, ...m, Date, URL, Set,
    useRef(initial) { const at = index++; return slots[at] ||= { current: initial }; },
    useState(initial) { const at = index++; if (!(at in slots)) slots[at] = initial; return [slots[at], (next) => { slots[at] = typeof next === "function" ? next(slots[at]) : next; }]; },
    useEffect(effect) { const at = index++; if (!(at in slots)) { slots[at] = true; cleanups.push(effect()); } },
    async apiFetch(path, options) { const request = { path, method: options.method, body: JSON.parse(options.body) }; requests.push(request); return await scope.reply(request); },
    reply: async () => { throw new Error("unexpected HTTP request"); },
  };
  const hook = read("components/cameras/useCameraEditor.js").replace(/^import[\s\S]*?from "[^"]+";\s*/gm, "").replace("export default function", "function");
  vm.runInNewContext(`${modelSource}\n${hook}\nthis.renderHook = useCameraEditor;`, scope);
  const copy = new Proxy({}, { get: (_, key) => key });
  return { scope, requests, render: () => { index = 0; return scope.renderHook(camera, copy, (value) => { saved = value; }); }, getSaved: () => saved, unmount: () => cleanups.forEach((cleanup) => cleanup?.()) };
}
const offline = hookHarness(stored);
let editor = offline.render();
editor.patch("retention_days", 46);
offline.scope.reply = async () => ({ id: stored.id });
await editor.save();
assert.equal(offline.requests.length, 1);
assert.equal(offline.requests[0].method, "PUT");
assert.deepEqual(plain(offline.requests[0].body), { retention_days: 46 });
assert.equal(offline.getSaved().id, stored.id);

function fillNew(harness) {
  const owner = harness.render();
  owner.patch("name", "Synthetic camera"); owner.patch("host", "camera.example.test");
  owner.patch("username", "synthetic"); owner.patch("password", "synthetic-password");
  return owner;
}
const late = hookHarness();
editor = fillNew(late);
let resolveProbe;
late.scope.reply = () => new Promise((resolve) => { resolveProbe = resolve; });
const pending = editor.check();
editor.patch("host", "other.example.test");
resolveProbe({ ok: true, onvif_probe_token: "stale-proof", ...profiles });
assert.equal(await pending, false);
assert.equal(late.render().device, null);
assert.equal(late.render().form.onvif_probe_token, null);
assert.equal(late.render().operation, "");
assert.equal(late.requests.length, 1, "stale ONVIF reply must not trigger RTSP to the previous camera");

const closed = hookHarness();
editor = fillNew(closed);
let resolveClosed;
closed.scope.reply = () => new Promise((resolve) => { resolveClosed = resolve; });
const closedPending = editor.check();
closed.unmount();
resolveClosed({ ok: true, onvif_probe_token: "closed-proof", ...profiles });
assert.equal(await closedPending, false);
assert.equal(closed.requests.length, 1);

const success = hookHarness();
editor = fillNew(success);
success.scope.reply = async (request) => request.path.endsWith("probe")
  ? { ok: true, onvif_probe_token: "probe", ...profiles }
  : { ok: true, tested_role: request.body.stream_role, validation_token: `${request.body.stream_role}-proof`, video: { codec: "h264", width: 1920, height: 1080 } };
assert.equal(await editor.check(), true);
assert.equal(success.render().connectionReady, true);
await success.render().next();
assert.equal(success.render().section, "video");
await success.render().next();
assert.equal(success.render().section, "review");
assert.deepEqual(success.requests.map((request) => request.path), ["/cameras/onvif/probe", "/cameras/test", "/cameras/test"]);
assert.equal(success.requests[2].body.stream_role, "sub");
assert.equal(success.render().verified, true);
success.render().invalidateVideo();
assert.equal(success.render().verified, false);
assert.equal(success.render().form.main_validation_token, null);
assert.equal(success.render().form.onvif_probe_token, "probe");

const frames = hookHarness();
editor = fillNew(frames);
frames.scope.reply = async (request) => request.path.endsWith("probe")
  ? { ok: true, onvif_probe_token: "probe", ...profiles }
  : request.path.endsWith("cleanup") ? { removed: request.body.tokens.length }
  : { ok: true, tested_role: request.body.stream_role, validation_token: `${request.body.stream_role}-proof`, preview_token: `frame-${request.body.stream_role}`, preview_url: `/previews/camera-tests/frame-${request.body.stream_role}.jpg`, video: { codec: "h264" } };
assert.equal(await editor.check(), true);
assert.equal(await frames.render().check({ all: true }), true);
assert.equal(frames.render().preview.role, "main", "stream validation must not replace the identification image");
await frames.render().selectPurpose("live", "low");
assert.equal(frames.render().preview.role, "main");
assert.equal(frames.render().form.preview_token, "frame-main");
frames.unmount();
const discardedFrames = frames.requests.find((request) => request.path.endsWith("cleanup"));
assert.deepEqual(plain(discardedFrames.body.tokens), ["frame-main", "frame-sub"]);

const lateFrame = hookHarness();
editor = fillNew(lateFrame);
let resolveStream;
lateFrame.scope.reply = async (request) => request.path.endsWith("probe")
  ? { ok: true, onvif_probe_token: "probe", ...profiles }
  : request.path.endsWith("cleanup") ? { removed: 1 } : await new Promise((resolve) => { resolveStream = resolve; });
const lateStreamPending = editor.check();
for (let tick = 0; tick < 20 && !resolveStream; tick += 1) await Promise.resolve();
assert.equal(typeof resolveStream, "function");
editor.patch("host", "changed.example.test");
resolveStream({ ok: true, tested_role: "main", validation_token: "late", preview_token: "abandoned-frame", video: { codec: "h264" } });
assert.equal(await lateStreamPending, false);
assert.equal(lateFrame.render().preview, null);
assert(lateFrame.requests.some((request) => request.path.endsWith("cleanup") && request.body.tokens.includes("abandoned-frame")), "a late reply must discard its temporary frame instead of leaking it");

const switching = hookHarness();
editor = fillNew(switching);
let finishSecondLens;
switching.scope.reply = async (request) => request.path.endsWith("probe")
  ? { ok: true, onvif_probe_token: "probe", ...dual }
  : request.path.endsWith("cleanup") ? { removed: request.body.tokens.length }
  : request.body.onvif_profile_token === "two-main"
    ? await new Promise((resolve) => { finishSecondLens = resolve; })
    : { ok: true, tested_role: request.body.stream_role, validation_token: "lens1-proof", preview_token: "lens1-image", preview_url: "/lens1.jpg", video: { codec: "h264", width: 1920, height: 1080 } };
assert.equal(await editor.check(), true);
const firstImage = switching.render().preview;
const toSecond = switching.render().selectSource("channel:2");
for (let tick = 0; tick < 20 && !finishSecondLens; tick += 1) await Promise.resolve();
assert.equal(typeof finishSecondLens, "function", "selecting a lens must automatically load its image");
const backToFirst = switching.render().selectSource("channel:1");
assert.equal(switching.render().preview.preview_url, firstImage.preview_url, "returning to a known lens must immediately restore its image");
finishSecondLens({ ok: true, tested_role: "main", validation_token: "lens2-late", preview_token: "lens2-late-image", preview_url: "/lens2.jpg", video: { codec: "h264" } });
assert.equal(await toSecond, false);
assert.equal(await backToFirst, true);
assert.equal(switching.render().preview.preview_url, "/lens1.jpg", "a late response must not replace the selected lens");
assert.equal(switching.requests.filter((request) => request.path.endsWith("probe")).length, 1);
assert.equal(switching.requests.filter((request) => request.path === "/cameras/test").length, 2, "return uses a valid cached check, not another RTSP read");
assert(switching.requests.some((request) => request.path.endsWith("cleanup") && request.body.tokens.includes("lens2-late-image")));
switching.scope.reply = async (request) => request.path.endsWith("cleanup") ? { removed: request.body.tokens.length }
  : { ok: true, tested_role: "main", validation_token: "lens2-proof", preview_token: "lens2-image", preview_url: "/lens2.jpg", video: { codec: "h264" } };
assert.equal(await switching.render().selectSource("channel:2"), true);
assert.equal(await switching.render().selectSource("channel:1"), true);
const cachedTwo = switching.render().selectSource("channel:2");
const cachedOne = switching.render().selectSource("channel:1");
assert.equal(await cachedTwo, false);
assert.equal(await cachedOne, true);
assert.equal(switching.render().preview.preview_url, "/lens1.jpg");
assert.equal(switching.requests.some((request) => request.path.endsWith("cleanup") && request.body.tokens.includes("lens2-image")), false, "a superseded cached check must not delete an existing cached image");
switching.unmount();

const failedSub = hookHarness();
editor = fillNew(failedSub);
failedSub.scope.reply = async (request) => request.path.endsWith("probe")
  ? { ok: true, onvif_probe_token: "probe", ...profiles }
  : request.body.stream_role === "sub" ? Promise.reject({ status: 400, code: "timeout" })
  : { ok: true, tested_role: "main", validation_token: "main-proof", preview_token: "stable-image", preview_url: "/stable.jpg", video: { codec: "h264" } };
assert.equal(await editor.check(), true);
assert.equal(await failedSub.render().selectPurpose("live", "low"), false);
assert.equal(failedSub.render().preview.preview_url, "/stable.jpg", "failed Sub validation must not erase the identification image");
assert.equal(failedSub.render().streamErrors.sub.message, "editorUnreachable");
assert.equal(failedSub.render().verified, false);

// Enrich every advertised profile, not just the two selected for use. No arbitrary
// ten-stream cap, JPEGs, save proofs, selection changes, or retry/polling loop.
const catalog = hookHarness();
editor = fillNew(catalog);
const catalogProfiles = Array.from({ length: 10 }, (_, index) => ({ token: `stream-${index}`, stream_path: `/stream/${index}` }));
const catalogData = { profiles: catalogProfiles, suggested_main_profile_token: "stream-0", suggested_sub_profile_token: "stream-1" };
let catalogInFlight = 0, catalogMaximum = 0;
catalog.scope.reply = async (request) => {
  if (request.path.endsWith("probe")) return { ok: true, onvif_probe_token: "catalog-proof", ...catalogData };
  if (request.body.metadata_only) {
    catalogInFlight += 1; catalogMaximum = Math.max(catalogMaximum, catalogInFlight);
    await Promise.resolve(); catalogInFlight -= 1;
    if (request.body.onvif_profile_token === "stream-4") throw new Error("timeout");
    return { ok: true, metadata_only: true, video: { codec: "hevc", width: 2048, height: 928, fps: "25/1" } };
  }
  return { ok: true, tested_role: request.body.stream_role, validation_token: "selected-proof", preview_token: "identification", preview_url: "/identification.jpg", video: { codec: "hevc", width: 4096, height: 1860, fps: "25/1" } };
};
await editor.check();
const selectedBeforeCatalog = plain(catalog.render().form);
catalog.render().requestProfileMetadata();
for (let tick = 0; tick < 200; tick += 1) await Promise.resolve();
assert.equal(catalog.requests.filter((request) => request.body.metadata_only).length, 9, "all remaining nine streams are read automatically");
assert.equal(catalogMaximum, 1, "catalog requests are serialized");
assert.deepEqual(plain(catalog.render().form), selectedBeforeCatalog, "metadata cannot assign profiles or save-gate proofs");
assert.equal(catalog.render().preview.preview_url, "/identification.jpg");
assert.equal(catalog.render().device.profiles.find((profile) => profile.token === "stream-4").metadata_status.state, "failed");
assert.equal(catalog.render().device.profiles.filter((profile) => profile.rtsp_probe).length, 9);
catalog.render().requestProfileMetadata();
for (let tick = 0; tick < 20; tick += 1) await Promise.resolve();
assert.equal(catalog.requests.filter((request) => request.body.metadata_only).length, 9, "failed metadata is not retried in a background loop");
catalog.unmount();

const closedCatalog = hookHarness();
editor = fillNew(closedCatalog);
let finishCatalog;
closedCatalog.scope.reply = async (request) => request.path.endsWith("probe") ? { ok: true, onvif_probe_token: "probe", ...catalogData }
  : request.body.metadata_only ? await new Promise((resolve) => { finishCatalog = resolve; })
  : request.path.endsWith("cleanup") ? { removed: 1 }
  : { ok: true, tested_role: "main", validation_token: "main-proof", video: { codec: "h264" } };
await editor.check();
closedCatalog.render().requestProfileMetadata();
assert.equal(typeof finishCatalog, "function");
closedCatalog.unmount();
const beforeClosed = closedCatalog.requests.length;
finishCatalog({ ok: true, video: { codec: "h264", width: 640, height: 360 } });
for (let tick = 0; tick < 20; tick += 1) await Promise.resolve();
assert.equal(closedCatalog.requests.length, beforeClosed, "closing stops the queue after its single bounded request");

const preemptCatalog = hookHarness();
editor = fillNew(preemptCatalog);
let finishMetadata;
preemptCatalog.scope.reply = async (request) => request.path.endsWith("probe") ? { ok: true, onvif_probe_token: "probe", ...catalogData }
  : request.body.metadata_only ? await new Promise((resolve) => { finishMetadata = resolve; })
  : { ok: true, tested_role: request.body.stream_role, validation_token: "foreground-proof", video: { codec: "h264", width: 1920, height: 1080 } };
await editor.check();
preemptCatalog.render().requestProfileMetadata();
assert.equal(typeof finishMetadata, "function");
const beforePreempt = preemptCatalog.requests.length;
const foreground = preemptCatalog.render().selectPurpose("live", "stream-5");
assert.equal(preemptCatalog.requests.length, beforePreempt, "foreground waits for the single active read instead of opening a concurrent RTSP probe");
finishMetadata({ ok: true, metadata_only: true, video: { codec: "hevc", width: 640, height: 360 } });
assert.equal(await foreground, true);
assert.equal(preemptCatalog.render().form.onvif_sub_profile_token, "stream-5");
preemptCatalog.unmount();
finishMetadata({ ok: true, metadata_only: true, video: { codec: "hevc" } });
for (let tick = 0; tick < 20; tick += 1) await Promise.resolve();

const changedCatalog = hookHarness();
editor = fillNew(changedCatalog);
let finishOldEndpoint;
changedCatalog.scope.reply = async (request) => request.path.endsWith("probe") ? { ok: true, onvif_probe_token: "probe", ...catalogData }
  : request.body.metadata_only ? await new Promise((resolve) => { finishOldEndpoint = resolve; })
  : { ok: true, tested_role: "main", validation_token: "proof", video: { codec: "h264" } };
await editor.check();
changedCatalog.render().requestProfileMetadata();
changedCatalog.render().patch("host", "changed.example.test");
const beforeChanged = changedCatalog.requests.length;
finishOldEndpoint({ ok: true, video: { codec: "hevc", width: 640, height: 360 } });
for (let tick = 0; tick < 20; tick += 1) await Promise.resolve();
assert.equal(changedCatalog.render().device, null, "late metadata cannot populate a different endpoint");
assert.equal(changedCatalog.requests.length, beforeChanged);
changedCatalog.unmount();

for (const locale of ["ru", "en", "zhCN"]) {
  const dictionary = read(`lib/i18n/${locale}.js`);
  for (const key of read("components/cameras/CameraEditor.js").matchAll(/copy\.(editor\w+)/g)) assert(dictionary.includes(`${key[1]}:`), `${locale}: ${key[1]}`);
}
const component = read("components/cameras/CameraEditor.js");
assert(component.includes("AuthenticatedPreviewImage"));
assert(!component.includes("<img"));
assert(component.includes('role="dialog"'));
assert(component.includes("beforeunload"));
assert(!component.includes("cameraFlowPreviewChoices"));
assert(!component.includes("cameraFlowSourceContext"), "lens caption is shown once, next to its image");
assert(component.includes("cameraFlowStreamSelected"), "closed stream controls show wrapping name and metadata");
assert(!component.includes("scrollIntoView"), "status changes cannot force scrolling");
assert.equal((component.match(/\{renderSourceChoice\(\)\}/g) || []).length, 1);
assert(component.includes('renderPurposeChoice("record", copy.recordStream)'));
assert(component.includes('renderPurposeChoice("live", copy.liveStream)'));
const styles = read("app/styles/50-cameras-shared-modals.css");
assert(styles.includes("height: min(900px, calc(100dvh - 48px))"));
assert(styles.includes(".cameraFlowFeedback { flex: 0 0 50px"));
assert(styles.includes(".cameraFlowFooter > div > .button { width: auto; flex: 0 0 auto; white-space: nowrap; }"), "global mobile button width cannot squeeze Continue out of the footer");
assert(!read("components/cameras/useCameraEditor.js").includes("localStorage"));
assert(read("app/cameras/page.js").includes("function openDeleteModal(camera)"));
assert(read("app/cameras/page.js").includes("function closeDeleteModal()"));
console.log("Stage 13.7.2.3 camera editor model, lifecycle and workflow tests PASS");
