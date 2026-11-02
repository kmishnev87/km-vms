import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const read = (path) => fs.readFileSync(resolve(root, path), "utf8");
const scope = {};
vm.runInNewContext(read("lib/recordingFormatChange.js").replaceAll("export function ", "function ") + "\nthis.h={recordingFormatCopy,recordingFormatError,recordingFormatNotice,recordingFormatCameraText,shouldWatchRecordingFormat};", scope);
const h = scope.h;
const plain = (value) => JSON.parse(JSON.stringify(value));
for (const lang of ["ru", "en", "zh-CN"]) {
  assert.deepEqual(Object.keys(h.recordingFormatCopy(lang)), Object.keys(h.recordingFormatCopy("ru")));
  for (const text of Object.values(h.recordingFormatCopy(lang))) assert.ok(typeof text === "string" && text.length > 0);
  assert.equal(h.recordingFormatError({ code: "recording_format_change_requires_choice" }, lang).kind, "choice");
  assert.equal(h.recordingFormatError({ detail: { error: "recording_format_settings_changed" } }, lang).kind, "stale");
  assert.equal(h.recordingFormatError({ code: "recording_format_change_blocked_active_recordings" }, lang).kind, "choice");
  assert.ok(h.recordingFormatNotice({ state: "pending", target_format: "mp4" }, lang).message.includes("MP4"));
  assert.equal(h.recordingFormatNotice(null, lang).state, "idle");
  assert.ok(!h.recordingFormatCameraText({ state: "failed", reason_code: "internal_secret_rtsp://secret" }, lang).includes("secret"));
}
assert.equal(h.recordingFormatError({ code: "other_problem" }, "ru"), null);
assert.equal(h.shouldWatchRecordingFormat({ state: "pending", requested_at: "2026-11-02T12:00:00Z" }), true);
assert.equal(h.shouldWatchRecordingFormat({ state: "failed", requested_at: "2026-11-02T12:00:00Z" }), true);
assert.equal(h.shouldWatchRecordingFormat({ state: "applied", requested_at: "2026-11-02T12:00:00Z" }), false);
assert.equal(h.shouldWatchRecordingFormat({ state: "pending" }), false);

const page = read("app/settings/page.js");
const submit = page.slice(page.indexOf("  async function submitSettings("), page.indexOf("  async function rescanHardware("));
assert.ok(submit.includes("async function submitSettings"));
function harness(api) {
  const state = { calls: [], toasts: [], choice: null, saving: false };
  const context = {
    savingRef: { current: false }, formatStatusGeneration: { current: 0 },
    setSaving: (value) => { state.saving = value; },
    setFormatChoiceError: (value) => { state.error = value; },
    setDraft: (value) => { state.draft = value; },
    setSavedDraft: (value) => { state.saved = value; },
    setFormatStatus: (value) => { state.status = value; },
    setFormatChoice: (value) => { state.choice = value; },
    persistLocale: () => {}, settingsDraftFromApi: (value) => value,
    showToast: (value) => state.toasts.push(value),
    recordingFormatNotice: h.recordingFormatNotice, recordingFormatError: h.recordingFormatError,
    normalizedError: (error) => ({ title: "typed error", text: error.code }),
    lang: "ru", t: { toasts: { saveOkTitle: "Настройки сохранены", saveOkText: "Изменения применены" } },
    apiFetch: async (path, options) => { state.calls.push({ path, body: JSON.parse(options.body) }); return api(path, options); },
  };
  vm.runInNewContext(submit + "\nthis.submit=submitSettings;", context);
  return { state, run: context.submit };
}
const snapshot = { recording_format: "mp4", expected_recording_format: "mkv", language: "ru" };
const choice = harness(async () => { throw { code: "recording_format_change_requires_choice", status: 409 }; });
await choice.run(null, snapshot);
assert.deepEqual(plain(choice.state.choice.payload), snapshot);
assert.equal(choice.state.calls.length, 1);
assert.equal(choice.state.toasts.length, 0, "a normal choice is not a server-unavailable toast");
assert.equal(choice.state.saving, false);

for (const mode of ["after_segment", "immediate"]) {
  const flow = harness(async () => ({ recording_format: "mp4", recording_format_change: { state: "pending", target_format: "mp4" } }));
  await flow.run(mode, snapshot);
  assert.equal(flow.state.calls[0].body.recording_format_apply_mode, mode);
  assert.equal(flow.state.calls[0].body.expected_recording_format, "mkv");
  assert.equal(flow.state.toasts[0].variant, "info", "saved request does not pretend recording has already switched");
  assert.equal(flow.state.choice, null);
}

const stale = harness(async () => { throw { code: "recording_format_settings_changed", status: 409 }; });
await stale.run("immediate", snapshot);
assert.equal(stale.state.error, h.recordingFormatCopy("ru").stale);
assert.equal(stale.state.toasts.length, 0);

let release;
const duplicate = harness(() => new Promise((resolve) => { release = resolve; }));
const first = duplicate.run("immediate", snapshot);
await duplicate.run("immediate", snapshot);
assert.equal(duplicate.state.calls.length, 1, "double click cannot submit a second operation");
release({ recording_format_change: { state: "applied", target_format: "mp4", items: [] } });
await first;
assert.equal(duplicate.state.saving, false);

assert.ok(page.indexOf('message.includes("403")') < page.indexOf("const formatError = recordingFormatError(err, lang)"));
assert.ok(page.includes("formatStatusGeneration.current"));
assert.ok(page.includes("controller?.abort()"));
assert.ok(page.includes("[401, 403].includes(err?.status)"));
assert.ok(page.includes("document.hidden"));
assert.ok(page.includes('submitSettings("after_segment", formatChoice.payload)'));
assert.ok(page.includes('submitSettings("immediate", formatChoice.payload)'));
assert.ok(!page.includes('Сейчас используется MP4'), "draft selection must not claim that every camera already uses the new format");
assert.ok(!page.includes('Сейчас используется MKV'));
const css = read("app/styles/20-settings-maintenance.css");
assert.ok(css.includes(".settingsRecordingFormatStatus"));
assert.ok(css.includes("height: 140px"));
assert.ok(css.includes(".settingsRecordingFormatChoice .operationFeedbackBusy { display: none; }"));
console.log("Stage 13.7.5 format choice, snapshots, races, copy, state and stable layout PASS");
