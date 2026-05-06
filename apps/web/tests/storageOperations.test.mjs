import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = fs
  .readFileSync(resolve(__dirname, "../lib/storageOperations.js"), "utf8")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${source}\nthis.formatBytes = formatBytes;\nthis.formatPercent = formatPercent;\nthis.statusLabel = statusLabel;\nthis.lowDiskPolicyText = lowDiskPolicyText;\nthis.cameraStorageRows = cameraStorageRows;`,
  context
);

assert.equal(context.formatBytes(0), "0 B");
assert.equal(context.formatBytes(1536), "1.5 KB");
assert.equal(context.formatPercent(9.456), "9.46%");
assert.equal(context.statusLabel("critical"), "Критично");

const offText = context.lowDiskPolicyText({
  auto_free_space_cleanup_enabled: false,
  warning_threshold_percent: 10,
  cleanup_threshold_percent: 5,
  critical_threshold_percent: 1,
});
assert.equal(offText.includes("только предупреждает"), true);
assert.equal(offText.includes("Автоосвобождение выключено"), true);
assert.equal(offText.includes("не разрешает удаление без opt-in"), true);

const onText = context.lowDiskPolicyText({
  auto_free_space_cleanup_enabled: true,
  warning_threshold_percent: 10,
  cleanup_threshold_percent: 5,
  critical_threshold_percent: 1,
});
assert.equal(onText.includes("owned metadata-safe"), true);
assert.equal(onText.includes("приостановлена"), true);

const rows = context.cameraStorageRows([
  { camera_id: 1, camera_name: "A", size_bytes: 100, segment_count: 1 },
  { camera_id: 2, camera_name: "Long camera name", size_bytes: 500, segment_count: 2 },
]);
assert.equal(rows[0].camera_id, 2);
assert.equal(JSON.stringify(rows).includes("rtsp://"), false);
assert.equal(JSON.stringify(rows).includes("Authorization"), false);
