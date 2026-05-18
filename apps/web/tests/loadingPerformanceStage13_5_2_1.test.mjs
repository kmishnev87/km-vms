import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), "utf8");

const camerasPage = read("app/cameras/page.js");
const recordsPage = read("app/recordings/page.js");
const recorderDiagnostics = read("../api/app/services/recorder_diagnostics.py");
const settingsRouter = read("../api/app/routers/settings.py");
const recordingsRouter = read("../api/app/routers/recordings.py");

assert.match(camerasPage, /const cams = await apiFetch\("\/cameras"\)/);
assert.doesNotMatch(camerasPage, /Promise\.all\(\[\s*apiFetch\("\/cameras"\),\s*apiFetch\("\/storage\/status"\)/s);
assert.match(camerasPage, /async function loadSecondaryStatus\(\)/);
assert.match(camerasPage, /apiFetch\("\/system\/recorder\/summary"\)/);
assert.match(camerasPage, /setInterval\(loadSecondaryStatus, 5000\)/);

assert.match(recordsPage, /const DEFAULT_PAGE_SIZE = 30/);
assert.match(recordsPage, /params\.set\("limit", String\(pageSize\)\)/);
assert.match(recordsPage, /params\.set\("offset", String\(Math\.max\(0, \(page - 1\) \* pageSize\)\)\)/);
assert.match(recordsPage, /params\.set\("sort_by", sortBy\)/);
assert.match(recordsPage, /params\.set\("sort_dir", sortDir\)/);
assert.match(recordsPage, /apiFetch\("\/system\/recorder\/summary"\)/);
assert.doesNotMatch(recordsPage, /return \[\.\.\.filteredItems\]\.sort/);
assert.match(recordsPage, /recordingsSummary\?\.count/);

assert.match(recordingsRouter, /MAX_RECORDINGS_PAGE_SIZE = 100/);
assert.match(recordingsRouter, /SUPPORTED_RECORDINGS_PAGE_SIZES = \(15, 30, 50, 100\)/);
assert.match(recordingsRouter, /verify_files=True/);
assert.match(recordingsRouter, /"availability_status": availability_status/);
assert.match(recordingsRouter, /"pagination": \{/);
assert.match(recordingsRouter, /"total_count": summary\["count"\]/);

assert.match(settingsRouter, /@router\.get\("\/system\/recorder\/summary"\)/);
assert.match(recorderDiagnostics, /def build_recorder_summary\(db: Session\)/);
assert.doesNotMatch(
  recorderDiagnostics.slice(
    recorderDiagnostics.indexOf("def build_recorder_summary"),
    recorderDiagnostics.indexOf("def _system_runtime_from_status")
  ),
  /build_storage_monitoring_summary|retention_diagnostics/
);

console.log("Stage 13.5.2.1 loading performance static contracts passed");
