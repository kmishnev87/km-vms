const DOMAIN_PRIORITY = {
  storage: 0,
  recorder: 1,
  cameras: 2,
  live: 3,
  retention: 4,
  reconciliation: 5,
};

const SEVERITY_PRIORITY = {
  error: 0,
  warning: 1,
  info: 2,
  ok: 3,
  unknown: 4,
};

const MAX_BANNERS = 8;
const MAX_DRILLDOWN_ROWS = 6;
const RUNTIME_STATUS_PERMISSION = "run_diagnostics";

export const DOMAIN_LABELS_RU = {
  cameras: "Камеры",
  live: "Онлайн",
  recorder: "Запись",
  storage: "Хранилище",
  retention: "Хранение",
  reconciliation: "Целостность архива",
};

const SEVERITY_LABELS_RU = {
  error: "Ошибка",
  warning: "Предупреждение",
  info: "Информация",
  ok: "Работает",
  unknown: "Нет данных",
};

const ACTIONS = {
  cameras: {
    href: "/cameras",
    label: "Открыть камеры",
    hint: "Проверьте настройки камер, сеть и параметры записи.",
  },
  live: {
    href: "/live",
    label: "Открыть онлайн",
    hint: "Проверьте активный онлайн-просмотр и доступность камеры.",
  },
  recorder: {
    href: "/diagnostics",
    label: "Открыть диагностику",
    hint: "Откройте диагностику и соберите архив только явным действием.",
  },
  storage: {
    href: "/storage",
    label: "Открыть хранилище",
    hint: "Проверьте настройки хранилища и доступность архива.",
  },
  retention: {
    href: "/storage",
    label: "Открыть хранение",
    hint: "Проверьте правила хранения и состояние обслуживания архива.",
  },
  reconciliation: {
    href: "/storage",
    label: "Открыть хранилище",
    hint: "Проверьте целостность архива через существующий раздел хранилища.",
  },
};

export function userCanReadRuntimeStatus(user) {
  return Array.isArray(user?.permissions) && user.permissions.includes(RUNTIME_STATUS_PERMISSION);
}

export function isRuntimeStatusAccessDenied(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return (
    message.includes("401") ||
    message.includes("403") ||
    message.includes("not authenticated") ||
    message.includes("invalid token") ||
    message.includes("forbidden") ||
    message.includes("permission") ||
    message.includes("недостат") ||
    message.includes("доступ")
  );
}

export function shouldStopRuntimeStatusPolling(error) {
  return isRuntimeStatusAccessDenied(error);
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function countFrom(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function domainLabel(domain) {
  return DOMAIN_LABELS_RU[domain] || "Система";
}

function severityLabel(severity) {
  return SEVERITY_LABELS_RU[severity] || SEVERITY_LABELS_RU.unknown;
}

function actionFor(domain) {
  return ACTIONS[domain] || null;
}

function pushBanner(items, banner) {
  if (!banner?.id || !banner?.domain || !banner?.severity) return;
  const action = actionFor(banner.domain);
  items.push({
    affected_count: 0,
    reason_codes: [],
    action_hint: action?.hint || "",
    action: action || null,
    domain_label: domainLabel(banner.domain),
    severity_label: severityLabel(banner.severity),
    ...banner,
  });
}

function sortAndBound(items, limit = MAX_BANNERS) {
  return items
    .sort((left, right) => {
      const severity = (SEVERITY_PRIORITY[left.severity] ?? 9) - (SEVERITY_PRIORITY[right.severity] ?? 9);
      if (severity) return severity;
      const domain = (DOMAIN_PRIORITY[left.domain] ?? 9) - (DOMAIN_PRIORITY[right.domain] ?? 9);
      if (domain) return domain;
      return String(left.id).localeCompare(String(right.id));
    })
    .slice(0, limit);
}

function storageWarnings(domain, items) {
  const reasons = asArray(domain.reason_codes);
  if (domain.severity === "ok") return;
  if (reasons.includes("storage_unavailable") || domain.available === false) {
    pushBanner(items, {
      id: "storage-unavailable",
      domain: "storage",
      severity: "error",
      title: "Хранилище недоступно",
      message: "Архив сейчас недоступен для записи или чтения.",
      reason_codes: reasons,
      action_hint: "Проверьте настройки хранилища и доступность папки архива.",
    });
    return;
  }
  if (reasons.includes("storage_unwritable") || domain.writable === false) {
    pushBanner(items, {
      id: "storage-unwritable",
      domain: "storage",
      severity: "error",
      title: "Хранилище недоступно для записи",
      message: "Новые записи могут не сохраняться в архив.",
      reason_codes: reasons,
      action_hint: "Проверьте права записи и свободное место.",
    });
  }
  if (reasons.includes("storage_unreadable") || domain.readable === false) {
    pushBanner(items, {
      id: "storage-unreadable",
      domain: "storage",
      severity: "warning",
      title: "Хранилище недоступно для чтения",
      message: "Просмотр архива может быть недоступен.",
      reason_codes: reasons,
      action_hint: "Проверьте подключение и права на папку архива.",
    });
  }
  if (reasons.includes("storage_low_space")) {
    pushBanner(items, {
      id: "storage-low-space",
      domain: "storage",
      severity: "warning",
      title: "Заканчивается место в архиве",
      message: "Свободное место ниже безопасного порога.",
      reason_codes: reasons,
      action_hint: "Проверьте правила хранения и настройки хранилища.",
    });
  }
}

function recorderWarnings(domain, items) {
  const reasons = asArray(domain.safe_reason_codes || domain.reason_codes);
  if (domain.severity === "ok") return;
  if (reasons.includes("recorder_heartbeat_stale") || domain.severity === "error") {
    pushBanner(items, {
      id: "recorder-stale",
      domain: "recorder",
      severity: "error",
      title: "Сервис записи не подтверждает работу",
      message: "Сервис записи не прислал актуальный heartbeat или сообщил об ошибке.",
      reason_codes: reasons,
      action_hint: "Проверьте сервис записи и состояние текущих задач.",
    });
    return;
  }
  if (reasons.includes("recording_failed") || domain.severity === "warning") {
    pushBanner(items, {
      id: "recorder-warning",
      domain: "recorder",
      severity: "warning",
      title: "Запись работает с предупреждениями",
      message: "Часть записей может быть в состоянии retry или failed.",
      reason_codes: reasons,
      action_hint: "Проверьте состояние записи камер.",
    });
  }
}

function cameraWarnings(domain, items) {
  const cameraItems = asArray(domain.items).filter((item) => {
    const reasons = asArray(item.reason_codes);
    if (reasons.includes("disabled") || reasons.includes("not_applicable")) return false;
    return ["warning", "error"].includes(item.severity);
  });
  const failed = cameraItems.filter((item) => item.severity === "error");
  const warning = cameraItems.filter((item) => item.severity === "warning");
  if (failed.length) {
    pushBanner(items, {
      id: "camera-recording-errors",
      domain: "cameras",
      severity: "error",
      title: "Есть камеры с ошибкой записи",
      message: `${failed.length} камер требуют проверки записи или доступности сети.`,
      affected_count: failed.length,
      reason_codes: Array.from(new Set(failed.flatMap((item) => asArray(item.reason_codes)))),
      action_hint: "Проверьте сеть камеры и состояние записи.",
    });
  }
  if (warning.length) {
    pushBanner(items, {
      id: "camera-recording-warnings",
      domain: "cameras",
      severity: "warning",
      title: "Есть предупреждения по записи камер",
      message: `${warning.length} камер без актуального подтверждения записи или со stale-сегментами.`,
      affected_count: warning.length,
      reason_codes: Array.from(new Set(warning.flatMap((item) => asArray(item.reason_codes)))),
      action_hint: "Проверьте режим записи и последние сегменты.",
    });
  }
}

function liveWarnings(domain, items) {
  const liveItems = asArray(domain.items).filter((item) => {
    const reasons = asArray(item.reason_codes);
    if (reasons.includes("not_applicable") || reasons.includes("not_requested")) return false;
    if (!item.running && !item.ready && item.state === "unknown" && reasons.includes("no_evidence")) return false;
    return ["warning", "error"].includes(item.severity);
  });
  const failed = liveItems.filter((item) => item.severity === "error");
  const starting = liveItems.filter((item) => item.severity === "warning");
  if (failed.length) {
    pushBanner(items, {
      id: "live-stream-errors",
      domain: "live",
      severity: "error",
      title: "Онлайн-поток недоступен",
      message: `${failed.length} активных онлайн-потоков завершились ошибкой или недоступны.`,
      affected_count: failed.length,
      reason_codes: Array.from(new Set(failed.flatMap((item) => asArray(item.reason_codes)))),
      action_hint: "Проверьте онлайн-просмотр и доступность камеры.",
    });
  }
  if (starting.length) {
    pushBanner(items, {
      id: "live-stream-starting",
      domain: "live",
      severity: "warning",
      title: "Онлайн-поток долго запускается",
      message: `${starting.length} активных онлайн-потоков ещё не готовы к просмотру.`,
      affected_count: starting.length,
      reason_codes: Array.from(new Set(starting.flatMap((item) => asArray(item.reason_codes)))),
      action_hint: "Проверьте онлайн-просмотр, если поток не появится.",
    });
  }
}

function retentionWarnings(domain, items) {
  const reasons = asArray(domain.reason_codes);
  if (domain.severity === "error" || reasons.includes("retention_failed")) {
    pushBanner(items, {
      id: "retention-failed",
      domain: "retention",
      severity: "error",
      title: "Обслуживание хранения завершилось ошибкой",
      message: "Автоматическое обслуживание архива не смогло завершиться штатно.",
      reason_codes: reasons,
      action_hint: "Проверьте правила хранения и состояние архива.",
    });
    return;
  }
  if (domain.severity === "warning" || reasons.includes("retention_completed_with_warnings") || reasons.includes("retention_policy_risk")) {
    pushBanner(items, {
      id: "retention-warning",
      domain: "retention",
      severity: "warning",
      title: "Хранение требует внимания",
      message: "Последний запуск обслуживания завершился с предупреждениями или политика требует проверки.",
      reason_codes: reasons,
      action_hint: "Проверьте правила хранения записей.",
    });
  }
}

function reconciliationWarnings(domain, items) {
  const reasons = asArray(domain.reason_codes);
  if (domain.severity === "ok") return;
  if (reasons.includes("reconciliation_problems_found")) {
    pushBanner(items, {
      id: "reconciliation-problems",
      domain: "reconciliation",
      severity: domain.path_outside_storage_count > 0 ? "error" : "warning",
      title: "Найдены проблемы целостности архива",
      message: "Проверка архива нашла расхождения между метаданными и файлами.",
      affected_count: countFrom(domain.problem_file_count),
      reason_codes: reasons,
      action_hint: "Проверьте диагностику целостности архива перед действиями с архивом.",
    });
  }
  if (reasons.includes("cleanup_candidates_present")) {
    pushBanner(items, {
      id: "reconciliation-cleanup",
      domain: "reconciliation",
      severity: "warning",
      title: "Есть кандидаты на очистку архива",
      message: "Найдены безопасные кандидаты для последующей проверки очистки.",
      affected_count: countFrom(domain.cleanup_candidate_count),
      reason_codes: reasons,
      action_hint: "Проверьте список проблем архива в соответствующем workflow.",
    });
  }
}

export function buildOperatorWarnings(runtimeStatus, options = {}) {
  const domains = asObject(runtimeStatus?.domains);
  const include = new Set(options.domains || Object.keys(DOMAIN_PRIORITY));
  const items = [];

  if (include.has("storage")) storageWarnings(asObject(domains.storage), items);
  if (include.has("recorder")) recorderWarnings(asObject(domains.recorder), items);
  if (include.has("cameras")) cameraWarnings(asObject(domains.cameras), items);
  if (include.has("live")) liveWarnings(asObject(domains.live), items);
  if (include.has("retention")) retentionWarnings(asObject(domains.retention), items);
  if (include.has("reconciliation")) reconciliationWarnings(asObject(domains.reconciliation), items);

  return sortAndBound(items, options.limit || MAX_BANNERS);
}

function domainSeverity(domain) {
  const severity = String(asObject(domain).severity || "unknown");
  return severity in SEVERITY_PRIORITY ? severity : "unknown";
}

function isLiveDomainNeutral(domain) {
  const data = asObject(domain);
  const items = asArray(data.items);
  if (!items.length && ["ok", "unknown"].includes(domainSeverity(data))) return true;
  return items.every((item) => {
    const reasons = asArray(item.reason_codes);
    if (reasons.includes("not_applicable") || reasons.includes("not_requested")) return true;
    if (!item.running && !item.ready && item.state === "unknown" && reasons.includes("no_evidence")) return true;
    return item.severity === "ok";
  });
}

function effectiveDomainSeverity(domainName, domain) {
  if (domainName === "live" && isLiveDomainNeutral(domain)) return "ok";
  const severity = domainSeverity(domain);
  return severity === "info" ? "ok" : severity;
}

export function buildDashboardStatusSummary(runtimeStatus, options = {}) {
  const domains = asObject(runtimeStatus?.domains);
  const warnings = buildOperatorWarnings(runtimeStatus, { limit: options.limit || MAX_BANNERS });
  const rows = Object.keys(DOMAIN_PRIORITY).map((domainName) => {
    const domain = asObject(domains[domainName]);
    const severity = effectiveDomainSeverity(domainName, domain);
    const domainWarnings = warnings.filter((item) => item.domain === domainName);
    const affectedCount = domainWarnings.reduce(
      (sum, item) => sum + countFrom(item.affected_count || (item.severity !== "ok" ? 1 : 0)),
      0
    );
    return {
      domain: domainName,
      label: domainLabel(domainName),
      severity,
      severity_label: severityLabel(severity),
      affected_count: affectedCount,
      problem_count: domainWarnings.length,
      action: actionFor(domainName),
    };
  });
  const problemRows = rows.filter((row) => ["error", "warning"].includes(row.severity) || row.problem_count > 0);
  const globalSeverity = warnings.some((item) => item.severity === "error") || problemRows.some((row) => row.severity === "error")
    ? "error"
    : warnings.some((item) => item.severity === "warning") || problemRows.some((row) => row.severity === "warning")
      ? "warning"
      : "ok";

  return {
    severity: globalSeverity,
    severity_label: severityLabel(globalSeverity),
    title: globalSeverity === "ok" ? "Система работает штатно" : "Требуется внимание оператора",
    summary: globalSeverity === "ok"
      ? "Критичных предупреждений по камерам, записи, архиву и обслуживанию нет."
      : "Проблемы сгруппированы по доменам без технических подробностей и секретов.",
    problem_count: warnings.length,
    affected_domains: problemRows.length,
    rows,
    problems: warnings.slice(0, options.problemLimit || MAX_DRILLDOWN_ROWS),
    diagnostics_hint: "Журнал и диагностический архив доступны в разделе «Настройки» при наличии прав.",
  };
}
