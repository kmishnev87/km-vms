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

export function factLabel(value, language = "ru") {
  if (value === true) return boolLabel(true, language);
  if (value === false) return boolLabel(false, language);
  if (value === "permission_limited" || value === "limited") {
    if (language === "en") return "Limited";
    if (language === "zh-CN") return "受限";
    return "Ограничено";
  }
  if (value === "unavailable") {
    if (language === "en") return "Unavailable";
    if (language === "zh-CN") return "不可用";
    return "Недоступно";
  }
  if (language === "en") return "Unknown";
  if (language === "zh-CN") return "未知";
  return "Не проверено";
}

export function factTone(value) {
  if (value === true) return "ok";
  if (value === false || value === "unavailable") return "error";
  if (value === "permission_limited" || value === "limited") return "warning";
  return "unknown";
}

export function policyStateLabel(policy, language = "ru") {
  const enabled = Boolean(policy?.auto_free_space_cleanup_enabled);
  if (language === "en") return enabled ? "ON" : "OFF";
  if (language === "zh-CN") return enabled ? "开启" : "关闭";
  return enabled ? "Включено" : "Выключено";
}

export function lowDiskPolicyText(policy, language = "ru") {
  const cleanupEnabled = Boolean(policy?.auto_free_space_cleanup_enabled);
  const warning = policy?.warning_threshold_percent ?? 10;
  const cleanup = policy?.cleanup_threshold_percent ?? 5;
  const critical = policy?.critical_threshold_percent ?? 1;
  if (language === "en") {
    return cleanupEnabled
      ? `Warning below ${warning}% free. Below ${cleanup}% the system may delete only KM VMS-owned recordings confirmed by metadata safety checks. Below ${critical}% recording may be suspended.`
      : `Warning below ${warning}% free. Automatic deletion is OFF; below ${critical}% recording may still be suspended to protect the disk.`;
  }
  if (language === "zh-CN") {
    return cleanupEnabled
      ? `可用空间低于 ${warning}% 时显示告警。低于 ${cleanup}% 时，系统只能删除属于 KM VMS 且已通过元数据安全检查的旧录像。低于 ${critical}% 时可暂停录像以保护磁盘。`
      : `可用空间低于 ${warning}% 时仅显示告警。自动删除已关闭；低于 ${critical}% 时仍可暂停录像以保护磁盘。`;
  }
  return cleanupEnabled
    ? `Ниже ${warning}% система предупреждает о нехватке места. Ниже ${cleanup}% она может автоматически удалить только старые записи, принадлежащие KM VMS и безопасно подтвержденные метаданными. Ниже ${critical}% запись может быть приостановлена для защиты диска.`
    : `Ниже ${warning}% система только предупреждает о нехватке места. Автоосвобождение выключено; ниже ${critical}% запись может быть приостановлена для защиты диска, но это не разрешает удаление без явного включения.`;
}

export function topReasonEntries(summary, limit = 5) {
  const counts = summary?.reason_counts || summary?.skipped_reason_counts || summary?.item_reason_counts || {};
  return Object.entries(counts)
    .filter(([, value]) => Number(value) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]))
    .slice(0, limit);
}

export function humanBlockerReason(reason, language = "ru") {
  const code = typeof reason === "string" ? reason : reason?.reason || reason?.code || "";
  const labels = {
    active_recording_jobs: {
      ru: "Перенос заблокирован: сейчас идет запись. Остановите запись на всех камерах или выполните перенос позже.",
      en: "Migration is blocked because recording is active. Stop recording on all cameras or run migration later.",
      "zh-CN": "迁移已阻止：当前正在录像。请停止所有摄像机录像或稍后再迁移。",
    },
    missing_file: {
      ru: "Файл отсутствует",
      en: "Missing file",
      "zh-CN": "文件缺失",
    },
    orphan_file: {
      ru: "Файл без метаданных KM VMS",
      en: "File without KM VMS metadata",
      "zh-CN": "文件没有 KM VMS 元数据",
    },
    invalid_path: {
      ru: "Некорректный путь",
      en: "Invalid path",
      "zh-CN": "路径无效",
    },
    path_outside_storage: {
      ru: "Путь вне безопасного хранилища",
      en: "Path outside safe storage",
      "zh-CN": "路径在安全存储之外",
    },
    namespace_missing: {
      ru: "Не создана папка пространства KM VMS: kmvms/recordings",
      en: "KM VMS namespace folder is missing: kmvms/recordings",
      "zh-CN": "缺少 KM VMS 命名空间文件夹：kmvms/recordings",
    },
    archive_root_namespace_mismatch: {
      ru: "Корень архива относится к другому пространству хранения",
      en: "Archive root belongs to a different storage namespace",
      "zh-CN": "归档根目录属于不同的存储命名空间",
    },
    archive_root_unavailable: {
      ru: "Корень архива недоступен",
      en: "Archive root is unavailable",
      "zh-CN": "归档根目录不可用",
    },
    archive_root_not_writable: {
      ru: "Корень архива недоступен для записи",
      en: "Archive root is not writable",
      "zh-CN": "归档根目录不可写",
    },
    archive_root_missing: {
      ru: "Корень архива не найден",
      en: "Archive root is missing",
      "zh-CN": "找不到归档根目录",
    },
    root_missing: {
      ru: "Путь архива не найден",
      en: "Archive path is missing",
      "zh-CN": "找不到归档路径",
    },
    root_not_directory: {
      ru: "Путь архива не является папкой",
      en: "Archive path is not a directory",
      "zh-CN": "归档路径不是目录",
    },
    root_not_readable: {
      ru: "Путь архива недоступен для чтения",
      en: "Archive path is not readable",
      "zh-CN": "归档路径不可读",
    },
  };
  return labels[code]?.[language] || labels[code]?.ru || (code ? String(code).replaceAll("_", " ") : statusLabel("unknown", language));
}

export function primaryStorageActionText({ operations = {}, pathHealth = {}, capacity = {}, policy = {}, reconciliation = {}, migrationPreview = {} } = {}, language = "ru") {
  const labels = {
    ru: {
      unavailable: "Сначала восстановите доступность хранилища.",
      unwritable: "Проверьте права записи в архив.",
      unreadable: "Проверьте права чтения архива.",
      lowDisk: "Освободите место или проверьте регламент хранения.",
      integrity: "Запустите проверку целостности и разберите найденные проблемы.",
      retention: "Сделайте предпросмотр регламента хранения перед удалением.",
      migration: "Перенос заблокирован активной записью; выполните его позже.",
      stale: "Обновите состояние хранилища перед действиями.",
      unknown: "Дождитесь проверки состояния: не хватает фактов для безопасного действия.",
      ok: "Немедленных действий не требуется.",
    },
    en: {
      unavailable: "Restore storage availability first.",
      unwritable: "Check archive write permissions.",
      unreadable: "Check archive read permissions.",
      lowDisk: "Free space or review retention policy.",
      integrity: "Run integrity check and review detected problems.",
      retention: "Preview retention before deleting anything.",
      migration: "Migration is blocked by active recording; run it later.",
      stale: "Refresh storage state before actions.",
      unknown: "Wait for storage checks: facts are incomplete.",
      ok: "No immediate action is required.",
    },
    "zh-CN": {
      unavailable: "请先恢复存储可用性。",
      unwritable: "请检查归档写入权限。",
      unreadable: "请检查归档读取权限。",
      lowDisk: "请释放空间或检查保留策略。",
      integrity: "请运行完整性检查并处理发现的问题。",
      retention: "删除前请先预览保留规则。",
      migration: "迁移被活动录像阻止；请稍后执行。",
      stale: "操作前请刷新存储状态。",
      unknown: "等待存储检查完成：安全操作所需事实不完整。",
      ok: "当前无需立即操作。",
    },
  };
  const text = labels[language] || labels.ru;
  const blockers = Array.isArray(migrationPreview?.blockers) ? migrationPreview.blockers : [];
  const hasActiveRecordingBlocker = blockers.some((item) => (typeof item === "string" ? item : item?.reason || item?.code) === "active_recording_jobs");
  if (operations?.status === "unavailable" || pathHealth?.available === false) return text.unavailable;
  if (pathHealth?.writable === false) return text.unwritable;
  if (pathHealth?.readable === false) return text.unreadable;
  if (policy?.recording_suspended_by_low_disk || policy?.state === "critical" || policy?.state === "cleanup_threshold" || Number(capacity?.free_percent) <= Number(policy?.warning_threshold_percent ?? 10)) return text.lowDisk;
  if (Number(reconciliation?.problem_file_count || 0) > 0 || Number(reconciliation?.cleanup_candidate_count || 0) > 0) return text.integrity;
  if (policy?.state === "warning" || operations?.status === "warning" || operations?.status === "degraded") return text.retention;
  if (hasActiveRecordingBlocker) return text.migration;
  if (operations?.stale || operations?.status === "stale") return text.stale;
  if (!operations?.status || !capacity?.total_bytes || pathHealth?.readable == null || pathHealth?.writable == null) return text.unknown;
  return text.ok;
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
