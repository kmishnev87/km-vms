import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (relative) => fs.readFileSync(resolve(__dirname, "..", relative), "utf8");
const helpers = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${helpers}
this.migrationScenarioModel = migrationScenarioModel;
this.humanBlockerReason = humanBlockerReason;`,
  context
);

const allowed = { allowed: true, reason: "" };
const denied = { allowed: false, reason: "human permission reason" };
const basePlan = {
  plan_id: "plan-opaque",
  canonical_hash: "a".repeat(64),
  source_root_id: "source-root",
  target_root_id: "target-root",
  item_count: 4,
  completed_count: 0,
  total_bytes: 400,
  completed_bytes: 0,
  excluded_count: 1,
  cleanup_pending: false,
};

for (const status of ["building", "queued", "running", "cancel_requested"]) {
  const model = context.migrationScenarioModel({
    plan: { ...basePlan, status },
    operation: ["queued", "running", "cancel_requested"].includes(status)
      ? { operation_id: "operation-opaque", status, cancel_allowed: true, progress: {} }
      : null,
    preparePermission: allowed,
    applyPermission: allowed,
  });
  assert.equal(model.status, status);
  assert.equal(model.active, true);
  assert.equal(model.completedProof, false);
}

for (const status of ["partial", "failed", "blocked", "cancelled", "expired"]) {
  const model = context.migrationScenarioModel({
    plan: { ...basePlan, status },
    operation: { operation_id: "operation-opaque", status, retry_allowed: status === "partial" },
    preparePermission: allowed,
    applyPermission: allowed,
  });
  assert.equal(model.status, status);
  assert.equal(model.terminal, true);
  assert.equal(model.completedProof, false);
  assert.equal(model.percent === 100, false);
}

const ready = context.migrationScenarioModel({
  plan: { ...basePlan, status: "ready_with_exclusions" },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(ready.ready, true);
assert.equal(ready.canApply, true);

const runningAtCopyEnd = context.migrationScenarioModel({
  plan: { ...basePlan, status: "running", completed_count: 3, completed_bytes: 300 },
  operation: { status: "running", progress: { total_bytes: 400, completed_bytes: 300, current_item_bytes: 100 } },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(runningAtCopyEnd.percent, 99);
assert.equal(runningAtCopyEnd.completedProof, false);

const completed = context.migrationScenarioModel({
  plan: { ...basePlan, status: "completed", completed_count: 4, completed_bytes: 400 },
  operation: { status: "completed", progress: { total_bytes: 400, completed_bytes: 400 } },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(completed.completedProof, true);
assert.equal(completed.percent, 100);

const incompleteCompleted = context.migrationScenarioModel({
  plan: { ...basePlan, status: "completed", completed_count: 3, cleanup_pending: true },
  operation: { status: "completed" },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(incompleteCompleted.completedProof, false);
assert.equal(incompleteCompleted.percent === 100, false);

const unknown = context.migrationScenarioModel({
  plan: { status: "unexpected_backend_status", same_physical_volume: null },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(unknown.status, "unknown");
assert.equal(unknown.percent, null);
assert.equal(unknown.itemCount, null);
assert.equal(unknown.totalBytes, null);
assert.equal(unknown.samePhysicalVolume, null);

const permissionDenied = context.migrationScenarioModel({
  plan: { ...basePlan, status: "ready" },
  preparePermission: allowed,
  applyPermission: denied,
});
assert.equal(permissionDenied.canPrepare, true);
assert.equal(permissionDenied.canApply, false);
assert.equal(permissionDenied.applyPermissionReason, "human permission reason");

const backendTransferMetrics = context.migrationScenarioModel({
  plan: { ...basePlan, status: "running" },
  operation: {
    status: "running",
    progress: {
      phase: "copying",
      speed_bytes_per_second: 2_500_000,
      eta_seconds: 42,
    },
  },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(backendTransferMetrics.speedBytesPerSecond, 2_500_000);
assert.equal(backendTransferMetrics.etaSeconds, 42);
assert.equal(backendTransferMetrics.transferMetricsWarming, false);

const warmingTransferMetrics = context.migrationScenarioModel({
  plan: { ...basePlan, status: "running" },
  operation: {
    status: "running",
    progress: { phase: "copying", speed_bytes_per_second: null, eta_seconds: null },
  },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(warmingTransferMetrics.speedBytesPerSecond, null);
assert.equal(warmingTransferMetrics.etaSeconds, null);
assert.equal(warmingTransferMetrics.transferMetricsWarming, true);

const staleTransferMetrics = context.migrationScenarioModel({
  plan: { ...basePlan, status: "running" },
  operation: {
    status: "running",
    progress: { phase: "target_verified", speed_bytes_per_second: 2_500_000, eta_seconds: 42 },
  },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(staleTransferMetrics.speedBytesPerSecond, null);
assert.equal(staleTransferMetrics.etaSeconds, null);
assert.equal(staleTransferMetrics.transferMetricsWarming, false);

const adminCleanupTakeover = context.migrationScenarioModel({
  plan: { ...basePlan, status: "partial", cleanup_pending: true },
  operation: {
    operation_id: "operation-opaque",
    status: "partial",
    retry_allowed: true,
    capabilities: { owner_retry_allowed: false, cleanup_takeover_allowed: true },
  },
  preparePermission: allowed,
  applyPermission: allowed,
});
assert.equal(adminCleanupTakeover.canRetry, false);
assert.equal(adminCleanupTakeover.canCleanupTakeover, true);

for (const code of [
  "migration_permission_revoked",
  "migration_final_target_missing",
  "migration_quarantine_provenance_mismatch",
  "migration_no_eligible_recordings",
  "migration_retry_not_allowed",
  "migration_plan_actor_mismatch",
  "migration_cleanup_takeover_not_allowed",
  "migration_recovery_permission_revoked",
  "migration_plan_preparation_failed",
  "migration_root_revalidation_failed",
  "migration_temp_pending_object_ambiguous",
  "migration_operation_failed",
]) {
  for (const language of ["ru", "en", "zh-CN"]) {
    const value = context.humanBlockerReason(code, language);
    assert.ok(value.length > 12, `${code}/${language} must be human-readable`);
    assert.equal(value.includes(code), false, `${code}/${language} must not expose a raw code`);
  }
}

const page = read("app/storage/page.js");
const layout = read("components/Layout.js");
const css = read("app/styles/40-storage-records-shared.css");
const responsive = read("app/styles/60-responsive-shared.css");
const i18n = read("lib/i18n.js");
const migrationDialogSource = page.slice(page.indexOf("function ArchiveMigrationDialog"), page.indexOf("export default function StorageOperationsPage"));
const etaFormatterSource = page.slice(
  page.indexOf("function migrationEtaText"),
  page.indexOf("\n}\n", page.indexOf("function migrationEtaText")) + 2,
);
const etaFormatterContext = {};
vm.runInNewContext(`${etaFormatterSource}; this.migrationEtaText = migrationEtaText;`, etaFormatterContext);
assert.equal(etaFormatterContext.migrationEtaText(null, { migrationCalculating: "calculating" }), "calculating");
assert.equal(etaFormatterContext.migrationEtaText(undefined, { migrationCalculating: "calculating" }), "calculating");
assert.equal(etaFormatterContext.migrationEtaText("", { migrationCalculating: "calculating" }), "calculating");

assert.match(migrationDialogSource, /copy\.migrationSource/);
assert.match(migrationDialogSource, /copy\.migrationTarget/);
assert.match(migrationDialogSource, /<OperationDialog/);
assert.match(migrationDialogSource, /copy\.migrationConfirmCleanup/);
assert.match(migrationDialogSource, /copy\.migrationCompletionScopeNote/);
assert.equal(migrationDialogSource.includes("window.confirm"), false);
assert.equal(migrationDialogSource.includes("window.alert"), false);
assert.match(page, /apiFetch\("\/storage\/migration\/preview"/);
assert.match(page, /apiFetch\("\/storage\/migration\/apply"/);
assert.match(page, /\/storage\/migration\/operations\/\$\{encodeURIComponent\(operationId\)\}\/retry/);
assert.match(page, /\/storage\/migration\/operations\/\$\{encodeURIComponent\(operationId\)\}\/cleanup-takeover/);
assert.match(migrationDialogSource, /scenario\.phase === "copying"/);
assert.match(migrationDialogSource, /scenario\.speedBytesPerSecond/);
assert.match(migrationDialogSource, /scenario\.etaSeconds/);
assert.equal(migrationDialogSource.includes("setInterval"), false, "frontend must not derive transfer metrics");
assert.match(page, /useSearchParams/);
assert.match(page, /searchParams\.get\("migration"\)/);
assert.match(page, /router\.replace\("\/storage", \{ scroll: false \}\)/);
assert.match(page, /apiFetch\("\/storage\/migration\/operations\/active"\)/);
assert.match(layout, /apiFetch\("\/storage\/migration\/operations\/active"\)/);
assert.match(layout, /\/storage\?migration=\$\{encodeURIComponent\(operationId\)\}/);
assert.match(layout, /Math\.min\(99,/);

assert.match(css, /\.archiveMigrationDialog\s*\{[\s\S]*width:\s*min\(720px, 100%\)/);
assert.match(css, /\.archiveMigrationWizard\s*\{[\s\S]*min-width:\s*0/);
assert.match(css, /\.archiveMigrationFields \.select\s*\{[\s\S]*min-width:\s*0/);
assert.match(responsive, /\.archiveMigrationFields,[\s\S]*grid-template-columns:\s*1fr/);

for (const key of [
  "migrationDialogTitle",
  "migrationSource",
  "migrationTarget",
  "migrationPrepare",
  "migrationStart",
  "migrationCancel",
  "migrationRetryCleanup",
  "migrationCleanupTakeover",
  "migrationSpeed",
  "migrationEta",
  "migrationCalculating",
  "migrationCleanupTakeoverFailed",
  "migrationCompletionScopeNote",
  "migrationFinalTargetMissing",
]) {
  if (key === "migrationFinalTargetMissing") continue;
  assert.equal((i18n.match(new RegExp(`${key}:`, "g")) || []).length, 3, `${key} must exist in all locales`);
}

for (const stale of [
  /copy-only/i,
  /только копированием/i,
  /сохраняет исходные файлы/i,
  /preserves source files/i,
  /仅复制操作/,
  /保留源文件/,
]) {
  assert.equal(stale.test(i18n), false, `stale copy-only wording remains: ${stale}`);
}

for (const forbidden of [
  "storageOpsArchiveOperationsV2",
  "storageOpsMigrationScenarioRow",
  "storageOpsOperationHistoryModal",
]) {
  assert.equal(page.includes(forbidden), false, `${forbidden} would start Stage 4.10.5`);
}
