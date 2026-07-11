import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const helper = fs.readFileSync(resolve(__dirname, "../lib/archiveExports.js"), "utf8");
const recordsPage = fs.readFileSync(resolve(__dirname, "../app/recordings/page.js"), "utf8");
const chronologyPage = fs.readFileSync(resolve(__dirname, "../app/chronology/page.js"), "utf8");
const removedRecordsEvidence = "recordings" + "Evidence";
const removedManifestJson = "manifest" + " JSON";
const removedRowModalCall = "openExportModal" + "(item)";
const removedSilentFirstCamera = "selectedCameraIds" + "[0]";
const removedBlobFirst = "response" + ".blob()";

function read(relative) {
  return fs.readFileSync(resolve(__dirname, "..", relative), "utf8");
}

function readEffectiveCss(relative) {
  const content = read(relative);
  const imports = [...content.matchAll(/@import\s+"\.\/([^"]+)";/g)];
  if (!imports.length) return content;
  return imports.map((match) => read(`app/${match[1]}`)).join("\n");
}

const globalsCss = readEffectiveCss("app/globals.css");

assert.equal(helper.includes('EXPORT_PERMISSION = "export_recordings"'), true);
assert.equal(helper.includes("getArchiveExportLimits"), true);
assert.equal(helper.includes("/archive/exports/limits"), true);
assert.equal(helper.includes("startChronologyCurrentRecordingDownload"), true);
assert.equal(helper.includes("/chronology/download-token?camera_id="), true);
assert.equal(helper.includes("/api/chronology/download?camera_id="), true);
assert.equal(helper.includes("ts="), true);
assert.equal(helper.includes("runArchiveExportWorkflow"), true);
assert.equal(helper.includes("/archive/exports"), true);
assert.equal(helper.includes("/generate"), true);
assert.equal(helper.includes("/manifest"), true);
assert.equal(helper.includes("/download"), true);
assert.equal(helper.includes("URL.revokeObjectURL"), true);
assert.equal(helper.includes("/volume"), true);
assert.equal(helper.includes("internal_"), true);
assert.equal(helper.includes("queued"), true);
assert.equal(helper.includes("archiveExportStatusMessage"), true);
assert.equal(helper.includes("validateArchiveExportSelection"), true);
assert.equal(helper.includes("normalizeChronologyDownloadError"), true);

assert.equal(recordsPage.includes("canExportRecordings(currentUser)"), true);
assert.equal(recordsPage.includes("openExportModal"), true);
assert.equal(recordsPage.includes("recordingsCreateClipButton"), true);
assert.equal(recordsPage.includes(`${removedRecordsEvidence}IconButton`), false);
assert.equal(recordsPage.includes("recordingsHelpTooltip"), false);
assert.equal(recordsPage.includes(`${removedRecordsEvidence}Col`), false);
assert.equal(recordsPage.includes(`${removedRecordsEvidence}Cell`), false);
assert.equal(recordsPage.includes(removedRowModalCall), false);
assert.equal(recordsPage.includes("createClipTooltip"), true);
assert.equal(recordsPage.includes("exportPickCamera"), true);
assert.equal(recordsPage.includes("exportManifest"), true);
assert.equal(recordsPage.includes(removedManifestJson), false);
assert.equal(recordsPage.includes("describeArchiveExportLimits(exportLimits)"), true);
assert.equal(recordsPage.includes("recordingsTableWrap"), true);
assert.equal(recordsPage.includes("recordingsViewToggle"), false);
assert.equal(recordsPage.includes("recordingsCardsGrid"), false);
assert.equal(recordsPage.indexOf("recordingsFilterDate") < recordsPage.indexOf("recordingsCreateClipButton"), true);
assert.equal(recordsPage.includes('colSpan="7"'), true);

assert.equal(chronologyPage.includes("canExportRecordings(currentUser)"), true);
assert.equal(chronologyPage.includes("openExportModal"), true);
assert.equal(chronologyPage.includes("selectedCameraIds"), true);
assert.equal(chronologyPage.includes("startChronologyCurrentRecordingDownload(cameraId, timestamp)"), true);
assert.equal(chronologyPage.includes("formatProductTimestampParam(timelineTs || currentTs"), true);
assert.equal(chronologyPage.includes("quickDownload"), true);
assert.equal(chronologyPage.includes("exportEvidence"), true);
assert.equal(chronologyPage.includes("exportManifest"), true);
assert.equal(chronologyPage.includes(removedManifestJson), false);
assert.equal(chronologyPage.includes(removedSilentFirstCamera), false);
assert.equal(chronologyPage.includes("chronologyDownloadChooser"), true);
assert.equal(chronologyPage.includes("allCameras"), true);
assert.equal(chronologyPage.includes("startQuickDownloadForAllCameras"), true);
assert.equal(chronologyPage.includes("exportPickCamera"), true);
assert.equal(chronologyPage.includes("quickDownloadHelp"), true);
assert.equal(chronologyPage.includes("exportEvidenceHelpShort"), true);
assert.equal(chronologyPage.includes("downloadChooserRef"), true);
assert.equal(chronologyPage.includes('event.key === "Escape"'), true);
assert.equal(chronologyPage.includes("setDownloadChooserOpen(false);"), true);

assert.equal(globalsCss.includes(".archiveExportForm"), true);
assert.equal(globalsCss.includes(".archiveExportHelp"), false);
assert.equal(globalsCss.includes(".archiveExportLimits"), false);
assert.equal(globalsCss.includes(".archiveExportStatus"), true);
assert.equal(globalsCss.includes(".chronologyDownloadChooser"), true);
assert.equal(globalsCss.includes(".chronologyHelpTooltip"), true);
assert.equal(globalsCss.includes(".recordingsCreateClipButton"), true);
assert.equal(globalsCss.includes(`.${removedRecordsEvidence}IconButton`), false);
assert.equal(recordsPage.includes("archiveExportHelp"), false);
assert.equal(chronologyPage.includes("archiveExportHelp"), false);
assert.equal(recordsPage.includes("archiveExportLimits"), false);
assert.equal(chronologyPage.includes("archiveExportLimits"), false);

const visibleSources = [helper, recordsPage, chronologyPage].join("\n");
for (const forbidden of ["source_missing", "checksum_mismatch", "manifest_not_ready", "invalid_job_status"]) {
  assert.equal(recordsPage.includes(forbidden), false);
  assert.equal(chronologyPage.includes(forbidden), false);
}
assert.equal(visibleSources.includes("Evidence export"), false);
assert.equal(visibleSources.includes("\u0414\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u044d\u043a\u0441\u043f\u043e\u0440\u0442"), false);
assert.equal(helper.includes(removedBlobFirst), false);
assert.equal(helper.includes("createObjectURL(blob)"), true);
assert.equal(helper.includes("3 * 60 * 60"), true);
assert.equal(helper.includes("1800"), false);
