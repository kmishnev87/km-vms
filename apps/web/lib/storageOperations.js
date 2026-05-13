export function asNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function formatBytes(value) {
  const bytes = asNumber(value, 0);
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let current = bytes;
  let index = 0;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  const precision = current >= 10 || index === 0 ? 0 : 1;
  return `${current.toFixed(precision)} ${units[index]}`;
}

export function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(number >= 10 ? 1 : 2)}%`;
}

export function formatDateTime(value, language = "ru") {
  if (!value) {
    if (language === "en") return "Never";
    if (language === "zh-CN") return "从未运行";
    return "Не запускалось";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(language === "en" ? "en-US" : language === "zh-CN" ? "zh-CN" : "ru-RU");
}

export function statusLabel(value, language = "ru") {
  const labels = {
    ru: {
      available: "Доступно",
      degraded: "Есть предупреждения",
      unavailable: "Недоступно",
      ok: "Норма",
      warning: "Предупреждение",
      critical: "Критично",
      cleanup_threshold: "Порог автоосвобождения",
      capacity_unknown: "Место неизвестно",
      problems_found: "Найдены проблемы",
      never_run: "Не запускалось",
      completed_with_warnings: "Завершено с предупреждениями",
      failed: "Ошибка",
      unknown: "Неизвестно",
    },
    en: {
      available: "Available",
      degraded: "Warnings",
      unavailable: "Unavailable",
      ok: "OK",
      warning: "Warning",
      critical: "Critical",
      cleanup_threshold: "Cleanup threshold",
      capacity_unknown: "Capacity unknown",
      problems_found: "Problems found",
      never_run: "Never run",
      completed_with_warnings: "Completed with warnings",
      failed: "Failed",
      unknown: "Unknown",
    },
    "zh-CN": {
      available: "可用",
      degraded: "有警告",
      blocked: "已阻止",
      warning: "警告",
      ok: "正常",
      pending: "等待中",
      running: "运行中",
      problems_found: "发现问题",
      never_run: "从未运行",
      completed_with_warnings: "已完成但有警告",
      failed: "失败",
      unknown: "未知",
    },
  };
  const table = labels[language] || labels.ru;
  return table[value] || table.unknown;
}

export function boolLabel(value, language = "ru") {
  if (language === "en") return value ? "Yes" : "No";
  if (language === "zh-CN") return value ? "是" : "否";
  return value ? "Да" : "Нет";
}

export function policyStateLabel(policy, language = "ru") {
  const enabled = Boolean(policy?.auto_free_space_cleanup_enabled);
  if (language === "en") return enabled ? "ON" : "OFF";
  if (language === "zh-CN") return enabled ? "开启" : "关闭";
  return enabled ? "Включена" : "Выключена";
}

export function lowDiskPolicyText(policy, language = "ru") {
  const cleanupEnabled = Boolean(policy?.auto_free_space_cleanup_enabled);
  const warning = policy?.warning_threshold_percent ?? 10;
  const cleanup = policy?.cleanup_threshold_percent ?? 5;
  const critical = policy?.critical_threshold_percent ?? 1;
  if (language === "en") {
    return cleanupEnabled
      ? `Warning below ${warning}% free. Below ${cleanup}% the system may delete only owned metadata-safe recordings. Below ${critical}% recording may be suspended.`
      : `Warning below ${warning}% free. Automatic deletion is OFF; below ${critical}% recording may still be suspended to protect the disk.`;
  }
  if (language === "zh-CN") {
    return cleanupEnabled
      ? `可用空间低于 ${warning}% 时显示警告。低于 ${cleanup}% 时系统只能删除属于 KM VMS 且已由元数据安全确认的旧录像。低于 ${critical}% 时可暂停录像以保护磁盘。`
      : `可用空间低于 ${warning}% 时仅显示警告。自动删除已关闭；低于 ${critical}% 时仍可暂停录像以保护磁盘。`;
  }
  return cleanupEnabled
    ? `Ниже ${warning}% система предупреждает о нехватке места. Ниже ${cleanup}% она может автоматически удалить только старые записи, принадлежащие KM VMS и безопасно подтверждённые метаданными. Ниже ${critical}% запись может быть приостановлена для защиты диска.`
    : `Ниже ${warning}% система только предупреждает о нехватке места. Автоосвобождение выключено; ниже ${critical}% запись может быть приостановлена для защиты диска, но это не разрешает удаление без явного включения.`;
}

export function topReasonEntries(summary, limit = 5) {
  const counts = summary?.reason_counts || summary?.skipped_reason_counts || summary?.item_reason_counts || {};
  return Object.entries(counts)
    .filter(([, value]) => Number(value) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, limit);
}

export function cameraStorageRows(rows, limit = 12) {
  return (Array.isArray(rows) ? rows : [])
    .map((row) => ({
      camera_id: row?.camera_id,
      camera_name: row?.camera_name || `Camera ${row?.camera_id || "-"}`,
      size_bytes: asNumber(row?.size_bytes, 0),
      segment_count: asNumber(row?.segment_count, 0),
      existing_file_count: asNumber(row?.existing_file_count, 0),
      missing_file_count: asNumber(row?.missing_file_count, 0),
      problem_file_count: asNumber(row?.problem_file_count, 0),
      oldest_recording_at: row?.oldest_recording_at || null,
      newest_recording_at: row?.newest_recording_at || null,
    }))
    .sort((a, b) => b.size_bytes - a.size_bytes)
    .slice(0, limit);
}
