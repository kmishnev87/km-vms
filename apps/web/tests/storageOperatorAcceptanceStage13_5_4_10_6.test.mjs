import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const repoRoot = resolve(webRoot, "..", "..");
const read = (relative) => fs.readFileSync(resolve(webRoot, relative), "utf8");
const page = read("app/storage/page.js");
const center = read("components/storage/ArchiveManagementCenter.js");
const helpers = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const css = read("app/styles/40-storage-records-shared.css");
const i18n = read("lib/i18n.js");
const routes = read("lib/routePermissions.js");
const backendHistory = fs.readFileSync(resolve(repoRoot, "apps/api/app/services/storage_monitoring.py"), "utf8");

const context = {};
vm.runInNewContext(
  `${helpers}
this.archiveIntegrityFindingPresentation = archiveIntegrityFindingPresentation;`,
  context
);

const overview = page.slice(page.indexOf("<section className={`storageOpsOverview"), page.indexOf("{refreshWarning ?"));
assert.equal((overview.match(/<TopMetric/g) || []).length, 3);
assert.match(overview, /label=\{copy\.archiveAccess\}/);
assert.match(overview, /label=\{`\$\{copy\.free\} \$\{formatPercent\(capacity\.free_percent\)\}`\}/);
assert.match(overview, /label=\{copy\.archiveProblems\}/);
assert.doesNotMatch(overview, /storageOpsBadges|compactAccessLabel|primaryAction/);
assert.match(overview, /onValueClick=\{\(\) => openIntegrityDialog\(\)\}/);
assert.doesNotMatch(overview, /storageOpsHealthAction|\/assets\/icons\/ui\/open\.png|recording\.detail/);
assert.match(overview, /actionLabel=\{diagnosticsPermission\.allowed \? copy\.integrityOpenCheck : diagnosticsPermission\.reason\}/);
assert.equal(page.includes("/assets/icons/ui/open-"), false);
assert.match(css, /Stage 4\.10\.6 final cascade ownership[\s\S]*\.storageOpsOverview\s*\{[\s\S]*grid-template-columns:\s*minmax\(280px, 1\.2fr\) minmax\(420px, 1\.65fr\);/);
assert.match(css, /\.storageOpsTopMetricValueButton\s*\{[\s\S]*text-decoration:\s*none;/);

assert.doesNotMatch(center, /historyCount/);
assert.doesNotMatch(center, /archiveManagementHistoryButton[\s\S]*<strong aria-hidden/);
assert.match(page, /apiFetch\("\/storage\/operations\/history"\)/);
assert.match(center, /history\.summary\?\.camera_retention/);
assert.match(center, /history\.daily_items/);
assert.match(center, /history\.attention_items/);
assert.match(backendHistory, /or_\(deleted_expr > 0, bytes_expr > 0\)/);
assert.match(backendHistory, /\.filter\([\s\S]*or_\(deleted_expr > 0, bytes_expr > 0\)[\s\S]*\.limit\(USEFUL_HISTORY_DAILY_LIMIT\)/);
assert.doesNotMatch(center, /Требуют внимания:\s*0|Needs attention:\s*0/);

const partial = context.archiveIntegrityFindingPresentation({
  category: "partial_file",
  impact_key: "recording_incomplete",
  action_key: "delete_unusable_recording",
  action_allowed: true,
  confirmation_level: "destructive_media",
  state: "active",
});
assert.equal(partial.actionLabelKey, "integrityActionDeleteUnusable");
assert.equal(partial.detailKey, "integrityDetailPartial");
const stale = context.archiveIntegrityFindingPresentation({
  category: "stale_writing_segment",
  no_action_reason: "automatic_reconciliation_pending",
  action_allowed: false,
  state: "active",
});
assert.equal(stale.actionAllowed, false);
assert.equal(stale.noActionLabelKey, "integrityNoActionAutomaticReconciliation");

for (const [asset, approvedSha256] of [
  ["delete-recording.svg", "27a63e80937052d7e2c5b110a73a76fdf50577a7b71ddbba9146ca1e9707a8a3"],
  ["retire-missing-recording.svg", "d2ed980d63be0343553282ab741f1d8f72664c7fce7f9a3c39ec46e9f7aa8320"],
  ["delete-orphan-file.svg", "4c4ef7c5eedbbe6755e48c8970756eb987e61eaf9722d1a22688d8665d189c87"],
]) {
  const product = fs.readFileSync(resolve(webRoot, "public/assets/icons/ui", asset));
  assert.equal(createHash("sha256").update(product).digest("hex"), approvedSha256, `${asset} must keep its approved bytes`);
  assert.match(page, new RegExp(asset.replace(".", "\\.")));
}

assert.match(page, /storageIntegrityConfirmationOverlay/);
assert.match(page, /ref=\{confirmationRef\}/);
assert.match(page, /if \(event\.key === "Escape" && !busy\)/);
assert.match(page, /onKeyDown=\{handleConfirmationKeyDown\}/);
assert.match(page, /confirm:\s*true/);
assert.doesNotMatch(page, /integrityConfirmed|type="checkbox"/);
assert.match(page, /title=\{copy\[item\.actionLabelKey\]\}/);
assert.match(page, /aria-label=\{copy\[item\.actionLabelKey\]\}/);
assert.match(page, /settingsInfoBubble" role="tooltip">\{copy\[item\.actionLabelKey\]\}/);
assert.match(css, /\.storageIntegrityConfirmationOverlay\s*\{[\s\S]*place-items:\s*center/);
assert.match(css, /\.storageIntegrityIconAction\s*\{[\s\S]*width:\s*40px !important;[\s\S]*height:\s*40px/);

assert.match(css, /--storage-root-action-column:\s*40px/);
assert.match(css, /\.storageOpsRootListRow\s*\{[\s\S]*var\(--storage-root-action-column\)/);
assert.match(css, /\.storageOpsAdvancedRoot \.storageOpsRootForm-product\s*\{[\s\S]*var\(--storage-root-action-column\)/);
const rootAxisRules = css.slice(css.indexOf(".storageOpsSection-roots {", css.indexOf("Stage 4.10.6")), css.indexOf("@media (max-width: 760px)", css.indexOf("Stage 4.10.6")));
assert.doesNotMatch(rootAxisRules, /transform:\s*translate|margin-right:\s*-/);
assert.match(css, /@media \(max-width: 760px\)[\s\S]*\.storageOpsAdvancedRoot \.storageOpsRootForm-product\s*\{[\s\S]*grid-template-columns:\s*1fr/);
assert.doesNotMatch(css, /\.storageOpsAdvancedRoot \.storageOpsRootForm-product\s*\{[^}]*padding-right:\s*0/);
assert.doesNotMatch(css, /\.storageOpsAdvancedRoot \.storageOpsRootAddButton\.appIllustratedAction\s*\{[^}]*justify-self:\s*start/);

assert.match(routes, /path: "\/storage\/operations\/history", permission: "manage_settings"/);
for (const key of [
  "operationHistory24Hours",
  "operationHistoryCameraRetention",
  "operationHistoryAutoFree",
  "operationHistoryUsefulEmpty",
  "integrityDetailZeroSize",
  "integrityDetailPartial",
  "integrityNoActionAutomaticReconciliation",
  "archiveAccess",
  "integrityScanCompletedWithProblemsTitle",
  "integrityScanCompletedWithProblemsText",
]) {
  assert.equal((i18n.match(new RegExp(`${key}:`, "g")) || []).length, 3, `${key} must exist in all locales`);
}

console.log("Stage 13.5 / 4.10.6 storage operator acceptance: PASS");
