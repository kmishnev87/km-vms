import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");

const storagePage = read("app/storage/page.js");
const recordingsPage = read("app/recordings/page.js");
const feedback = read("components/OperationFeedback.js");
const storageHelpers = read("lib/storageOperations.js");
const storageCss = read("app/styles/40-storage-records-shared.css");
const responsiveCss = read("app/styles/60-responsive-shared.css");
const i18n = read("lib/i18n.js");

assert.doesNotMatch(recordingsPage, /window\.confirm/, "recording deletion never uses a browser-native confirm");
assert.match(recordingsPage, /\/recordings\/deletion-plans/);
assert.match(recordingsPage, /deletion-plans\/\$\{encodeURIComponent\(plan\.plan_id\)\}\/execute/);
assert.match(recordingsPage, /body:\s*JSON\.stringify\(\{ confirm: true \}\)/);
assert.match(recordingsPage, /operation_id:\s*operationId,\s*items:\s*selectedItems/);
assert.match(recordingsPage, /recordingIdentityQuery\(item\).*operation_id=/s);
assert.match(recordingsPage, /result\?\.ok === true && result\?\.status === "completed"/);
assert.match(recordingsPage, /result\?\.skipped_reason_counts|skipped_reason_counts/);
assert.match(recordingsPage, /<OperationDialog dialog=\{deleteDialog\}/);
assert.match(recordingsPage, /<OperationToast toast=\{deleteToast\}/);

for (const contract of [
  /role="dialog"/,
  /aria-modal="true"/,
  /aria-labelledby=\{titleId\}/,
  /aria-describedby=\{descriptionId\}/,
  /aria-busy=/,
  /event\.key === "Escape"/,
  /event\.key !== "Tab"/,
  /returnFocusRef\.current\.focus/,
  /cancelRef\.current \|\| closeRef\.current/,
  /tabIndex=\{-1\}/,
]) {
  assert.match(feedback, contract, `shared operation dialog keeps ${contract}`);
}
assert.match(feedback, /aria-live="polite"/);
assert.match(feedback, /window\.setTimeout\(\(\) => onCloseRef\.current/);

assert.doesNotMatch(storagePage, /showArchiveRootActivation|setArchiveRootMessage|storageOpsSupportDetails/);
assert.match(storagePage, /ACTIVATION_ACK_KEY/);
assert.match(storagePage, /activationOperationId/);
assert.match(storagePage, /window\.sessionStorage\.setItem\(ACTIVATION_ACK_KEY/);
assert.match(storagePage, /window\.setTimeout\([\s\S]*?setArchiveRootDialog\(null\)/);
assert.match(storagePage, /<OperationDialog dialog=\{archiveRootDialog\}/);
assert.match(storagePage, /<OperationToast toast=\{operationToast\}/);
assert.match(storagePage, /rootAddedTitle/);
assert.match(storagePage, /archiveRootDeletedTitle/);
assert.match(storagePage, /archiveRootCleanupReason/);
assert.match(storagePage, /return rootHasProblems\(root\) \? "error" : "ok"/);
assert.match(storagePage, /requires_activation && !root\.is_active && Number\(root\.segments_count \|\| 0\) === 0/);

const archiveIndex = storagePage.indexOf("<Section title={copy.archiveSpace}");
const cameraIndex = storagePage.indexOf("<Section title={copy.cameras}");
const operationsIndex = storagePage.indexOf("<Section title={copy.archiveOperations}");
const rootsIndex = storagePage.indexOf("<Section title={copy.archiveRoots}");
assert.ok(archiveIndex > 0 && archiveIndex < cameraIndex && cameraIndex < operationsIndex && operationsIndex < rootsIndex);
assert.equal((storagePage.slice(cameraIndex, operationsIndex).match(/<th>\{copy\.problems\}<\/th>/g) || []).length, 1);
assert.match(storagePage, /row\.problem_counts/);
assert.match(storagePage, /showCameraProblems\(row\)/);
assert.match(storageHelpers, /problem_counts:\s*Object\.fromEntries/);

assert.match(storageCss, /grid-template-areas:[\s\S]*?"archive cameras"[\s\S]*?"roots operations"/);
assert.match(storageCss, /storageOpsSection-operations \.storageOpsOperationList[\s\S]*?grid-template-columns:\s*minmax\(0, 1fr\)/);
assert.match(storageCss, /storageOpsRootForm-product[\s\S]*?height:\s*38px/);
assert.match(storageCss, /storageOpsCameraTable[\s\S]*?font-size:\s*12px/);
assert.match(responsiveCss, /@media \(max-width: 980px\)[\s\S]*?"archive"[\s\S]*?"cameras"[\s\S]*?"operations"[\s\S]*?"roots"/);

for (const key of [
  "rootAddedTitle",
  "archiveRootDeletedTitle",
  "activationCompletedTitle",
  "cameraProblemDialogTitle",
  "archiveRootCleanupPending",
  "archiveRootCleanupRetry",
]) {
  assert.equal((i18n.match(new RegExp(`\\b${key}:`, "g")) || []).length, 3, `${key} exists in RU, EN and zh-CN`);
}

for (const [file, source] of [
  ["storage page", storagePage],
  ["recordings page", recordingsPage],
  ["operation feedback", feedback],
  ["i18n", i18n],
]) {
  assert.doesNotMatch(source, /\uFFFD|Рџ|Рґ|Ð|�|\?\?\?/, `${file} has no encoding corruption`);
}

console.log("Stage 13.5.4.9 frontend contracts passed");
