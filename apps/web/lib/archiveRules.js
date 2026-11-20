export const ARCHIVE_RULE_FIELDS = ["segment_minutes", "retention_days", "storage_quota_gb"];
export const ARCHIVE_TARGET_FIELD = "recording_archive_root_id";
export const ARCHIVE_RULE_LIMITS = { segment_minutes: 2147483647, retention_days: 36500, storage_quota_gb: 2147483647 };

export function archiveRuleDraft(rows) {
  return Object.fromEntries(rows.map(row => [row.camera_id, {
    ...Object.fromEntries(ARCHIVE_RULE_FIELDS.map(key => [key, String(row[key])])),
    ...(ARCHIVE_TARGET_FIELD in row ? { [ARCHIVE_TARGET_FIELD]: row[ARCHIVE_TARGET_FIELD] } : {}),
  }]));
}

export function archiveRuleProblems(rows, draft) {
  const problems = {};
  for (const row of rows) for (const key of ARCHIVE_RULE_FIELDS) {
    const raw = String(draft[row.camera_id]?.[key] ?? "");
    const value = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(value) || value < 1 || value > ARCHIVE_RULE_LIMITS[key]) {
      problems[`${row.camera_id}:${key}`] = true;
    }
  }
  return problems;
}

export function archiveRuleChanges(rows, draft) {
  return rows.flatMap(row => {
    const values = Object.fromEntries(ARCHIVE_RULE_FIELDS.filter(key => Number(draft[row.camera_id]?.[key]) !== row[key])
      .map(key => [key, Number(draft[row.camera_id]?.[key])]));
    const previousRoot = row[ARCHIVE_TARGET_FIELD];
    const nextRoot = draft[row.camera_id]?.[ARCHIVE_TARGET_FIELD];
    if (nextRoot !== undefined && nextRoot !== previousRoot) values[ARCHIVE_TARGET_FIELD] = nextRoot;
    return Object.keys(values).length ? [{ camera_id: row.camera_id,
      expected: { ...Object.fromEntries(ARCHIVE_RULE_FIELDS.map(key => [key, row[key]])),
        ...(ARCHIVE_TARGET_FIELD in values ? { [ARCHIVE_TARGET_FIELD]: previousRoot ?? null } : {}) }, values }] : [];
  });
}

export function reducedArchiveRules(rows, draft) {
  return rows.some(row => ["retention_days", "storage_quota_gb"].some(key => Number(draft[row.camera_id]?.[key]) < row[key]));
}

export const ARCHIVE_RULE_COPY = {
  ru: { title: "Правила записи и хранения", help: "Настройки сохраняются сразу для всех изменённых камер.", camera: "Камера", segment_minutes: "Сегмент, мин", retention_days: "Хранение, дней", storage_quota_gb: "Лимит, ГБ", close: "Закрыть", cancel: "Отмена", save: "Сохранить", saving: "Сохранение…", loading: "Загрузка камер…", empty: "Нет добавленных камер.", failed: "Не удалось загрузить правила. Повторите попытку.", retry: "Повторить", invalid: "Введите целые положительные значения. Срок хранения — до 36 500 дней.", conflict: "Правила некоторых камер уже изменились. Обновите список и повторите изменения.", saveFailed: "Не удалось подтвердить сохранение. Обновите список и проверьте значения перед повтором.", reduced: "Срок или лимит уменьшен: штатная очистка сможет удалить старые записи по новым правилам.", selectAll: "Выбрать все камеры", select: "Выбрать камеру", selected: "Выбрано", bulk: "Общие значения", keep: "Не менять", apply: "Применить к выбранным", disabled: "Запись выключена", discard: "Закрыть без сохранения изменений?", discardYes: "Не сохранять", continue: "Продолжить редактирование" },
  en: { title: "Recording and retention rules", help: "Save all changed camera rules together.", camera: "Camera", segment_minutes: "Segment, min", retention_days: "Keep, days", storage_quota_gb: "Limit, GB", close: "Close", cancel: "Cancel", save: "Save", saving: "Saving…", loading: "Loading cameras…", empty: "No cameras have been added.", failed: "Could not load the rules. Try again.", retry: "Retry", invalid: "Enter positive whole numbers. Retention supports up to 36,500 days.", conflict: "Some camera rules have changed. Reload the list and make your changes again.", saveFailed: "Could not confirm the save. Reload and verify the values before retrying.", reduced: "A retention period or limit is lower: automatic cleanup may delete older recordings under the new rules.", selectAll: "Select all cameras", select: "Select camera", selected: "Selected", bulk: "Shared values", keep: "Keep current", apply: "Apply to selected", disabled: "Recording disabled", discard: "Discard changes and close?", discardYes: "Discard", continue: "Keep editing" },
  "zh-CN": { title: "录像和保留规则", help: "一次保存所有已修改的摄像机规则。", camera: "摄像机", segment_minutes: "分段（分钟）", retention_days: "保留（天）", storage_quota_gb: "限额（GB）", close: "关闭", cancel: "取消", save: "保存", saving: "正在保存…", loading: "正在加载摄像机…", empty: "尚未添加摄像机。", failed: "无法加载规则，请重试。", retry: "重试", invalid: "请输入正整数。保留时间最长为 36,500 天。", conflict: "部分摄像机规则已更改，请重新加载后再次修改。", saveFailed: "无法确认保存结果。请重新加载并检查数值后再重试。", reduced: "保留时间或限额已降低：自动清理可能按新规则删除较早的录像。", selectAll: "选择所有摄像机", select: "选择摄像机", selected: "已选择", bulk: "统一设置", keep: "保持原值", apply: "应用到已选摄像机", disabled: "录像已关闭", discard: "放弃更改并关闭？", discardYes: "放弃", continue: "继续编辑" },
};
