import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const helper = fs.readFileSync(resolve(__dirname, "../lib/archiveExports.js"), "utf8");
const recordsPage = fs.readFileSync(resolve(__dirname, "../app/recordings/page.js"), "utf8");
const chronologyPage = fs.readFileSync(resolve(__dirname, "../app/chronology/page.js"), "utf8");
const globalsCss = fs.readFileSync(resolve(__dirname, "../app/globals.css"), "utf8");

assert.equal(helper.includes('EXPORT_PERMISSION = "export_recordings"'), true);
assert.equal(helper.includes("runArchiveExportWorkflow"), true);
assert.equal(helper.includes("/archive/exports"), true);
assert.equal(helper.includes("/generate"), true);
assert.equal(helper.includes("/manifest"), true);
assert.equal(helper.includes("/download"), true);
assert.equal(helper.includes("URL.revokeObjectURL"), true);
assert.equal(helper.includes("/volume"), true);
assert.equal(helper.includes("internal_"), true);

assert.equal(recordsPage.includes("canExportRecordings(currentUser)"), true);
assert.equal(recordsPage.includes("openExportModal(item)"), true);
assert.equal(recordsPage.includes("recordingsTableWrap"), true);
assert.equal(recordsPage.includes("recordingsViewToggle"), false);
assert.equal(recordsPage.includes("recordingsCardsGrid"), false);

assert.equal(chronologyPage.includes("canExportRecordings(currentUser)"), true);
assert.equal(chronologyPage.includes("openExportModal"), true);
assert.equal(chronologyPage.includes("selectedCameraIds"), true);
assert.equal(chronologyPage.includes("Chronology evidence"), true);

assert.equal(globalsCss.includes(".archiveExportForm"), true);
assert.equal(globalsCss.includes(".archiveExportStatus"), true);
