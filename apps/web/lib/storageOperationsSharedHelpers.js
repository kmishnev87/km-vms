export function asNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function finiteCount(value) {
  if (value == null || value === "") return null;
  const count = Number(value);
  return Number.isFinite(count) && count >= 0 ? count : null;
}

export function statusLabel(value, language = "ru") {
  const labels = {
    ru: {
      available: "Доступно",
      blocked: "Заблокировано",
      degraded: "Есть предупреждения",
      unavailable: "Недоступно",
      ok: "Норма",
      warning: "Предупреждение",
      critical: "Критично",
      cleanup_threshold: "Порог автоосвобождения",
      capacity_unknown: "Место неизвестно",
      problems_found: "Найдены проблемы",
      pending: "Ожидает",
      running: "Выполняется",
      never_run: "Не запускалось",
      completed_with_warnings: "Завершено с предупреждениями",
      failed: "Ошибка",
      idle: "Ожидает действия",
      unavailable_due_to_permissions: "Недоступно по правам",
      check_needed: "Нужна проверка",
      preview_completed: "Предпросмотр готов",
      apply_completed: "Применение завершено",
      apply_blocked: "Применение заблокировано",
      apply_failed: "Применение завершилось ошибкой",
      unknown: "Неизвестно",
    },
    en: {
      available: "Available",
      blocked: "Blocked",
      degraded: "Warnings",
      unavailable: "Unavailable",
      ok: "OK",
      warning: "Warning",
      critical: "Critical",
      cleanup_threshold: "Cleanup threshold",
      capacity_unknown: "Capacity unknown",
      problems_found: "Problems found",
      pending: "Pending",
      running: "Running",
      never_run: "Never run",
      completed_with_warnings: "Completed with warnings",
      failed: "Failed",
      idle: "Waiting for action",
      unavailable_due_to_permissions: "Permission limited",
      check_needed: "Check needed",
      preview_completed: "Preview ready",
      apply_completed: "Apply completed",
      apply_blocked: "Apply blocked",
      apply_failed: "Apply failed",
      unknown: "Unknown",
    },
    "zh-CN": {
      available: "可用",
      blocked: "已阻止",
      degraded: "有告警",
      unavailable: "不可用",
      ok: "正常",
      warning: "告警",
      critical: "严重",
      cleanup_threshold: "自动清理阈值",
      capacity_unknown: "容量未知",
      problems_found: "发现问题",
      pending: "等待中",
      running: "运行中",
      never_run: "从未运行",
      completed_with_warnings: "已完成但有告警",
      failed: "失败",
      idle: "等待操作",
      unavailable_due_to_permissions: "权限受限",
      check_needed: "需要检查",
      preview_completed: "预览已就绪",
      apply_completed: "应用已完成",
      apply_blocked: "应用已阻止",
      apply_failed: "应用失败",
      unknown: "未知",
    },
  };
  const table = labels[language] || labels.ru;
  return table[value] || table.unknown;
}
