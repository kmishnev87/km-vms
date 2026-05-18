import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(__dirname, "..");
const read = (relativePath) => fs.readFileSync(path.join(root, relativePath), "utf8");

const recordsPage = read("app/recordings/page.js");
const recordsCss = read("app/styles/40-storage-records-shared.css");
const recordingsRouter = read("../api/app/routers/recordings.py");

assert.match(recordsPage, /const PAGE_SIZE_OPTIONS = \[15, 30, 50, 100\]/);
assert.match(recordsPage, /const \[pageSize, setPageSize\] = useState\(DEFAULT_PAGE_SIZE\)/);
assert.match(recordsPage, /const \[exportCameraOptions, setExportCameraOptions\] = useState\(\[\]\)/);
assert.match(recordsPage, /params\.set\("limit", String\(pageSize\)\)/);
assert.match(recordsPage, /params\.set\("offset", String\(Math\.max\(0, \(page - 1\) \* pageSize\)\)\)/);
assert.match(recordsPage, /setRecordingsLoadState\("loading"\)/);
assert.match(recordsPage, /setItems\(\[\]\)/);
assert.match(recordsPage, /const recordingsLoaded = recordingsLoadState === "loaded"/);
assert.match(recordsPage, /const filteredItems = recordingsLoaded \? items : \[\]/);
assert.doesNotMatch(recordsPage, /recordingsLoadState === "loaded" \|\| recordingsLoadState === "refreshing"/);
assert.doesNotMatch(recordsPage, /Проверяется|not_checked/);

assert.match(recordsPage, /function isRecordingAvailable\(item\)/);
assert.match(recordsPage, /item\?\.availability_status === "available"/);
assert.match(recordsPage, /item\?\.available === true/);
assert.match(recordsPage, /recordingAvailabilityLabel\(item, t\)/);
assert.match(recordsPage, /<select value=\{pageSize\} onChange=\{handlePageSizeChange\}>/);
assert.match(recordsPage, /PAGE_SIZE_OPTIONS\.map\(\(size\) =>/);
assert.match(recordsPage, /<form className="recordingsPageJump" onSubmit=\{handlePageJump\}>/);
assert.match(recordsPage, /Math\.min\(Math\.max\(requested, 1\), pageCount\)/);
assert.match(recordsPage, /setExportCameraOptions\(data\.export_items \|\| \[\]\)/);
assert.match(recordsPage, /const clipCameraOptions = useMemo\(\(\) => \{\s*return exportCameraOptions;\s*\}, \[exportCameraOptions\]\);/);
assert.doesNotMatch(
  recordsPage.slice(recordsPage.indexOf("const clipCameraOptions")),
  /items\.forEach|selectedCamera !== "__all__"/
);
assert.match(recordsPage, /cameraId: ""/);
assert.doesNotMatch(
  recordsPage.slice(recordsPage.indexOf("function openExportModal"), recordsPage.indexOf("function closeExportModal")),
  /clipCameraOptions\[0\]|selectedCamera === "__all__"/
);

assert.match(recordsCss, /\.recordingsPageSizeControl/);
assert.match(recordsCss, /\.recordingsPageJump/);

assert.match(recordingsRouter, /verify_files=True/);
assert.match(recordingsRouter, /availability_status = "missing"/);
assert.match(recordingsRouter, /availability_status = "error"/);
assert.match(recordingsRouter, /"file_exists": file_exists/);
assert.match(recordingsRouter, /"export_items": collect_recording_camera_options\(db, export_query\)/);
assert.doesNotMatch(
  recordingsRouter.slice(recordingsRouter.indexOf("def list_recordings")),
  /verify_files=False/
);

console.log("Stage 13.5.2.1.1 records page-scoped verified load static contracts passed");
