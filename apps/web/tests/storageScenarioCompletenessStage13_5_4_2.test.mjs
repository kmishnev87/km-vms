import { readI18nSource } from "./helpers/readI18nSources.mjs";
import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import * as storageOperations from "../lib/storageOperations.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storagePage = read("app/storage/page.js");
const i18n = readI18nSource();

const context = storageOperations;

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
  context.retentionOperationPresentation({ configured_camera_count: 0 }).status,
  "not_configured"
);
assert.equal(
  context.retentionOperationPresentation({ configured_camera_count: 2, running: true }).status,
  "running"
);
assert.equal(
  context.integrityOperationPresentation({ status: "completed", problem_file_count: 1 }).status,
  "findings"
);
assert.equal(
  context.migrationScenarioModel({ plan: { status: "blocked", reason_code: "stale_or_tampered_plan" } }).status,
  "blocked"
);
assert.equal(
  context.archiveRootScenarioModel({ root: { is_active: false, is_available: false, problem: "namespace_missing" } }).canActivate,
  false
);

for (const required of [
  "Что сделать сейчас",
  "Управление архивом",
  "Самые старые записи удаляются автоматически",
  "Откройте список, чтобы увидеть причину",
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
  assert.equal(storagePage.includes(forbidden), false, `${forbidden} must not render in primary /storage UI`);
}

assert.match(storagePage, /storageTopHealthModel/, "top health is model-driven");
assert.match(storagePage, /errorDetailText/, "structured action errors are mapped before display");
assert.match(storagePage, /retentionOperationPresentation/, "retention state is model-driven");
assert.match(storagePage, /integrityOperationPresentation/, "integrity state is model-driven");
assert.match(storagePage, /migrationScenarioModel/, "migration state is model-driven");
assert.match(storagePage, /archiveRootScenarioModel/, "archive roots state is model-driven");
