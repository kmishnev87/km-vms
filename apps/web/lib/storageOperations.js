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

export function isStorageAccessDeniedError(error) {
  const status = Number(error?.status ?? error?.response?.status);
  if (status === 401 || status === 403) return true;

  const message = String(error?.message || error || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
  if (!message) return false;

  return [
    "http 401",
    "http 403",
    "forbidden",
    "not authenticated",
    "invalid token",
    "insufficient permissions",
    "permission denied",
    "права пользователя ограничены",
    "недостаточно прав",
  ].some((phrase) => message.includes(phrase));
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

export function accessRightsModel(pathHealth = {}, language = "ru") {
  const readable = pathHealth?.readable;
  const writable = pathHealth?.writable;
  const labels = {
    ru: {
      ok: "Права на чтение и запись: есть",
      writeLimited: "Чтение есть, запись недоступна",
      readLimited: "Чтение недоступно, запись есть",
      none: "Права на чтение и запись: нет",
      unknown: "Права на чтение и запись: не проверены",
    },
    en: {
      ok: "Read and write access: available",
      writeLimited: "Read is available, write is unavailable",
      readLimited: "Read is unavailable, write is available",
      none: "Read and write access: unavailable",
      unknown: "Read and write access: not checked",
    },
    "zh-CN": {
      ok: "读写权限：可用",
      writeLimited: "可读取，无法写入",
      readLimited: "无法读取，可写入",
      none: "读写权限：不可用",
      unknown: "读写权限：未检查",
    },
  };
  const text = labels[language] || labels.ru;
  if (readable === true && writable === true) return { status: "ok", tone: "ok", label: text.ok };
  if (readable === true && writable === false) return { status: "write_limited", tone: "error", label: text.writeLimited };
  if (readable === false && writable === true) return { status: "read_limited", tone: "error", label: text.readLimited };
  if (readable === false && writable === false) return { status: "none", tone: "error", label: text.none };
  return { status: "unknown", tone: "unknown", label: text.unknown };
}

export function freeSpaceTone(capacity = {}, policy = {}) {
  if (policy?.recording_suspended_by_low_disk || policy?.state === "critical") return "error";
  if (policy?.state === "cleanup_threshold" || policy?.state === "warning") return "warning";
  const freePercent = Number(capacity?.free_percent);
  const warning = Number(policy?.warning_threshold_percent ?? 10);
  const critical = Number(policy?.critical_threshold_percent ?? 1);
  if (Number.isFinite(freePercent) && Number.isFinite(critical) && freePercent <= critical) return "error";
  if (Number.isFinite(freePercent) && Number.isFinite(warning) && freePercent <= warning) return "warning";
  return "neutral";
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

const REVIEW_ONLY_RECONCILIATION_CLASSES = new Set(["orphan_file", "pre_metadata_km_vms_file", "legacy_archive_file"]);
const SAFE_METADATA_RECONCILIATION_CLASSES = new Set([
  "missing_file",
  "orphan_metadata",
  "zero_size_file",
  "partial_file",
  "corrupted_file",
  "stale_writing_segment",
  "invalid_path",
  "path_outside_storage",
  "unreadable_file",
]);

export function reconciliationClassLabel(code, labels = {}, language = "ru") {
  const known = {
    missing_file: { ru: "Записи без файлов", en: "Records without files", "zh-CN": "无文件的录像记录" },
    orphan_metadata: { ru: "Записи без файлов", en: "Metadata records without files", "zh-CN": "无文件的元数据记录" },
    orphan_file: { ru: "Файлы без метаданных", en: "Files without metadata", "zh-CN": "无元数据的文件" },
    pre_metadata_km_vms_file: { ru: "Файлы без метаданных", en: "Pre-metadata KM VMS files", "zh-CN": "无新元数据的 KM VMS 文件" },
    legacy_archive_file: { ru: "Старые архивные файлы", en: "Legacy archive files", "zh-CN": "旧归档文件" },
    invalid_path: { ru: "Некорректные пути", en: "Invalid paths", "zh-CN": "无效路径" },
    path_outside_storage: { ru: "Пути вне хранилища", en: "Paths outside storage", "zh-CN": "存储之外的路径" },
    zero_size_file: { ru: "Файлы нулевого размера", en: "Zero-size files", "zh-CN": "零大小文件" },
    partial_file: { ru: "Частичные файлы", en: "Partial files", "zh-CN": "部分文件" },
    corrupted_file: { ru: "Поврежденные файлы", en: "Corrupted files", "zh-CN": "损坏文件" },
    stale_writing_segment: { ru: "Зависшие записи", en: "Stale writing records", "zh-CN": "卡住的录像记录" },
    unreadable_file: { ru: "Файлы недоступны для чтения", en: "Unreadable files", "zh-CN": "无法读取的文件" },
    storage_unavailable: { ru: "Хранилище недоступно", en: "Storage unavailable", "zh-CN": "存储不可用" },
    skipped: { ru: "Пропущено", en: "Skipped", "zh-CN": "已跳过" },
  };
  const backendLabel = typeof labels?.[code] === "string" ? labels[code] : "";
  if (known[code]) return known[code][language] || known[code].ru;
  return backendLabel || statusLabel("unknown", language);
}

export function normalizeReconciliationSummary(source = {}, language = "ru") {
  const counts = {
    ...(source?.counts || {}),
    ...(source?.classification_counts || {}),
  };
  if (!Object.keys(counts).length) {
    if (source?.missing_file_count != null) counts.missing_file = Number(source.missing_file_count || 0);
    if (source?.orphan_file_count != null) counts.orphan_file = Number(source.orphan_file_count || 0);
    if (source?.invalid_path_count != null) counts.invalid_path = Number(source.invalid_path_count || 0);
    if (source?.path_outside_storage_count != null) counts.path_outside_storage = Number(source.path_outside_storage_count || 0);
  }
  const labels = source?.classification_labels_ru || {};
  const cleanup = source?.cleanup_candidates || source?.cleanup_candidates_summary || {};
  const cleanupCounts = cleanup?.classification_counts || {};
  const reviewOnlyCount = Number(cleanup.count ?? source?.cleanup_candidate_count ?? source?.cleanup_candidates_count ?? 0);
  const safeReasonCounts = source?.apply_safe_summary?.reason_counts || {};
  const safeFixCount = Number(source?.apply_safe_summary?.updated_metadata_count ?? source?.updated_metadata_count ?? 0);
  const safeProblemCount = Object.entries(counts)
    .filter(([key]) => SAFE_METADATA_RECONCILIATION_CLASSES.has(key))
    .reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const manualProblemCount = Object.entries(counts)
    .filter(([key]) => REVIEW_ONLY_RECONCILIATION_CLASSES.has(key))
    .reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const problemCount = Object.entries(counts)
    .filter(([key]) => key !== "ok_owned_finalized" && key !== "skipped")
    .reduce((sum, [, value]) => sum + Number(value || 0), 0);
  const categories = Object.entries({ ...counts, ...cleanupCounts, ...safeReasonCounts })
    .map(([code, value]) => ({ code, count: Number(value || 0), label: reconciliationClassLabel(code, labels, language) }))
    .filter((item) => item.count > 0 && item.code !== "ok_owned_finalized")
    .sort((a, b) => b.count - a.count)
    .slice(0, 6);
  return {
    status: source?.status || (problemCount ? "problems_found" : "ok"),
    problemCount,
    safeFixCount: safeFixCount || safeProblemCount,
    reviewOnlyCount,
    manualProblemCount,
    totalRows: Number(source?.total_metadata_rows_checked ?? source?.checked_count ?? 0),
    categories,
    counts,
    canApplySafe: Boolean((safeFixCount || safeProblemCount) > 0),
    hasReviewOnly: reviewOnlyCount > 0 || manualProblemCount > 0,
  };
}

export const STORAGE_ACTION_PERMISSIONS = Object.freeze({
  openStorage: "manage_settings",
  autoFreeSpace: "manage_settings",
  archiveRootDiscovery: "manage_settings",
  archiveRootCreate: "manage_settings",
  archiveRootActivate: "manage_settings",
  migrationPreview: "manage_settings",
  migrationApply: "manage_settings",
  reconciliationSummary: "run_diagnostics",
  reconciliationApply: "manage_settings",
  retentionPreview: "delete_recordings",
  retentionApply: "delete_recordings",
  settingsRead: "manage_settings",
});

export function userHasPermission(user, permission) {
  return Array.isArray(user?.permissions) && user.permissions.includes(permission);
}

export function storagePermissionReason(permission, language = "ru") {
  const labels = {
    manage_settings: {
      ru: "Недостаточно прав для управления настройками хранилища.",
      en: "Not enough permissions to manage storage settings.",
      "zh-CN": "没有足够权限管理存储设置。",
    },
    delete_recordings: {
      ru: "Недостаточно прав для удаления записей.",
      en: "Not enough permissions to delete recordings.",
      "zh-CN": "没有足够权限删除录像。",
    },
    run_diagnostics: {
      ru: "Недостаточно прав для запуска диагностики.",
      en: "Not enough permissions to run diagnostics.",
      "zh-CN": "没有足够权限运行诊断。",
    },
  };
  return labels[permission]?.[language] || labels[permission]?.ru || labels.manage_settings.ru;
}

export function actionPermissionState(user, permission, language = "ru") {
  const allowed = userHasPermission(user, permission);
  return {
    allowed,
    permission,
    reason: allowed ? "" : storagePermissionReason(permission, language),
  };
}

function reasonCode(reason) {
  return typeof reason === "string" ? reason : reason?.reason || reason?.code || reason?.error || "";
}

export function humanBlockerReason(reason, language = "ru") {
  const code = reasonCode(reason);
  const labels = {
    active_recording_jobs: {
      ru: "Перенос заблокирован: сейчас идет запись. Остановите запись на всех камерах или выполните перенос позже.",
      en: "Migration is blocked because recording is active. Stop recording on all cameras or run migration later.",
      "zh-CN": "迁移已阻止：当前正在录像。请停止所有摄像机录像或稍后再迁移。",
    },
    active_recording_jobs_block_root_switch: {
      ru: "Переключение корня архива заблокировано активной записью. Остановите запись или дождитесь завершения текущей операции.",
      en: "Archive root switch is blocked by active recording. Stop recording or wait for the current operation to finish.",
      "zh-CN": "活动录像阻止切换归档根目录。请停止录像或等待当前操作完成。",
    },
    archive_root_not_writable_or_namespace_missing: {
      ru: "Корень архива не готов: нет записи или пространства kmvms/recordings. Проверьте путь и права NAS.",
      en: "Archive root is not ready: write access or kmvms/recordings namespace is missing. Check NAS path and permissions.",
      "zh-CN": "归档根目录未就绪：缺少写入权限或 kmvms/recordings 命名空间。请检查 NAS 路径和权限。",
    },
    archive_migration_apply_requires_confirm: {
      ru: "Применение переноса требует явного подтверждения.",
      en: "Migration apply requires explicit confirmation.",
      "zh-CN": "应用迁移需要明确确认。",
    },
    recording_format_change_blocked_active_recordings: {
      ru: "Изменение формата записи заблокировано активной записью.",
      en: "Recording format change is blocked by active recording.",
      "zh-CN": "活动录像阻止更改录像格式。",
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
    archive_root_overlap: {
      ru: "Корни архива пересекаются. Выберите отдельную папку архива.",
      en: "Archive roots overlap. Choose a separate archive folder.",
      "zh-CN": "归档根目录重叠。请选择独立的归档文件夹。",
    },
    archive_root_missing: {
      ru: "Корень архива не найден",
      en: "Archive root is missing",
      "zh-CN": "找不到归档根目录",
    },
    archive_root_outside_approved_storage_base: {
      ru: "Путь корня архива вне разрешенной области хранения.",
      en: "Archive root path is outside the approved storage base.",
      "zh-CN": "归档根路径位于允许的存储范围之外。",
    },
    target_root_missing: {
      ru: "Целевой корень архива не выбран или недоступен.",
      en: "Target archive root is missing or unavailable.",
      "zh-CN": "目标归档根目录缺失或不可用。",
    },
    path_outside_archive_root: {
      ru: "Путь записи вне корня архива. Нужна ручная проверка метаданных.",
      en: "Recording path is outside the archive root. Manual metadata review is required.",
      "zh-CN": "录像路径位于归档根目录之外。需要手动检查元数据。",
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
    storage_root_missing: {
      ru: "Корень хранилища отсутствует. Проверьте подключение и путь NAS.",
      en: "Storage root is missing. Check NAS mount and path.",
      "zh-CN": "存储根目录缺失。请检查 NAS 挂载和路径。",
    },
    storage_root_not_directory: {
      ru: "Корень хранилища не является папкой.",
      en: "Storage root is not a directory.",
      "zh-CN": "存储根目录不是文件夹。",
    },
    storage_root_not_readable: {
      ru: "Корень хранилища недоступен для чтения.",
      en: "Storage root is not readable.",
      "zh-CN": "存储根目录不可读。",
    },
    storage_root_not_writable: {
      ru: "Корень хранилища недоступен для записи.",
      en: "Storage root is not writable.",
      "zh-CN": "存储根目录不可写。",
    },
    insufficient_target_free_space: {
      ru: "На целевом корне недостаточно свободного места для переноса.",
      en: "Target root does not have enough free space for migration.",
      "zh-CN": "目标根目录没有足够可用空间执行迁移。",
    },
    stale_or_tampered_plan: {
      ru: "План переноса устарел или изменен. Обновите проверку и повторите подтверждение.",
      en: "Migration plan is stale or changed. Refresh the check and confirm again.",
      "zh-CN": "迁移计划已过期或已更改。请刷新检查并重新确认。",
    },
    source_file_changed_after_plan: {
      ru: "Исходный файл изменился после предпросмотра. Обновите проверку перед повтором.",
      en: "Source file changed after preview. Refresh the check before retrying.",
      "zh-CN": "源文件在预览后发生变化。重试前请刷新检查。",
    },
    source_changed_during_copy: {
      ru: "Исходный файл изменился во время копирования. Исходник сохранен, нужна ручная проверка.",
      en: "Source file changed during copy. Source is preserved; manual review is required.",
      "zh-CN": "源文件在复制期间发生变化。源文件已保留，需要手动检查。",
    },
    target_collision: {
      ru: "В целевом корне уже есть файл с таким путем. Нужна ручная проверка.",
      en: "Target root already has a file at that path. Manual review is required.",
      "zh-CN": "目标根目录中已有同路径文件。需要手动检查。",
    },
    temp_target_collision: {
      ru: "Не удалось создать временный файл переноса. Повторите после проверки целевого корня.",
      en: "Could not create migration temporary file. Retry after checking the target root.",
      "zh-CN": "无法创建迁移临时文件。检查目标根后重试。",
    },
    plan_item_missing_after_validation: {
      ru: "Элемент плана исчез после проверки. Обновите проверку и повторите.",
      en: "Plan item disappeared after validation. Refresh the check and retry.",
      "zh-CN": "计划项在验证后消失。请刷新检查并重试。",
    },
    permission_denied: {
      ru: "Недостаточно прав для действия.",
      en: "Not enough permissions for this action.",
      "zh-CN": "没有足够权限执行此操作。",
    },
    unavailable: {
      ru: "Состояние недоступно. Обновите проверку или откройте диагностику.",
      en: "State is unavailable. Refresh the check or open diagnostics.",
      "zh-CN": "状态不可用。请刷新检查或打开诊断。",
    },
  };
  return labels[code]?.[language] || labels[code]?.ru || statusLabel("unknown", language);
}

export function storageTopHealthModel({ operations = {}, pathHealth = {}, capacity = {}, policy = {}, reconciliation = {}, migrationPreview = {}, retention = {} } = {}, language = "ru") {
  const labels = {
    ru: {
      unavailable: "Корень архива недоступен. Проверьте подключение NAS, путь и права доступа.",
      unwritable: "Проверьте права записи в архив.",
      unreadable: "Проверьте права чтения архива.",
      ambiguousAvailability: "Корень архива требует проверки: чтение и запись доступны, но общая проверка архива не подтверждена. Проверьте путь, служебную папку архива и права.",
      lowDisk: "Освободите место или проверьте регламент хранения.",
      integrity: "Запустите проверку целостности и разберите найденные проблемы.",
      retention: "Сделайте предпросмотр регламента хранения перед удалением.",
      migration: "Перенос заблокирован активной записью; выполните его позже.",
      stale: "Обновите состояние хранилища перед действиями.",
      unknown: "Дождитесь проверки состояния: не хватает фактов для безопасного действия.",
      ok: "Немедленных действий не требуется.",
    },
    en: {
      unavailable: "Archive root is unavailable. Check NAS mount, path, and permissions.",
      unwritable: "Check archive write permissions.",
      unreadable: "Check archive read permissions.",
      ambiguousAvailability: "Archive root needs verification: read and write checks pass, but the overall archive check is not confirmed. Check the path, archive service folder, and permissions.",
      lowDisk: "Free space or review retention policy.",
      integrity: "Run integrity check and review detected problems.",
      retention: "Preview retention before deleting anything.",
      migration: "Migration is blocked by active recording; run it later.",
      stale: "Refresh storage state before actions.",
      unknown: "Wait for storage checks: facts are incomplete.",
      ok: "No immediate action is required.",
    },
    "zh-CN": {
      unavailable: "归档根目录不可用。请检查 NAS 挂载、路径和权限。",
      unwritable: "请检查归档写入权限。",
      unreadable: "请检查归档读取权限。",
      ambiguousAvailability: "归档根目录需要验证：读写检查通过，但整体归档检查未确认。请检查路径、归档服务文件夹和权限。",
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
  const hasActiveRecordingBlocker = blockers.some((item) => reasonCode(item) === "active_recording_jobs");
  let status = "ok";
  let tone = "ok";
  let reason = text.ok;
  let nextStep = text.ok;
  if (pathHealth?.available == null || pathHealth?.readable == null || pathHealth?.writable == null) {
    status = "unknown";
    tone = "unknown";
    reason = text.unknown;
    nextStep = text.unknown;
  } else if (pathHealth?.readable === true && pathHealth?.writable === true && pathHealth?.available === false) {
    status = "availability_unconfirmed";
    tone = "warning";
    reason = text.ambiguousAvailability;
    nextStep = text.ambiguousAvailability;
  } else if (operations?.status === "unavailable" || pathHealth?.available === false) {
    status = "unavailable";
    tone = "error";
    reason = text.unavailable;
    nextStep = text.unavailable;
  } else if (pathHealth?.readable === false) {
    status = "unreadable";
    tone = "error";
    reason = text.unreadable;
    nextStep = text.unreadable;
  } else if (pathHealth?.writable === false) {
    status = "unwritable";
    tone = "error";
    reason = text.unwritable;
    nextStep = text.unwritable;
  } else if (policy?.recording_suspended_by_low_disk || policy?.state === "critical" || policy?.state === "cleanup_threshold" || Number(capacity?.free_percent) <= Number(policy?.warning_threshold_percent ?? 10)) {
    status = "low_disk";
    tone = policy?.state === "critical" || policy?.recording_suspended_by_low_disk ? "error" : "warning";
    reason = text.lowDisk;
    nextStep = text.lowDisk;
  } else if (Number(reconciliation?.problem_file_count || 0) > 0 || Number(reconciliation?.cleanup_candidate_count || 0) > 0) {
    status = "reconciliation";
    tone = "warning";
    reason = text.integrity;
    nextStep = text.integrity;
  } else if (retention?.last_status === "failed") {
    status = "retention_failed";
    tone = "warning";
    reason = text.retention;
    nextStep = text.retention;
  } else if (hasActiveRecordingBlocker) {
    status = "migration_blocked";
    tone = "warning";
    reason = humanBlockerReason("active_recording_jobs", language);
    nextStep = text.migration;
  } else if (operations?.stale || operations?.status === "stale") {
    status = "stale";
    tone = "unknown";
    reason = text.stale;
    nextStep = text.stale;
  } else if (!operations?.status || !capacity?.total_bytes) {
    status = "unknown";
    tone = "unknown";
    reason = text.unknown;
    nextStep = text.unknown;
  }
  return { status, tone, reason, nextStep };
}

export function primaryStorageActionText(args = {}, language = "ru") {
  return storageTopHealthModel(args, language).nextStep;
}

export function retentionScenarioModel({ preview = null, result = null, retention = {}, permission = { allowed: true }, running = false } = {}, language = "ru") {
  const source = preview || result || retention?.last_summary || null;
  const count = Number(source?.planned_count ?? source?.deleted_count ?? 0);
  const bytes = Number(source?.estimated_freed_bytes ?? source?.bytes_freed ?? 0);
  const failed = retention?.last_status === "failed";
  return {
    status: !permission.allowed ? "unavailable_due_to_permissions" : running ? "running" : result ? "apply_completed" : preview ? "preview_completed" : failed ? "apply_failed" : "idle",
    permissionReason: permission.allowed ? "" : permission.reason,
    hasPreview: Boolean(preview),
    canPreview: Boolean(permission.allowed && !running),
    canApply: Boolean(permission.allowed && preview && count > 0 && !running),
    plannedCount: count,
    plannedBytes: bytes,
    failedReason: failed ? humanBlockerReason(retention?.last_error || "unavailable", language) : "",
  };
}

export function reconciliationScenarioModel({ preview = null, result = null, reconciliation = {}, canCheck = { allowed: true }, canApply = { allowed: true }, running = false } = {}, language = "ru") {
  const source = preview || result || reconciliation || {};
  const normalized = normalizeReconciliationSummary(source, language);
  return {
    status: !canCheck.allowed ? "unavailable_due_to_permissions" : running ? "running" : result ? "apply_completed" : preview ? "preview_completed" : normalized.problemCount || normalized.reviewOnlyCount ? "check_needed" : "idle",
    checkPermissionReason: canCheck.allowed ? "" : canCheck.reason,
    applyPermissionReason: canApply.allowed ? "" : canApply.reason,
    canCheck: Boolean(canCheck.allowed && !running),
    canApply: Boolean(canApply.allowed && preview && normalized.canApplySafe && !running),
    problemCount: normalized.problemCount,
    cleanupCount: normalized.reviewOnlyCount,
    safeFixCount: normalized.safeFixCount,
    manualProblemCount: normalized.manualProblemCount,
    categories: normalized.categories,
    totalRows: normalized.totalRows,
    noAutoFixReason: preview && !normalized.canApplySafe && normalized.problemCount ? "review_only" : "",
    scope: "metadata_status_safe_repair",
    failedReason: result?.status === "failed" ? humanBlockerReason(result?.reason || "unavailable", language) : "",
  };
}

export function migrationScenarioModel({ preview = {}, result = null, permission = { allowed: true }, running = false } = {}, language = "ru") {
  const blockers = Array.isArray(preview?.blockers) ? preview.blockers : [];
  const resultBlockers = Array.isArray(result?.blockers) ? result.blockers : [];
  const blockerText = (resultBlockers.length ? resultBlockers : blockers).map((item) => humanBlockerReason(item, language));
  const failed = result?.status === "failed";
  const blocked = Boolean(blockers.length || result?.status === "blocked");
  return {
    status: !permission.allowed ? "unavailable_due_to_permissions" : running ? "running" : failed ? "apply_failed" : result?.status === "completed" ? "apply_completed" : blocked ? "apply_blocked" : preview?.apply_available ? "preview_completed" : "idle",
    permissionReason: permission.allowed ? "" : permission.reason,
    canPreview: Boolean(permission.allowed && !running),
    canApply: Boolean(permission.allowed && preview?.apply_available && !running && !blockers.length),
    targetRootId: preview?.target_root_id || result?.target_root_id || null,
    targetLabel: preview?.target_label || result?.target_label || "",
    blockerReason: Array.from(new Set(blockerText)).join(" ") || "",
    sourcePreserved: preview?.source_preserved ?? result?.source_preserved ?? true,
    cleanupPending: preview?.cleanup_pending ?? result?.cleanup_pending ?? false,
    manualReviewRequired: Boolean(failed || result?.cleanup_pending || result?.status === "blocked"),
  };
}

export function archiveRootScenarioModel({ root = null, permission = { allowed: true }, running = false } = {}, language = "ru") {
  const hasPath = Boolean(root?.configured_path || root?.path || root?.root_path || root?.display_path || root?.label);
  const problem = root?.problem || "";
  const canRuntimeActivate = Boolean(root?.requires_activation && !root?.is_active && problem === "root_missing");
  const blockedByProblem = Boolean(problem && !canRuntimeActivate);
  return {
    status: !permission.allowed ? "unavailable_due_to_permissions" : running ? "running" : root?.is_active ? "active" : blockedByProblem ? "blocked" : !hasPath ? "blocked" : root?.is_available === false ? "check_needed" : "idle",
    permissionReason: permission.allowed ? "" : permission.reason,
    canActivate: Boolean(permission.allowed && root && !root.is_active && !blockedByProblem && hasPath && !running),
    reason: problem ? humanBlockerReason(problem, language) : !hasPath ? humanBlockerReason("archive_root_missing", language) : "",
  };
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
