import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  formatMaintenanceMessage,
  maintenanceStatusText,
  updateApplyErrorMessages,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");

const dictionaries = {
  ru: {
    maintenanceStatuses: {
      complete: "Завершено",
      completed: "Завершено",
      drift_known_safe: "Известное безопасное расхождение",
      draft_known_safe: "Известный безопасный черновик",
      unknown: "Неизвестно",
    },
    maintenanceMessageFallback: "Статус получен, подробности недоступны.",
    maintenanceActionFallback: "Действие сейчас недоступно. Проверьте состояние системы и повторите позже.",
    maintenanceMessageLabels: {
      schema_metadata_valid: "Метаданные схемы уже в порядке.",
      schema_current_no_pending_migrations: "Схема актуальна, ожидающих миграций нет.",
      restore_no_valid_artifacts: "В настроенной папке резервных копий нет подходящих артефактов восстановления.",
      update_apply_not_available_for_release: "Применение этого релиза из интерфейса недоступно.",
      maintenance_history_limited: "Долговременная история ограничена: показаны текущий статус и последний безопасный отчёт.",
      drift_known_safe: "Известное безопасное расхождение.",
      draft_known_safe: "Известный безопасный черновик.",
    },
  },
  en: {
    maintenanceStatuses: {
      complete: "Complete",
      completed: "Completed",
      drift_known_safe: "Known-safe drift",
      draft_known_safe: "Known-safe draft",
      unknown: "Unknown",
    },
    maintenanceMessageFallback: "Status received; details are unavailable.",
    maintenanceActionFallback: "The action is currently unavailable. Check system status and try again later.",
    maintenanceMessageLabels: {
      schema_metadata_valid: "Schema metadata is already valid.",
      schema_current_no_pending_migrations: "Schema is current; no pending migrations.",
      restore_no_valid_artifacts: "No valid restore artifacts are available in the configured backup root.",
      update_apply_not_available_for_release: "In-app apply is not available for this release.",
      maintenance_history_limited: "Durable history is limited: current status and the latest safe report are shown.",
      drift_known_safe: "Known-safe drift.",
      draft_known_safe: "Known-safe draft.",
    },
  },
  "zh-CN": {
    maintenanceStatuses: {
      complete: "已完成",
      completed: "已完成",
      drift_known_safe: "已知安全的差异",
      draft_known_safe: "已知安全的草稿",
      unknown: "未知",
    },
    maintenanceMessageFallback: "已收到状态，详细信息不可用。",
    maintenanceActionFallback: "该操作当前不可用。请检查系统状态后重试。",
    maintenanceMessageLabels: {
      schema_metadata_valid: "架构元数据已有效。",
      schema_current_no_pending_migrations: "架构已是最新，没有待执行的迁移。",
      restore_no_valid_artifacts: "配置的备份根目录中没有可用的恢复工件。",
      update_apply_not_available_for_release: "此版本不支持在界面内应用。",
      maintenance_history_limited: "持久历史记录有限：仅显示当前状态和最新安全报告。",
      drift_known_safe: "已知安全的差异。",
      draft_known_safe: "已知安全的草稿。",
    },
  },
};

const samples = [
  ["Schema metadata is already valid.", "schema_metadata_valid"],
  ["Schema is current; no pending migrations.", "schema_current_no_pending_migrations"],
  ["No valid restore artifacts are available in configured backup root.", "restore_no_valid_artifacts"],
  ["update_apply_not_available_for_release", "update_apply_not_available_for_release"],
  ["No durable maintenance action history is available beyond current status and generated upgrade report summary.", "maintenance_history_limited"],
  ["drift_known_safe", "drift_known_safe"],
  ["draft_known_safe", "draft_known_safe"],
];

for (const [lang, copy] of Object.entries(dictionaries)) {
  for (const [raw, key] of samples) {
    assert.equal(formatMaintenanceMessage(raw, copy, lang), copy.maintenanceMessageLabels[key], `${lang}: ${raw}`);
  }
  assert.equal(formatMaintenanceMessage("unknown_snake_case_backend_code", copy, lang), copy.maintenanceMessageFallback);
  assert.equal(formatMaintenanceMessage("unknown_snake_case_backend_code", copy, lang, "action"), copy.maintenanceActionFallback);
  assert.equal(maintenanceStatusText("complete", copy), copy.maintenanceStatuses.complete);
  assert.equal(maintenanceStatusText("drift_known_safe", copy), copy.maintenanceStatuses.drift_known_safe);
  assert.equal(maintenanceStatusText("unknown_backend_status", copy), copy.maintenanceStatuses.unknown);
  assert.deepEqual(
    updateApplyErrorMessages({ message: "drift_known_safe", operator_action: "draft_known_safe" }, copy, lang),
    [copy.maintenanceMessageLabels.drift_known_safe, copy.maintenanceMessageLabels.draft_known_safe],
  );
}

for (const forbidden of [
  "{flow.reason || t.maintenanceUnsupported}",
  "maintenanceOverview.upgrade_report?.status ||",
  "<strong>{maintenanceOverview.history?.last_action?.available ? maintenanceOverview.history.last_action.status",
  "maintenanceOverview.history?.last_action?.reason ||",
  "{updateApplyStatus.error.message}",
  "{maintenanceActionResult.reason}",
]) {
  assert.equal(settingsPage.includes(forbidden), false, `${forbidden} must not render raw backend text`);
}

assert.equal(settingsPage.includes("maintenanceReadinessRows(maintenanceOverview, t)"), true);
assert.equal(settingsPage.includes("updateApplyErrorMessages(updateApplyStatus?.error, t, lang)"), true);
