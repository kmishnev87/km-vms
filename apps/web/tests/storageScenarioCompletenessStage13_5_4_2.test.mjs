import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storageSource = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const storagePage = read("app/storage/page.js");
const i18n = read("lib/i18n.js");

const context = {};
vm.runInNewContext(
  `${storageSource}
this.humanBlockerReason = humanBlockerReason;
this.storageTopHealthModel = storageTopHealthModel;
this.retentionScenarioModel = retentionScenarioModel;
this.reconciliationScenarioModel = reconciliationScenarioModel;
this.migrationScenarioModel = migrationScenarioModel;
this.archiveRootScenarioModel = archiveRootScenarioModel;`,
  context
);

assert.match(
  context.storageTopHealthModel({
    operations: { status: "available" },
    capacity: { total_bytes: 100, free_percent: 50 },
    pathHealth: { readable: true, writable: true, available: false },
  }).reason,
  /чтение и запись доступны, но общая проверка архива не подтверждена/
);
assert.equal(
  context.storageTopHealthModel({
    operations: { status: "available" },
    capacity: { total_bytes: 100, free_percent: 1 },
    pathHealth: { readable: true, writable: false, available: true },
    policy: { state: "critical", warning_threshold_percent: 10 },
  }).status,
  "unwritable",
  "write blocker has priority over low disk"
);

for (const code of [
  "active_recording_jobs",
  "active_recording_jobs_block_root_switch",
  "archive_root_not_writable_or_namespace_missing",
  "archive_migration_apply_requires_confirm",
  "recording_format_change_blocked_active_recordings",
  "archive_root_missing",
  "archive_root_unavailable",
  "archive_root_not_writable",
  "archive_root_namespace_mismatch",
  "archive_root_overlap",
  "target_root_missing",
  "path_outside_archive_root",
  "path_outside_storage",
  "insufficient_target_free_space",
  "stale_or_tampered_plan",
  "source_file_changed_after_plan",
  "target_collision",
  "namespace_missing",
  "storage_root_missing",
  "storage_root_not_directory",
  "storage_root_not_readable",
  "storage_root_not_writable",
]) {
  const text = context.humanBlockerReason(code);
  assert.equal(text.includes(code), false, `${code} must not be raw primary text`);
  assert.notEqual(text, "Неизвестно", `${code} must have a human label`);
}
assert.equal(context.humanBlockerReason("future_unknown_raw_code"), "Неизвестно");

assert.equal(
  context.retentionScenarioModel({ permission: { allowed: false, reason: "no" } }).status,
  "unavailable_due_to_permissions"
);
assert.equal(
  context.retentionScenarioModel({ preview: { planned_count: 2 }, permission: { allowed: true } }).canApply,
  true
);
assert.equal(
  context.reconciliationScenarioModel({ preview: { problem_file_count: 1 }, canCheck: { allowed: true }, canApply: { allowed: false, reason: "no" } }).canApply,
  false
);
assert.equal(
  context.migrationScenarioModel({ preview: { apply_available: false, blockers: [{ reason: "stale_or_tampered_plan" }] } }).status,
  "apply_blocked"
);
assert.equal(
  context.archiveRootScenarioModel({ root: { is_active: false, is_available: false, problem: "namespace_missing" } }).canActivate,
  false
);

for (const required of [
  "Что сделать сейчас",
  "Показать, что будет удалено по правилам хранения",
  "Удалить найденные старые записи",
  "Исправить только безопасные проблемы метаданных",
  "Обновить состояние хранилища",
]) {
  assert.ok(i18n.includes(required), `${required} must be visible copy`);
}
for (const forbidden of [
  "Самое безопасное действие",
  "Предпросмотр регламента",
  "Удалить по плану",
  "Применить безопасно",
  "Обновить проверку",
  "Доступность: Нет",
]) {
  assert.equal(i18n.includes(forbidden) || storagePage.includes(forbidden), false, `${forbidden} must be replaced`);
}

assert.match(storagePage, /storageTopHealthModel/, "top health is model-driven");
assert.match(storagePage, /errorDetailText/, "structured action errors are mapped before display");
assert.match(storagePage, /retentionScenarioModel/, "retention state is model-driven");
assert.match(storagePage, /reconciliationScenarioModel/, "reconciliation state is model-driven");
assert.match(storagePage, /migrationScenarioModel/, "migration state is model-driven");
assert.match(storagePage, /archiveRootScenarioModel/, "archive roots state is model-driven");
