import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const camerasPage = read("app/cameras/page.js");
const recordsPage = read("app/recordings/page.js");
const livePage = read("app/live/page.js");
const chronologyPage = read("app/chronology/page.js");
const systemStatusPage = read("app/system-status/page.js");
const storagePage = read("app/storage/page.js");
const homePage = read("app/page.js");

assert.match(camerasPage, /const \[camerasLoadState, setCamerasLoadState\] = useState\("idle"\)/);
assert.match(camerasPage, /setCamerasLoadState\(\(prev\) => \(prev === "loaded" \|\| prev === "refreshing" \? "refreshing" : "loading"\)\)/);
assert.match(camerasPage, /const camerasLoaded = camerasLoadState === "loaded" \|\| camerasLoadState === "refreshing"/);
assert.match(camerasPage, /const camerasFirstLoading = camerasLoadState === "idle" \|\| camerasLoadState === "loading"/);
assert.match(camerasPage, /camerasFirstLoading \? \(\s*<div className="card">\{t\("common\.loading"\)\}<\/div>/s);
assert.match(camerasPage, /camerasLoadState === "error" \? \(\s*<div className="card">\{error\}<\/div>/s);
assert.match(camerasPage, /camerasLoaded && !cameras\.length \? \(\s*<div className="card">\{copy\.noCameras\}<\/div>/s);
assert.doesNotMatch(camerasPage, /\{\s*!cameras\.length \? \(\s*<div className="card">\{copy\.noCameras\}<\/div>/s);

assert.match(recordsPage, /loading: "\\u0417\\u0430\\u0433\\u0440\\u0443\\u0437\\u043a\\u0430\.\.\."/);
assert.match(recordsPage, /const \[recordingsLoadState, setRecordingsLoadState\] = useState\("idle"\)/);
assert.match(recordsPage, /setRecordingsLoadState\("loading"\)/);
assert.match(recordsPage, /try \{\s*const data = await apiFetch\(`\/recordings\$\{query\}`\)/s);
assert.match(recordsPage, /setRecordingsLoadState\("loaded"\)/);
assert.match(recordsPage, /setRecordingsLoadState\("error"\)/);
assert.match(recordsPage, /const recordingsLoaded = recordingsLoadState === "loaded"/);
assert.match(recordsPage, /const recordingsFirstLoading = recordingsLoadState === "idle" \|\| recordingsLoadState === "loading"/);
assert.match(recordsPage, /\(\) => recordingsLoaded && !filteredItems\.length && hasActiveRecordingJobs\(recorderStatus, selectedCamera\)/);
assert.match(recordsPage, /\{t\.totalFiles\}: \{recordingsLoaded \? visibleSummary\.count : t\.loading\}/);
assert.match(recordsPage, /\{t\.totalSize\}: \{recordingsLoaded \? visibleSummary\.size_human : t\.loading\}/);
assert.match(recordsPage, /\{t\.page\}: \{recordingsLoaded \? `\$\{currentPage\} \/ \$\{pageCount\}` : t\.loading\}/);
assert.match(recordsPage, /\{recordingsEmptyMessage\}/);
assert.doesNotMatch(recordsPage, /\{activeRecordingEmpty \? t\.recordingActiveEmpty : t\.noRecords\}/);

assert.match(systemStatusPage, /const summaryLoaded = Boolean\(summary\)/);
assert.match(systemStatusPage, /row\.severity === "unknown"\) return t\("systemStatus\.loading"\)/);
assert.match(systemStatusPage, /systemStatusScore \$\{summaryLoaded \? \(hasProblems \? "problem" : "ok"\) : "unknown"\}/);
assert.match(systemStatusPage, /!summaryLoaded \? \(\s*<article className="systemStatusIncident unknown">/s);
assert.match(storagePage, /loading \? \(\s*<div className="storageOpsState">\{copy\.loading\}<\/div>/s);

for (const source of [livePage, chronologyPage]) {
  assert.match(source, /const \[camerasLoaded, setCamerasLoaded\] = useState\(false\)/);
  assert.match(source, /if \(!camerasLoaded\) return tiles/);
  assert.match(source, /data-touch-add-path="double-tap-card"/);
}

const secondaryAudit = [
  ["live", "app/live/page.js", "audited"],
  ["chronology", "app/chronology/page.js", "audited"],
  ["storage", "app/storage/page.js", "audited"],
  ["system-status", "app/system-status/page.js", "fixed"],
  ["dashboard", "app/page.js", "preserved"],
];

for (const [, relative] of secondaryAudit) {
  assert.ok(fs.existsSync(resolve(root, relative)), `${relative} must exist for Stage 13.5.2 audit`);
}

assert.match(homePage, /if \(!ready\) \{\s*return \(\s*<Layout>/s);
