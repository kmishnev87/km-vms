import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (file) => fs.readFileSync(resolve(root, file), "utf8");
const { ALL_RECORDING_CAMERAS, resolveEffectiveRecordingCamera } = await import(
  pathToFileURL(resolve(root, "lib/recordingFilters.js"))
);

assert.equal(ALL_RECORDING_CAMERAS, "__all__");
assert.equal(resolveEffectiveRecordingCamera("Archive-only", ["Other"]), "__all__");
assert.equal(resolveEffectiveRecordingCamera("Active camera", ["Active camera"]), "Active camera");
assert.equal(resolveEffectiveRecordingCamera("__all__", ["Camera"]), "__all__");

const recordings = read("app/recordings/page.js");
const storage = read("app/storage/page.js");
const css = read("app/styles/40-storage-records-shared.css");
const operations = read("../api/app/services/recording_operations.py");
const router = read("../api/app/routers/recordings.py");
const permissions = read("../api/app/core/endpoint_permissions.py");

assert.match(recordings, /const \[cameraOptions\] = await Promise\.all\(\[loadCameras\(\), loadRecorderStatus\(\)\]\)/);
assert.match(recordings, /resolveEffectiveRecordingCamera\(selectedCamera, cameraOptions\)/);
assert.match(recordings, /setSelectedCamera\(effectiveCamera\)[\s\S]*setCurrentPage\(1\)[\s\S]*loadRecordings\(effectiveCamera, selectedDate, effectivePage\)/);
assert.match(recordings, /readyPlanId:\s*plan\.plan_id/);
assert.match(recordings, /deletion-plans\/\$\{encodeURIComponent\(readyPlanId\)\}.*method:\s*"DELETE"/s);
assert.match(recordings, /dangerTriggerRef\.current\?\.focus\(\{ preventScroll: true \}\)/);
assert.match(recordings, /ref=\{dangerTriggerRef\}[\s\S]*recordingsDangerTrigger/);

assert.match(storage, /archiveRootCleanupCapabilityModel\(detail\)/);
assert.match(storage, /capability\.canRetryNow[\s\S]*archiveRootCleanupRetry/);
assert.match(storage, /capability\.shouldRefresh \|\| capability\.needsExternalFix[\s\S]*refresh-storage/);

assert.match(router, /@router\.delete\("\/deletion-plans\/\{plan_id\}"\)/);
assert.match(permissions, /"DELETE", "\/recordings\/deletion-plans\/\{plan_id\}"/);
assert.match(operations, /EXPIRED_READY_GRACE_SECONDS = 5 \* 60/);
assert.match(operations, /TERMINAL_RETENTION_SECONDS = 24 \* 60 \* 60/);
assert.match(operations, /STALE_RUNNING_RETENTION_SECONDS = 7 \* 24 \* 60 \* 60/);
assert.match(operations, /OPERATION_CLEANUP_SCAN_LIMIT = 256/);
assert.match(operations, /OPERATION_CLEANUP_DELETE_LIMIT = 128/);
assert.match(operations, /entry\.is_symlink\(\)/);
assert.match(operations, /entry\.is_file\(follow_symlinks=False\)/);

assert.match(css, /storageOpsSection-roots\s*\{[\s\S]*--storage-root-spacing:\s*10px/);
assert.match(css, /storageOpsSection-roots > \.storageOpsRootList\s*\{\s*margin-top:\s*var\(--storage-root-spacing\)/);
for (const column of ["40%", "20%"] ) assert.match(css, new RegExp(`width:\\s*${column.replace("%", "\\%")}`));
assert.match(css, /th:nth-child\(n \+ 2\),[\s\S]*td:nth-child\(n \+ 2\)[\s\S]*text-align:\s*center/);
assert.match(css, /storageOpsCameraTable\s*\{[\s\S]*overflow-x:\s*hidden/);

console.log("Stage 13.5.4.9.1 frontend safety contracts passed");
