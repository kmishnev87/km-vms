import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

const source = fs.readFileSync(new URL("../lib/cameraEditor.js", import.meta.url), "utf8")
  .replace(/^import .*;$/gm, "").replaceAll("export function ", "function ").replaceAll("export const ", "const ");
const context = {};
vm.runInNewContext(`${source}\nthis.m = {cameraEditorProblem, cameraEditorDraft, MAX_RETENTION_DAYS};`, context);
const { cameraEditorProblem, cameraEditorDraft, MAX_RETENTION_DAYS } = context.m;
const copy = { editorRequired: "required", editorNameTooLong: "name-too-long", editorRetentionRange: "retention-range", editorRecordingInvalid: "invalid-number" };
const draft = { ...cameraEditorDraft(), host: "camera.invalid", rtsp_main_url: "/main" };
for (const character of ["a", "К", "🎥"]) {
  assert.equal(cameraEditorProblem({...draft, name: character.repeat(255)}, copy), "");
  assert.equal(cameraEditorProblem({...draft, name: character.repeat(256)}, copy), "name-too-long");
}
assert.equal(cameraEditorProblem({...draft, name: "Camera", retention_days: MAX_RETENTION_DAYS}, copy), "");
assert.equal(cameraEditorProblem({...draft, name: "Camera", retention_days: MAX_RETENTION_DAYS + 1}, copy), "retention-range");
assert.equal(cameraEditorProblem({...draft, name: "Camera", retention_days: 0}, copy), "invalid-number");
console.log("Final camera policy UI bounds: PASS");
