import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const recordsPage = fs.readFileSync(resolve(__dirname, "../app/recordings/page.js"), "utf8");
const globalsCss = fs.readFileSync(resolve(__dirname, "../app/globals.css"), "utf8");
const camerasPage = fs.readFileSync(resolve(__dirname, "../app/cameras/page.js"), "utf8");

assert.equal(recordsPage.includes("recordingsTableWrap"), true);
assert.equal(recordsPage.includes("recordingsTable"), true);
assert.equal(recordsPage.includes("recordingActiveEmpty"), true);

assert.equal(recordsPage.includes("recordingsViewToggle"), false);
assert.equal(recordsPage.includes("recordingsCardsGrid"), false);
assert.equal(recordsPage.includes("recordingsCardItem"), false);
assert.equal(recordsPage.includes("recordingsCardTitle"), false);
assert.equal(recordsPage.includes("RECORDS_VIEW_MODE_KEY"), false);
assert.equal(recordsPage.includes("km_vms_records_view_mode"), false);
assert.equal(recordsPage.includes("localStorage.getItem"), false);
assert.equal(recordsPage.includes("localStorage.setItem"), false);

assert.equal(globalsCss.includes(".recordingsViewToggle"), false);
assert.equal(globalsCss.includes(".recordingsCardsGrid"), false);
assert.equal(globalsCss.includes(".recordingsCardItem"), false);
assert.equal(globalsCss.includes(".recordingsCardTitle"), false);

assert.equal(camerasPage.includes("cameraViewToggle"), true);
assert.equal(camerasPage.includes("cameraTileGrid"), true);
