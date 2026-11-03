import assert from "node:assert/strict";
import fs from "node:fs";

const page = fs.readFileSync(new URL("../app/cameras/page.js", import.meta.url), "utf8");
const fn = page.slice(page.indexOf("function archiveCleanupMessage("), page.indexOf("\nfunction cameraEndpointLabel"));
const message = new Function("result", "copy", `${fn}; return archiveCleanupMessage(result, copy);`);
const copy = Object.fromEntries(["cameraDeleted", "archiveRetained", "archiveDeleteActiveJob", "archiveDeleteUnowned", "archiveCleanupPartial", "archiveDeleteStorageUnavailable", "archiveDeleteFileFailed"].map((key) => [key, key]));
const success = { status: "deleted", camera_removed: true, warnings: [], recordings: {
  ok: true, requested_count: 3, deleted_count: 3, skipped_count: 0, failed_count: 0,
  reason_counts: { camera_delete_with_files: 3 },
} };
assert.equal(message(success, copy), copy.cameraDeleted, "successful deletion reasons are not errors");
assert.equal(message({ ...success, recordings: { ...success.recordings, reason_counts: { already_absent: 3 } } }, copy), copy.cameraDeleted);
assert.equal(message({ ...success, warnings: ["archive_retained"] }, copy), copy.archiveRetained);
for (const [reason, expected] of [
  ["camera_recording_not_stopped", "archiveDeleteActiveJob"], ["unowned", "archiveDeleteUnowned"],
  ["storage_unavailable", "archiveDeleteStorageUnavailable"], ["delete_failed", "archiveDeleteFileFailed"],
]) {
  assert.equal(message({ status: "blocked", camera_removed: false, recordings: { ok: false, reason_counts: { [reason]: 1 } } }, copy), copy[expected]);
}
const confirmSource = page.match(/^  async function confirmDeleteCamera\([\s\S]*?^  }/m)?.[0];
assert.ok(confirmSource);
for (const result of [success, { ...success, status: "partial", camera_removed: false, recordings: { ...success.recordings, ok: false, deleted_count: 2, failed_count: 1 } }]) {
  const state = { busy: false, dialogs: [], toasts: [] };
  const confirm = new Function("apiFetch", "archiveCleanupMessage", "copy", "newCameraDeleteOperationId", "setBusy", "closeDeleteModal", "setDeleteResult", "setOperationToast", "load", "loadSecondaryStatus", "notifyCameraRuntimeChanged", "setCameraToDelete", "setDeleteFiles", "setDeleteModalOpen", `
    const cameraToDelete = {id: 88, name: "Test camera"}, busy = false, deleteFiles = true;
    ${confirmSource}; return confirmDeleteCamera;
  `)(async () => result, message, copy, () => "test-delete", (value) => { state.busy = value; }, () => {}, (value) => state.dialogs.push(value), (value) => state.toasts.push(value), async () => {}, async () => {}, () => {}, () => {}, () => {}, () => {});
  await confirm();
  assert.equal(state.busy, false);
  if (result.camera_removed) {
    assert.equal(state.dialogs.length, 0, "a fully successful real response must not open the incomplete-cleanup window");
    assert.equal(state.toasts[0].tone, "success");
  } else {
    assert.equal(state.dialogs.length, 1);
    assert.equal(state.dialogs[0].actions[0].id, "retry-camera-delete");
  }
}
console.log("Camera deletion success, blocked reasons and retry feedback PASS");
