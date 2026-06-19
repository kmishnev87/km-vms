"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "../../components/Layout";
import { apiFetch, apiFetchBlob, clearAuthToken, forbiddenMessage } from "../../lib/api";
import { LanguageSelect, normalizeLocale, persistLocale, translateText } from "../../lib/i18n";

import {
  AUDIT_CATEGORIES,
  AUDIT_LIMIT,
  AUDIT_SEVERITIES,
  HARDWARE_OPTIONS,
  MAINTENANCE_DRY_RUN_ENDPOINTS,
  UTC_TIMEZONES,
  auditLabel,
  auditMessage,
  auditTarget,
  backendLabel,
  configureSettingsPageHelpers,
  formatAuditTimestamp,
  hardwareOptionState,
  humanErrorText,
  languageOf,
  maintenanceDetailRows,
  maintenanceFlowRows,
  maintenanceStatusClass,
  maintenanceStatusText,
  passwordConfirmMessage,
  passwordHint,
  passwordLengthMessage,
  parseErrorDetail,
  payloadFromDraft,
  recordingFormatForProfile,
  roleLabel,
  roleOptionsFor,
  safeMetadataRows,
  samePayload,
  settingsDraftFromApi,
  sortedUsersForTable,
  timezoneValueForSettings,
  userCanBeDeleted,
  userCanBeManaged,
} from "../../lib/settingsPageHelpers";

configureSettingsPageHelpers({ normalizeLocale, translateText });

function settingsTextFor(lang) {
  if (TEXT[lang]) return TEXT[lang];
  if (lang !== "zh-CN") return TEXT.ru;
  const convert = (value) => {
    if (Array.isArray(value)) return value.map(convert);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, convert(child)]));
    }
    return typeof value === "string" ? translateText("zh-CN", value) : value;
  };
  return mergeText(convert(TEXT.ru), ZH_TEXT_OVERRIDES);
}

function mergeText(base, overrides) {
  const result = { ...(base || {}) };
  for (const [key, value] of Object.entries(overrides || {})) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      result[key] = mergeText(result[key], value);
    } else {
      result[key] = value;
    }
  }
  return result;
}

const TEXT = {
  ru: {
    title: "Настройки",
    subtitle: "Системные параметры KM VMS: язык, время, запись, ускорение, безопасность и обслуживание.",
    save: "Сохранить",
    saving: "Сохранение...",
    cancel: "Отменить",
    dirty: "Есть несохранённые изменения",
    checking: "Проверка...",
    systemName: "Имя системы",
    systemNameHelp: "Несекретное имя продукта для админских экранов.",
    language: "Язык",
    languageHelp: "Язык интерфейса KM VMS.",
    russian: "Русский",
    english: "English",
    timezone: "Часовой пояс",
    timezoneHelp: "Определяет время интерфейса, архива и хронологии.",
    recording: "Формат записи",
    compatibility: "Макс. совместимость",
    compatibilityHelp: "Сейчас используется MP4. Удобнее для плееров.",
    reliability: "Макс. надежность",
    reliabilityHelp: "Сейчас используется MKV. Лучше переносит сбои записи.",
    mapsTo: "Формат",
    hardware: "Аппаратное ускорение",
    hardwareAvailable: "Аппаратное ускорение доступно.",
    hardwareUnavailable: "Аппаратное ускорение недоступно. Будет использоваться CPU fallback.",
    selected: "Выбрано",
    unavailableMode: "Этот режим недоступен на данном сервере или не прошёл проверку.",
    rescan: "Обновить аппаратные возможности",
    failedValidation: "Не прошёл проверку",
    notDetected: "Не найдено на этом сервере",
    security: "Безопасность",
    securityText: "Журнал логирования, сбор диагностических логов и отчёт об ошибке.",
    maintenance: "Обслуживание",
    maintenanceText: "Статусы обновления, миграций, восстановления и диагностического отчёта без запуска действий применения.",
    maintenanceOverview: "Обзор обслуживания",
    maintenanceRefresh: "Обновить",
    maintenanceLoadError: "Обзор обслуживания недоступен.",
    maintenanceLimitedHistory: "Долговременная история ограничена: показаны текущий статус и последний безопасный отчёт.",
    maintenanceReport: "Отчёт обновления",
    maintenanceReportView: "Показать отчёт",
    maintenanceReportDownload: "Скачать JSON",
    maintenanceReportReady: "Санитизированный отчёт доступен.",
    maintenanceReportUnavailable: "Отчёт недоступен.",
    maintenanceDryRun: "Проверить",
    maintenanceDryRunResult: "Проверка выполнена.",
    maintenanceNoApply: "Применение не запускается с этого экрана.",
    maintenanceBackupRequired: "Требуется резервная копия",
    maintenanceBackupNotRequired: "Резервная копия не требуется",
    maintenanceConfirmationRequired: "Нужно подтверждение",
    maintenanceUnsupported: "Действие заблокировано или не поддерживается",
    maintenanceLastAction: "Последнее действие",
    maintenanceNoHistory: "История действий не найдена",
    maintenanceGeneratedAt: "Сформирован",
    maintenanceWarnings: "Предупреждения",
    updateApplyTitle: "Применение обновления",
    updateApplyCheck: "Проверить обновление",
    updateApplyStart: "Применить обновление",
    updateApplyConfirm: "Запустить обновление KM VMS? Система выполнит проверку, применит trusted release через helper и может временно перезапустить сервисы.",
    updateApplyQueued: "Запрос обновления передан helper.",
    updateApplyUnavailable: "Применение недоступно для текущего состояния.",
    updateApplyConnection: "Сервис может временно перезапускаться; опрос статуса продолжится автоматически.",
    updateCurrent: "Текущая",
    updateLatest: "Доступная",
    updateSource: "Источник",
    maintenanceLabels: {
      pending: "Ожидают",
      artifacts: "Артефакты",
      current: "Текущая версия",
      target: "Целевая версия",
      available: "Доступная версия",
      backup: "Резервная копия",
      confirm: "Подтверждение",
      apply: "Применение",
      reportId: "ID отчёта",
    },
    maintenanceFlows: {
      db_adoption: "Принятие БД",
      migration: "Миграции",
      restore: "Восстановление",
      update: "Обновление",
    },
    maintenanceStatuses: {
      ok: "OK",
      current: "Актуально",
      available: "Доступно",
      adoptable: "Можно принять",
      adopted: "Принято",
      already_adopted: "Уже принято",
      blocked: "Заблокировано",
      no_artifacts: "Нет артефактов",
      not_configured: "Не настроено",
      limited: "Ограничено",
      unknown: "Неизвестно",
    },
    logJournal: "Журнал логирования",
    open: "Открыть",
    createDiagnosticArchive: "Создать диагностический архив",
    bugReport: "Отчёт об ошибке",
    bugReportPlaceholder: "Опишите проблему простым языком: что произошло, где нажимали, что ожидали увидеть.",
    sendBugReport: "Отправить отчёт",
    journalEmpty: "Журнал событий будет отображаться здесь после подключения backend-логирования.",
    journalLoading: "Загрузка журнала...",
    journalError: "Журнал событий недоступен.",
    journalSystemActor: "система",
    journalFilters: "Фильтры",
    journalCategory: "Категория",
    journalSeverity: "Важность",
    journalActor: "Инициатор",
    journalTarget: "Объект",
    journalSince: "Период",
    journalSearch: "Поиск",
    journalAll: "Все",
    journalApply: "Применить",
    journalLoadMore: "Загрузить ещё",
    journalMetadata: "Метаданные",
    journalEventType: "Тип события",
    journalTargetEmpty: "без объекта",
    journalSince60: "1 час",
    journalSince360: "6 часов",
    journalSince1440: "24 часа",
    journalSinceAll: "Любое время",
    reportSendingPending: "Отправка отчётов будет подключена после реализации backend-отправки.",
    diagnosticArchiveReady: "Диагностический архив создан, прикреплён и скачан.",
    diagnosticArchiveQuestion: "Какой лог снять?",
    diagnosticArchiveNormal: "Обычный",
    diagnosticArchiveExtended: "Расширенный",
    resetPasswordLabel: "Новый пароль (сброс администратором)",
    users: "Пользователи и роли",
    usersText: "Управление пользователями, ролями и доступом к системе.",
    usersDenied: "Недостаточно прав для управления пользователями.",
    currentUser: "Текущий пользователь",
    session: "Сессия",
    sessionPolicy: "Сессия сохраняется до 24:00 при включённом режиме «Оставаться в системе».",
    addUser: "Добавить пользователя",
    editUser: "Изменить пользователя",
    username: "Логин",
    displayName: "Имя",
    password: "Пароль",
    passwordOptional: "Пароль (если нужно изменить)",
    role: "Роль",
    status: "Статус",
    active: "Активен",
    inactive: "Отключён",
    actions: "Управление",
    edit: "Изменить",
    deactivate: "Отключить",
    activate: "Включить",
    create: "Создать",
    update: "Сохранить",
    close: "Закрыть",
    usernameRequired: "Укажите логин.",
    passwordRequired: "Укажите пароль не короче 8 символов.",
    currentPasswordRequired: "Укажите текущий пароль.",
    credentialsChanged: "Данные входа изменены. Войдите заново.",
    writeDenied: "Нет доступа на запись.",
    roleRequired: "Выберите роль.",
    roleOwner: "Владелец",
    roleAdmin: "Администратор",
    roleOperator: "Оператор",
    roleViewer: "Наблюдатель",
    toasts: {
      saveOkTitle: "Настройки сохранены",
      saveOkText: "Изменения успешно применены",
      hardwareOkTitle: "Аппаратные возможности проверены",
      hardwareOkText: "Доступно: {modes}",
      hardwareFallbackTitle: "Режим изменён",
      hardwareFallbackText: "Выбранный режим недоступен. Включён автоматический выбор.",
      authTitle: "Нужно войти заново",
      authText: "Сессия истекла или токен недействителен",
      forbiddenTitle: "Недостаточно прав",
      forbiddenText: "У текущего пользователя нет доступа к настройкам",
      networkTitle: "Сервер недоступен",
      networkText: "Проверьте подключение или состояние сервиса",
      hardwareFailTitle: "Проверка аппаратных возможностей не выполнена",
      hardwareFailText: "Повторите проверку позже",
      unavailableTitle: "Режим недоступен",
      userCreatedTitle: "Пользователь создан",
      userUpdatedTitle: "Пользователь обновлён",
      userDisabledTitle: "Пользователь отключён",
      userEnabledTitle: "Пользователь включён",
      usersFailTitle: "Ошибка пользователя",
      usersFailText: "Не удалось выполнить действие с пользователем",
      logsTitle: "Архив логов подготовлен",
    },
    tooltips: {
      timezone: "Используется для отображения времени, записи файлов и хронологии.",
      recording: "Определяет формат и поведение записи видео.",
      hardware: "Использует видеоускорение сервера для обработки видео.",
      auto: "Система сама выбирает оптимальное ускорение.",
      qsv: "Аппаратное ускорение через встроенную графику Intel.",
      vaapi: "Аппаратное ускорение для Linux-систем.",
      nvenc: "Аппаратное ускорение через видеокарты NVIDIA.",
      amf: "Аппаратное ускорение через видеокарты AMD.",
      cpu: "Обработка видео без ускорения, только на процессоре.",
      security: "Системный журнал логирования и отчёты об ошибках.",
      users: "Управление доступом пользователей к системе.",
    },
  },
  en: {
    title: "Settings",
    subtitle: "KM VMS system settings: language, time, archive storage, recording, acceleration, and security.",
    save: "Save",
    saving: "Saving...",
    cancel: "Cancel",
    dirty: "You have unsaved changes",
    checking: "Checking...",
    systemName: "System name",
    systemNameHelp: "Non-secret product name for admin screens.",
    language: "Language",
    languageHelp: "KM VMS interface language.",
    russian: "Русский",
    english: "English",
    timezone: "Timezone",
    timezoneHelp: "Defines interface time, archive timestamps, and chronology.",
    recording: "Recording format",
    compatibility: "Maximum compatibility",
    compatibilityHelp: "Current mapping: MP4. Easier to open in players.",
    reliability: "Maximum reliability",
    reliabilityHelp: "Current mapping: MKV. More resilient to recording interruptions.",
    mapsTo: "Format",
    hardware: "Hardware Acceleration",
    hardwareAvailable: "Hardware acceleration is available.",
    hardwareUnavailable: "Hardware acceleration is unavailable. CPU fallback will be used.",
    selected: "Selected",
    unavailableMode: "This mode is unavailable on this server or failed validation.",
    rescan: "Refresh hardware capabilities",
    failedValidation: "Failed validation",
    notDetected: "Not detected on this server",
    security: "Security",
    securityText: "Logging journal, diagnostic logs collection and bug report.",
    maintenance: "Maintenance",
    maintenanceText: "Update, migration, restore and diagnostic report status without running apply actions.",
    maintenanceOverview: "Maintenance overview",
    maintenanceRefresh: "Refresh",
    maintenanceLoadError: "Maintenance overview is unavailable.",
    maintenanceLimitedHistory: "Durable history is limited: current status and the latest safe report are shown.",
    maintenanceReport: "Upgrade report",
    maintenanceReportView: "View report",
    maintenanceReportDownload: "Download JSON",
    maintenanceReportReady: "Sanitized report is available.",
    maintenanceReportUnavailable: "Report is unavailable.",
    maintenanceDryRun: "Check",
    maintenanceDryRunResult: "Check completed.",
    maintenanceNoApply: "Apply is not executed from this screen.",
    maintenanceBackupRequired: "Backup required",
    maintenanceBackupNotRequired: "Backup not required",
    maintenanceConfirmationRequired: "Confirmation required",
    maintenanceUnsupported: "Action blocked or unsupported",
    maintenanceLastAction: "Last action",
    maintenanceNoHistory: "No action history found",
    maintenanceGeneratedAt: "Generated",
    maintenanceWarnings: "Warnings",
    updateApplyTitle: "Update apply",
    updateApplyCheck: "Check update",
    updateApplyStart: "Apply update",
    updateApplyConfirm: "Start KM VMS update? The system will run preflight, apply the trusted release through the helper and may temporarily restart services.",
    updateApplyQueued: "Update request was handed to the helper.",
    updateApplyUnavailable: "Apply is unavailable for the current state.",
    updateApplyConnection: "Services may restart temporarily; status polling will continue automatically.",
    updateCurrent: "Current",
    updateLatest: "Latest",
    updateSource: "Source",
    maintenanceLabels: {
      pending: "Pending",
      artifacts: "Artifacts",
      current: "Current version",
      target: "Target version",
      available: "Available version",
      backup: "Backup",
      confirm: "Confirmation",
      apply: "Apply",
      reportId: "Report ID",
    },
    maintenanceFlows: {
      db_adoption: "DB adoption",
      migration: "Migrations",
      restore: "Restore",
      update: "Update",
    },
    maintenanceStatuses: {
      ok: "OK",
      current: "Current",
      available: "Available",
      adoptable: "Adoptable",
      adopted: "Adopted",
      already_adopted: "Already adopted",
      blocked: "Blocked",
      no_artifacts: "No artifacts",
      not_configured: "Not configured",
      limited: "Limited",
      unknown: "Unknown",
    },
    logJournal: "Logging journal",
    open: "Open",
    createDiagnosticArchive: "Create diagnostic archive",
    bugReport: "Bug report",
    bugReportPlaceholder: "Describe the problem in simple language: what happened, where you clicked, what you expected.",
    sendBugReport: "Send report",
    journalEmpty: "Event journal will appear here after backend logging is connected.",
    journalLoading: "Loading journal...",
    journalError: "Event journal is unavailable.",
    journalSystemActor: "system",
    journalFilters: "Filters",
    journalCategory: "Category",
    journalSeverity: "Severity",
    journalActor: "Actor",
    journalTarget: "Target",
    journalSince: "Period",
    journalSearch: "Search",
    journalAll: "All",
    journalApply: "Apply",
    journalLoadMore: "Load more",
    journalMetadata: "Metadata",
    journalEventType: "Event type",
    journalTargetEmpty: "no target",
    journalSince60: "1 hour",
    journalSince360: "6 hours",
    journalSince1440: "24 hours",
    journalSinceAll: "Any time",
    reportSendingPending: "Report sending will be connected after backend sending is implemented.",
    diagnosticArchiveReady: "Diagnostic archive created, attached and downloaded.",
    diagnosticArchiveQuestion: "Which log should be collected?",
    diagnosticArchiveNormal: "Normal",
    diagnosticArchiveExtended: "Extended",
    resetPasswordLabel: "New password (admin reset)",
    users: "Users and roles",
    usersText: "Manage users, roles and system access.",
    usersDenied: "Section unavailable. User permissions are limited.",
    currentUser: "Current user",
    session: "Session",
    sessionPolicy: "Session is kept until 24:00 when \"Stay signed in\" is enabled.",
    addUser: "Add user",
    editUser: "Edit user",
    username: "Username",
    displayName: "Name",
    password: "Password",
    passwordOptional: "Password (change only if needed)",
    role: "Role",
    status: "Status",
    active: "Active",
    inactive: "Disabled",
    actions: "Management",
    edit: "Edit",
    deactivate: "Disable",
    activate: "Enable",
    create: "Create",
    update: "Save",
    close: "Close",
    usernameRequired: "Enter username.",
    passwordRequired: "Enter a password with at least 8 characters.",
    currentPasswordRequired: "Enter current password.",
    credentialsChanged: "Login credentials changed. Please sign in again.",
    writeDenied: "Write access denied.",
    roleRequired: "Select a role.",
    roleOwner: "Owner",
    roleAdmin: "Administrator",
    roleOperator: "Operator",
    roleViewer: "Viewer",
    toasts: {
      saveOkTitle: "Settings saved",
      saveOkText: "Changes applied successfully",
      hardwareOkTitle: "Hardware capabilities checked",
      hardwareOkText: "Available: {modes}",
      hardwareFallbackTitle: "Mode changed",
      hardwareFallbackText: "Selected mode is unavailable. Automatic selection is enabled.",
      authTitle: "Sign in required",
      authText: "Session expired or token is invalid",
      forbiddenTitle: "Section unavailable",
      forbiddenText: "User permissions are limited.",
      networkTitle: "Server unavailable",
      networkText: "Check connection or service status",
      hardwareFailTitle: "Hardware capability check failed",
      hardwareFailText: "Try checking again later",
      unavailableTitle: "Mode unavailable",
      userCreatedTitle: "User created",
      userUpdatedTitle: "User updated",
      userDisabledTitle: "User disabled",
      userEnabledTitle: "User enabled",
      usersFailTitle: "User error",
      usersFailText: "User action failed",
      logsTitle: "Log archive prepared",
    },
    tooltips: {
      timezone: "Used for time display, recording timestamps, and chronology.",
      recording: "Defines recording format and behavior.",
      hardware: "Uses server video acceleration for video processing.",
      auto: "The system selects the best available acceleration.",
      qsv: "Hardware acceleration through Intel integrated graphics.",
      vaapi: "Hardware acceleration for Linux systems.",
      nvenc: "Hardware acceleration through NVIDIA GPUs.",
      amf: "Hardware acceleration through AMD GPUs.",
      cpu: "Video processing without acceleration, using CPU only.",
      security: "System logging and bug reports.",
      users: "Manage user access to the system.",
    },
  },
};

const ZH_TEXT_OVERRIDES = {
  maintenance: "维护",
  maintenanceText: "显示更新、迁移、恢复和诊断报告状态，不执行应用操作。",
  maintenanceOverview: "维护概览",
  maintenanceRefresh: "刷新",
  maintenanceLoadError: "维护概览不可用。",
  maintenanceLimitedHistory: "持久历史记录有限：仅显示当前状态和最新安全报告。",
  maintenanceReport: "升级报告",
  maintenanceReportView: "查看报告",
  maintenanceReportDownload: "下载 JSON",
  maintenanceReportReady: "已提供脱敏报告。",
  maintenanceReportUnavailable: "报告不可用。",
  maintenanceDryRun: "检查",
  maintenanceDryRunResult: "检查已完成。",
  maintenanceNoApply: "此界面不会执行应用操作。",
  maintenanceBackupRequired: "需要备份",
  maintenanceBackupNotRequired: "不需要备份",
  maintenanceConfirmationRequired: "需要确认",
  maintenanceUnsupported: "操作被阻止或不受支持",
  maintenanceLastAction: "最近操作",
  maintenanceNoHistory: "未找到操作历史",
  maintenanceGeneratedAt: "生成时间",
  maintenanceWarnings: "警告",
  updateApplyTitle: "应用更新",
  updateApplyCheck: "检查更新",
  updateApplyStart: "应用更新",
  updateApplyConfirm: "启动 KM VMS 更新？系统将执行预检查，通过 helper 应用受信任版本，并可能短暂重启服务。",
  updateApplyQueued: "更新请求已交给 helper。",
  updateApplyUnavailable: "当前状态不可应用更新。",
  updateApplyConnection: "服务可能会短暂重启；状态轮询会自动继续。",
  updateCurrent: "当前",
  updateLatest: "可用",
  updateSource: "来源",
  maintenanceLabels: {
    pending: "待处理",
    artifacts: "工件",
    current: "当前版本",
    target: "目标版本",
    available: "可用版本",
    backup: "备份",
    confirm: "确认",
    apply: "应用",
    reportId: "报告 ID",
  },
  maintenanceFlows: {
    db_adoption: "数据库接管",
    migration: "迁移",
    restore: "恢复",
    update: "更新",
  },
  maintenanceStatuses: {
    ok: "正常",
    current: "当前",
    available: "可用",
    adoptable: "可接管",
    adopted: "已接管",
    already_adopted: "已接管",
    blocked: "已阻止",
    no_artifacts: "无工件",
    not_configured: "未配置",
    limited: "受限",
    unknown: "未知",
  },
};

function normalizedError(err, lang, context = "generic") {
  const t = settingsTextFor(lang);
  const message = String(err?.message || "");
  const detail = parseErrorDetail(message);
  const lower = message.toLowerCase();

  if (lower.includes("not authenticated") || lower.includes("invalid token") || message.includes("401")) {
    return { variant: "error", title: t.toasts.authTitle, text: t.toasts.authText };
  }
  if (message.includes("403") || lower.includes("forbidden") || message.includes("ограничены права пользователя")) {
    return { variant: "error", title: t.toasts.forbiddenTitle, text: forbiddenMessage(lang) };
  }
  if (lower.includes("failed to fetch") || lower.includes("networkerror") || lower.includes("server is unavailable")) {
    return { variant: "error", title: t.toasts.networkTitle, text: t.toasts.networkText };
  }
  if (context === "hardware") {
    return { variant: "error", title: t.toasts.hardwareFailTitle, text: t.toasts.hardwareFailText };
  }
  if (context === "users") {
    if (lower.includes("password") && (lower.includes("8") || lower.includes("short"))) {
      return { variant: "error", title: t.toasts.usersFailTitle, text: passwordLengthMessage(lang) };
    }
    return { variant: "error", title: t.toasts.usersFailTitle, text: humanErrorText(message, t.toasts.usersFailText) };
  }
  return { variant: "error", title: t.toasts.networkTitle, text: humanErrorText(message, t.toasts.networkText) };
}

function InfoTip({ text }) {
  if (!text) return null;
  return (
    <span className="settingsInfoTip" tabIndex="0" aria-label={text}>
      i
      <span className="settingsInfoBubble" role="tooltip">{text}</span>
    </span>
  );
}

export default function SettingsPage() {
  const router = useRouter();
  const [draft, setDraft] = useState(null);
  const [savedDraft, setSavedDraft] = useState(null);
  const [hardware, setHardware] = useState(null);
  const [toast, setToast] = useState(null);
  const [saving, setSaving] = useState(false);
  const [hardwareChecking, setHardwareChecking] = useState(false);
  const [currentUser, setCurrentUser] = useState(null);
  const [users, setUsers] = useState([]);
  const [usersLoading, setUsersLoading] = useState(false);
  const [userBusy, setUserBusy] = useState(false);
  const [usersModalOpen, setUsersModalOpen] = useState(false);
  const [userModal, setUserModal] = useState(null);
  const [userDeleteTarget, setUserDeleteTarget] = useState(null);
  const [securityModalOpen, setSecurityModalOpen] = useState(false);
  const [maintenanceModalOpen, setMaintenanceModalOpen] = useState(false);
  const [maintenanceOverview, setMaintenanceOverview] = useState(null);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceError, setMaintenanceError] = useState("");
  const [maintenanceBusy, setMaintenanceBusy] = useState("");
  const [maintenanceActionResult, setMaintenanceActionResult] = useState(null);
  const [maintenanceReport, setMaintenanceReport] = useState(null);
  const [updateStatus, setUpdateStatus] = useState(null);
  const [updateApplyStatus, setUpdateApplyStatus] = useState(null);
  const [updateApplyTransientError, setUpdateApplyTransientError] = useState("");
  const [diagnosticChoiceOpen, setDiagnosticChoiceOpen] = useState(false);
  const [securityBusy, setSecurityBusy] = useState(false);
  const [auditEvents, setAuditEvents] = useState([]);
  const [auditOffset, setAuditOffset] = useState(0);
  const [auditHasMore, setAuditHasMore] = useState(false);
  const [auditFilters, setAuditFilters] = useState({ category: "", severity: "", actor: "", target: "", since: "1440", q: "" });
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState("");
  const [bugReportText, setBugReportText] = useState("");
  const [diagnosticArchive, setDiagnosticArchive] = useState(null);
  const toastTimerRef = useRef(null);
  const lang = languageOf(draft || savedDraft);
  const t = settingsTextFor(lang);
  const dirty = Boolean(draft && savedDraft && !samePayload(draft, savedDraft));
  const anyBusy = saving || hardwareChecking;
  const canManageMaintenance = Boolean(currentUser?.permissions?.includes("manage_settings"));
  const canManageUsers = Boolean(currentUser?.permissions?.includes("manage_users"));
  const sortedUsers = useMemo(() => sortedUsersForTable(users), [users]);
  const languageIcon = lang === "en" ? "/assets/icons/ui/language-en.png" : "/assets/icons/ui/language-ru.png";
  const updateLatest = updateStatus?.latest || updateStatus?.latest_release || {};
  const updateInstalled = updateStatus?.installed_build || updateStatus?.installed || {};
  const updateApplyRunning = ["queued", "starting_helper", "preflight", "downloading", "extracting", "validating_source", "applying", "compose_config", "rebuilding", "restarting", "health_check"].includes(updateApplyStatus?.status || "");
  const updateApplyAllowed = Boolean(updateStatus?.can_apply_from_ui && !updateApplyRunning && !maintenanceBusy);

  useEffect(() => {
    load();
    function onLanguage(event) {
      if (event.detail) patch("language", normalizeLocale(event.detail));
    }
    window.addEventListener("km-vms-language", onLanguage);
    return () => {
      window.clearTimeout(toastTimerRef.current);
      window.removeEventListener("km-vms-language", onLanguage);
    };
  }, []);

  useEffect(() => {
    if (securityModalOpen) {
      loadAuditEvents();
    }
  }, [securityModalOpen, auditFilters]);

  useEffect(() => {
    if (maintenanceModalOpen && canManageMaintenance) {
      loadMaintenanceOverview();
      loadUpdateApplySurface({ silent: true });
    }
  }, [maintenanceModalOpen, canManageMaintenance]);

  useEffect(() => {
    if (!maintenanceModalOpen || !canManageMaintenance) return undefined;
    const active = ["queued", "starting_helper", "preflight", "downloading", "extracting", "validating_source", "applying", "compose_config", "rebuilding", "restarting", "health_check"].includes(updateApplyStatus?.status || "");
    if (!active) return undefined;
    const timer = window.setInterval(() => loadUpdateApplySurface({ silent: true }), 5000);
    return () => window.clearInterval(timer);
  }, [maintenanceModalOpen, canManageMaintenance, updateApplyStatus?.status]);

  function showToast(nextToast) {
    setToast(nextToast);
    window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => setToast(null), 2600);
  }

  async function load() {
    try {
      const [settingsData, hardwareData, meData] = await Promise.all([
        apiFetch("/settings"),
        apiFetch("/hardware/capabilities"),
        apiFetch("/users/me"),
      ]);
      const nextDraft = settingsDraftFromApi(settingsData);
      setDraft(nextDraft);
      setSavedDraft(nextDraft);
      setHardware(hardwareData);
      setCurrentUser(meData);
      if (meData?.permissions?.includes("admin_access")) {
        await loadUsers();
      }
    } catch (err) {
      showToast(normalizedError(err, lang));
    }
  }

  async function loadUsers() {
    setUsersLoading(true);
    try {
      setUsers(await apiFetch("/users"));
    } catch (err) {
      showToast(normalizedError(err, lang, "users"));
    } finally {
      setUsersLoading(false);
    }
  }

  function auditQuery(offset = 0) {
    const params = new URLSearchParams();
    params.set("limit", String(AUDIT_LIMIT));
    params.set("offset", String(offset));
    if (auditFilters.category) params.set("category", auditFilters.category);
    if (auditFilters.severity) params.set("severity", auditFilters.severity);
    if (auditFilters.actor.trim()) params.set("actor", auditFilters.actor.trim());
    if (auditFilters.target.trim()) params.set("target", auditFilters.target.trim());
    if (auditFilters.since) params.set("since_minutes", auditFilters.since);
    if (auditFilters.q.trim()) params.set("q", auditFilters.q.trim());
    return `/audit/events?${params.toString()}`;
  }

  async function loadAuditEvents(offset = 0) {
    setAuditLoading(true);
    setAuditError("");
    try {
      const data = await apiFetch(auditQuery(offset));
      const items = Array.isArray(data?.items) ? data.items : [];
      setAuditEvents(offset > 0 ? (current) => [...current, ...items] : items);
      setAuditOffset(offset + items.length);
      setAuditHasMore(items.length === AUDIT_LIMIT);
    } catch (err) {
      if (offset === 0) setAuditEvents([]);
      setAuditError(humanErrorText(String(err?.message || ""), t.journalError || "Event journal is unavailable."));
    } finally {
      setAuditLoading(false);
    }
  }

  function patchAuditFilter(key, value) {
    setAuditOffset(0);
    setAuditFilters((current) => ({ ...current, [key]: value }));
  }

  function patch(key, value) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function cancelChanges() {
    if (!savedDraft) return;
    setDraft(savedDraft);
    persistLocale(savedDraft.language);
  }

  async function save() {
    if (!draft || !dirty || saving) return;
    setSaving(true);
    try {
      const updated = await apiFetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payloadFromDraft(draft)),
      });
      const nextDraft = settingsDraftFromApi(updated);
      setDraft(nextDraft);
      setSavedDraft(nextDraft);
      persistLocale(nextDraft.language);
      showToast({ variant: "success", title: t.toasts.saveOkTitle, text: t.toasts.saveOkText });
    } catch (err) {
      showToast(normalizedError(err, lang));
    } finally {
      setSaving(false);
    }
  }

  async function rescanHardware() {
    if (hardwareChecking) return;
    setHardwareChecking(true);
    try {
      const nextHardware = await apiFetch("/hardware/rescan", { method: "POST" });
      setHardware(nextHardware);
      const selected = draft?.hardware_preferred_backend;
      if (selected && !["auto", "cpu"].includes(selected) && !(nextHardware?.available_backends || []).includes(selected)) {
        patch("hardware_preferred_backend", null);
        showToast({ variant: "warning", title: t.toasts.hardwareFallbackTitle, text: t.toasts.hardwareFallbackText });
      } else {
        const available = (nextHardware?.available_backends || []).map((backend) => backendLabel(backend, lang)).join(", ") || backendLabel("cpu", lang);
        showToast({
          variant: "success",
          title: t.toasts.hardwareOkTitle,
          text: t.toasts.hardwareOkText.replace("{modes}", available),
        });
      }
    } catch (err) {
      showToast(normalizedError(err, lang, "hardware"));
    } finally {
      setHardwareChecking(false);
    }
  }

  const selectedHardware = draft?.hardware_preferred_backend || "auto";
  const profileHelp = {
    compatibility: t.compatibilityHelp,
    reliability: t.reliabilityHelp,
  }[draft?.recordingProfile || "reliability"];

  const hardwareSummary = useMemo(() => (
    HARDWARE_OPTIONS.map((backend) => ({
      backend,
      ...hardwareOptionState(backend, hardware, t),
    }))
  ), [hardware, t]);

  function handleHardwareChange(event) {
    const value = event.target.value || "auto";
    const state = hardwareOptionState(value, hardware, t);
    if (!state.selectable) {
      showToast({ variant: "warning", title: t.toasts.unavailableTitle, text: t.unavailableMode });
      return;
    }
    patch("hardware_preferred_backend", value === "auto" ? null : value);
  }

  function handleSettingsLanguageChange(event) {
    const nextLanguage = normalizeLocale(event.target.value);
    patch("language", nextLanguage);
    persistLocale(nextLanguage);
  }

  async function openUsersModal() {
    if (!canManageUsers || usersLoading) return;
    setUsersModalOpen(true);
    await loadUsers();
  }

  function openCreateUser() {
    const options = roleOptionsFor(currentUser);
    setUserModal({
      mode: "create",
      id: null,
      username: "",
      display_name: "",
      password: "",
      password_confirm: "",
      current_password: "",
      fieldErrors: {},
      role: options[0] || "viewer",
      is_active: true,
      error: "",
    });
  }

  function openEditUser(user) {
    setUserModal({
      mode: "edit",
      id: user.id,
      username: user.username,
      display_name: user.display_name || "",
      password: "",
      password_confirm: "",
      current_password: "",
      fieldErrors: {},
      role: user.role,
      is_active: Boolean(user.is_active),
      is_owner: user.role === "owner",
      error: "",
    });
  }

  function patchUserModal(key, value) {
    setUserModal((current) => ({
      ...current,
      [key]: value,
      error: "",
      fieldErrors: {
        ...(current?.fieldErrors || {}),
        [key]: "",
      },
    }));
  }

  function rejectUserPassword(field, message) {
    setUserModal((current) => ({
      ...current,
      error: "",
      fieldErrors: {
        ...(current?.fieldErrors || {}),
        [field]: message,
      },
    }));
  }

  function patchBugReportText(value) {
    setBugReportText(value);
  }

  function closeSecurityModal() {
    setSecurityModalOpen(false);
    setDiagnosticChoiceOpen(false);
    setDiagnosticArchive(null);
    setBugReportText("");
    setAuditError("");
  }

  function openMaintenanceModal() {
    if (!canManageMaintenance) return;
    setMaintenanceModalOpen(true);
  }

  function closeMaintenanceModal() {
    setMaintenanceModalOpen(false);
    setMaintenanceActionResult(null);
    setMaintenanceReport(null);
    setMaintenanceError("");
    setUpdateApplyTransientError("");
  }

  async function loadMaintenanceOverview() {
    if (!canManageMaintenance) return;
    setMaintenanceLoading(true);
    setMaintenanceError("");
    try {
      setMaintenanceOverview(await apiFetch("/system/maintenance/overview"));
    } catch (err) {
      setMaintenanceOverview(null);
      setMaintenanceError(humanErrorText(String(err?.message || ""), t.maintenanceLoadError));
    } finally {
      setMaintenanceLoading(false);
    }
  }

  async function loadUpdateApplySurface({ silent = false } = {}) {
    if (!canManageMaintenance) return;
    if (!silent) setMaintenanceBusy("update-status");
    try {
      const [statusData, applyData] = await Promise.all([
        apiFetch("/system/update/status"),
        apiFetch("/system/update/apply/status"),
      ]);
      setUpdateStatus(statusData);
      setUpdateApplyStatus(applyData);
      setUpdateApplyTransientError("");
    } catch (err) {
      setUpdateApplyTransientError(humanErrorText(String(err?.message || ""), t.updateApplyConnection));
    } finally {
      if (!silent) setMaintenanceBusy("");
    }
  }

  async function runMaintenanceDryRun(flowKey) {
    const config = MAINTENANCE_DRY_RUN_ENDPOINTS[flowKey];
    if (!config || maintenanceBusy) return;
    setMaintenanceBusy(flowKey);
    setMaintenanceActionResult(null);
    try {
      const result = await apiFetch(config.path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config.body),
      });
      setMaintenanceActionResult({ flowKey, status: result?.status || "ok", reason: result?.reason || result?.blocked_reason || "" });
      if (flowKey === "update") {
        setUpdateStatus(result);
        try {
          setUpdateApplyStatus(await apiFetch("/system/update/apply/status"));
        } catch (statusErr) {
          setUpdateApplyTransientError(humanErrorText(String(statusErr?.message || ""), t.updateApplyConnection));
        }
      }
      showToast({ variant: "success", title: t.maintenanceDryRunResult, text: maintenanceStatusText(result?.status, t) });
      await loadMaintenanceOverview();
    } catch (err) {
      const message = humanErrorText(String(err?.message || ""), t.maintenanceLoadError);
      setMaintenanceActionResult({ flowKey, status: "blocked", reason: message });
      showToast({ variant: "warning", title: t.maintenanceDryRun, text: message });
    } finally {
      setMaintenanceBusy("");
    }
  }

  async function startUpdateApply() {
    if (maintenanceBusy) return;
    if (!window.confirm(t.updateApplyConfirm)) return;
    setMaintenanceBusy("update-apply");
    setMaintenanceActionResult(null);
    try {
      const latest = updateStatus?.latest || updateStatus?.latest_release || {};
      const result = await apiFetch("/system/update/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirm: true,
          expected_manifest_version: latest.version || latest.latest_version || null,
          expected_manifest_commit: latest.commit || latest.build_id || null,
        }),
      });
      setUpdateApplyStatus(result?.apply_status || result);
      showToast({ variant: "success", title: t.updateApplyTitle, text: t.updateApplyQueued });
      await loadUpdateApplySurface({ silent: true });
    } catch (err) {
      const message = humanErrorText(String(err?.message || ""), t.updateApplyUnavailable);
      setMaintenanceActionResult({ flowKey: "update", status: "blocked", reason: message });
      showToast({ variant: "warning", title: t.updateApplyTitle, text: message });
    } finally {
      setMaintenanceBusy("");
    }
  }

  async function viewMaintenanceReport() {
    if (maintenanceBusy) return;
    setMaintenanceBusy("report");
    try {
      const report = await apiFetch("/system/upgrade/report");
      setMaintenanceReport(report);
      showToast({ variant: "success", title: t.maintenanceReport, text: t.maintenanceReportReady });
    } catch (err) {
      setMaintenanceReport(null);
      showToast({ variant: "warning", title: t.maintenanceReport, text: humanErrorText(String(err?.message || ""), t.maintenanceReportUnavailable) });
    } finally {
      setMaintenanceBusy("");
    }
  }

  async function downloadMaintenanceReport() {
    if (maintenanceBusy) return;
    setMaintenanceBusy("report-download");
    try {
      const { blob } = await apiFetchBlob("/system/upgrade/report");
      downloadBlob(blob, "km-vms-upgrade-report.json");
      showToast({ variant: "success", title: t.maintenanceReport, text: t.maintenanceReportReady });
    } catch (err) {
      showToast({ variant: "warning", title: t.maintenanceReport, text: humanErrorText(String(err?.message || ""), t.maintenanceReportUnavailable) });
    } finally {
      setMaintenanceBusy("");
    }
  }

  async function submitUserModal(event) {
    event.preventDefault();
    if (!userModal || userBusy) return;
    if (!userModal.username.trim()) {
      patchUserModal("error", t.usernameRequired);
      return;
    }
    if (userModal.mode === "create" && userModal.password.length < 8) {
      rejectUserPassword("password", passwordLengthMessage(lang));
      return;
    }
    const availableRoles = userModal.mode === "edit" && userModal.id === currentUser?.id
      ? [currentUser.role]
      : userModal.mode === "edit" && userModal.is_owner
      ? ["owner"]
      : roleOptionsFor(currentUser);
    if (!availableRoles.includes(userModal.role)) {
      patchUserModal("error", t.roleRequired);
      return;
    }
    if (userModal.mode === "edit" && userModal.password && userModal.password.length < 8) {
      rejectUserPassword("password", passwordLengthMessage(lang));
      return;
    }
    if ((userModal.mode === "create" || userModal.password) && userModal.password !== userModal.password_confirm) {
      rejectUserPassword("password_confirm", passwordConfirmMessage(lang));
      return;
    }
    if (userModal.mode === "edit" && userModal.id === currentUser?.id && userModal.password && !userModal.current_password) {
      patchUserModal("error", t.currentPasswordRequired || t.passwordRequired);
      return;
    }

    setUserBusy(true);
    try {
      const body = {
        username: userModal.username.trim(),
        display_name: userModal.display_name.trim(),
        role: userModal.role,
        is_active: Boolean(userModal.is_active),
      };
      if (userModal.password) body.password = userModal.password;
      if (userModal.current_password) body.current_password = userModal.current_password;

      const changesOwnCredentials = userModal.mode === "edit"
        && userModal.id === currentUser?.id
        && (body.username !== currentUser?.username || Boolean(userModal.password));

      if (userModal.mode === "create") {
        await apiFetch("/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        showToast({ variant: "success", title: t.toasts.userCreatedTitle, text: userModal.username.trim() });
      } else {
        await apiFetch(`/users/${userModal.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (changesOwnCredentials) {
          setUserModal(null);
          showToast({ variant: "success", title: t.credentialsChanged });
          clearAuthToken();
          router.replace("/login");
          return;
        }
        showToast({ variant: "success", title: t.toasts.userUpdatedTitle, text: userModal.username.trim() });
      }
      setUserModal(null);
      await loadUsers();
    } catch (err) {
      const errorToast = normalizedError(err, lang, "users");
      showToast(errorToast);
    } finally {
      setUserBusy(false);
    }
  }

  async function toggleUserActive(user) {
    if (userBusy || !userCanBeManaged(currentUser, user) || user.id === currentUser?.id) return;
    setUserBusy(true);
    try {
      await apiFetch(`/users/${user.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !user.is_active }),
      });
      showToast({
        variant: "success",
        title: user.is_active ? t.toasts.userDisabledTitle : t.toasts.userEnabledTitle,
        text: user.username,
      });
      await loadUsers();
    } catch (err) {
      showToast(normalizedError(err, lang, "users"));
    } finally {
      setUserBusy(false);
    }
  }

  function requestDeleteUser(user) {
    if (userBusy || !userCanBeDeleted(currentUser, user, users)) return;
    setUserDeleteTarget(user);
  }

  async function deleteUser(user) {
    if (userBusy || !userCanBeDeleted(currentUser, user, users)) return;
    setUserBusy(true);
    try {
      await apiFetch(`/users/${user.id}`, { method: "DELETE" });
      showToast({
        variant: "success",
        title: t.toasts.userDeletedTitle || (lang === "en" ? "User deleted" : "Пользователь удалён"),
        text: user.username,
      });
      setUserDeleteTarget(null);
      await loadUsers();
    } catch (err) {
      showToast(normalizedError(err, lang, "users"));
    } finally {
      setUserBusy(false);
    }
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename || "km-vms-logs.zip";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  async function downloadLogArchive(mode = "normal") {
    if (securityBusy) return;
    setDiagnosticChoiceOpen(false);
    setSecurityBusy(true);
    try {
      const { blob, filename } = await apiFetchBlob(`/settings/logs/archive?mode=${encodeURIComponent(mode)}`);
      downloadBlob(blob, filename);
      setDiagnosticArchive({ filename: filename || "km-vms-logs.zip" });
      showToast({ variant: "success", title: t.toasts.logsTitle, text: filename || "" });
    } catch (err) {
      setDiagnosticArchive(null);
      showToast(normalizedError(err, lang));
    } finally {
      setSecurityBusy(false);
    }
  }

  async function submitBugReport() {
    showToast({ variant: "info", title: t.bugReport, text: t.reportSendingPending });
  }

  return (
    <Layout>
      <div className="settingsPage">
        {toast ? (
          <div className={`settingsToast ${toast.variant || "info"}`}>
            <strong>{toast.title}</strong>
            {toast.text ? <span>{toast.text}</span> : null}
          </div>
        ) : null}

        <div className="settingsWorkspace">
          <div className="pageHeader settingsHeader">
            <div className="settingsTitleBlock">
              <img src="/assets/icons/ui/settings.png" alt="" />
              <div>
                <h1 className="pageTitle">{t.title}</h1>
                <div className="pageSubtitle">{t.subtitle}</div>
              </div>
            </div>

            <div className="settingsHeaderActions">
              {dirty ? <span className="settingsDirtyNote">{t.dirty}</span> : null}
              <button className="button secondary small settingsCancelButton" onClick={cancelChanges} disabled={!dirty || anyBusy}>
                {t.cancel}
              </button>
              <button className="button small" onClick={save} disabled={!draft || !dirty || saving}>
                {saving ? t.saving : t.save}
              </button>
            </div>
          </div>

          {!draft ? null : (
            <div className="settingsReferenceLayout">
              <section className="settingsPanel">
                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/settings.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label htmlFor="settings-system-name">{t.systemName}</label>
                    <span>{t.systemNameHelp}</span>
                  </div>
                  <div className="settingsRowControl">
                    <input id="settings-system-name" className="input settingsSelect" value={draft.system_name || ""} onChange={(event) => patch("system_name", event.target.value)} maxLength={80} disabled={saving} />
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src={languageIcon} alt="" /></div>
                  <div className="settingsRowText">
                    <label htmlFor="settings-language">{t.language}</label>
                    <span>{t.languageHelp}</span>
                  </div>
                  <div className="settingsRowControl">
                    <LanguageSelect id="settings-language" className="select settingsSelect" value={draft.language} onChange={(nextLanguage) => handleSettingsLanguageChange({ target: { value: nextLanguage } })} disabled={saving} aria-label={t.language} />
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/timezone.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label htmlFor="settings-timezone">{t.timezone}<InfoTip text={t.tooltips.timezone} /></label>
                    <span>{t.timezoneHelp}</span>
                  </div>
                  <div className="settingsRowControl">
                    <select id="settings-timezone" className="select settingsSelect timezoneSelect" value={timezoneValueForSettings(draft.timezone)} onChange={(event) => patch("timezone", event.target.value)} disabled={saving}>
                      {draft.timezone && !UTC_TIMEZONES.some((zone) => zone.value === draft.timezone) ? (
                        <option value={draft.timezone}>{draft.timezone}</option>
                      ) : null}
                      {UTC_TIMEZONES.map((zone) => <option key={zone.value} value={zone.value}>{zone.label}</option>)}
                    </select>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/recordings.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label htmlFor="settings-recording">{t.recording}<InfoTip text={t.tooltips.recording} /></label>
                    <span>{profileHelp} {t.mapsTo}: {recordingFormatForProfile(draft.recordingProfile).toUpperCase()}.</span>
                  </div>
                  <div className="settingsRowControl">
                    <select id="settings-recording" className="select settingsSelect" value={draft.recordingProfile} onChange={(event) => patch("recordingProfile", event.target.value)} disabled={saving}>
                      <option value="reliability">{t.reliability}</option>
                      <option value="compatibility">{t.compatibility}</option>
                    </select>
                  </div>
                </div>

                <div className="settingsRow settingsRowHardware">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/hardware.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label htmlFor="settings-hardware">{t.hardware}<InfoTip text={t.tooltips.hardware} /></label>
                    <span>{hardware?.hardware_accel_available ? t.hardwareAvailable : t.hardwareUnavailable} {t.selected}: {backendLabel(selectedHardware, lang)}.</span>
                    <div className="settingsHardwareOptions">
                      {hardwareSummary.map(({ backend, selectable, reason }) => (
                        <span key={backend} className={`settingsBadge settingsBadge-${backend} ${selectable ? "ok" : "disabled"}`} title={reason || t.tooltips[backend]}>
                          {backendLabel(backend, lang)}
                          <InfoTip text={t.tooltips[backend]} />
                        </span>
                      ))}
                      <button
                        type="button"
                        className={`settingsHardwareRescanButton ${hardwareChecking ? "isChecking" : ""}`}
                        onClick={rescanHardware}
                        disabled={hardwareChecking || saving}
                        title={hardwareChecking ? t.checking : t.rescan}
                        aria-label={hardwareChecking ? t.checking : t.rescan}
                      >
                        {"↻"}
                      </button>
                    </div>
                  </div>
                  <div className="settingsRowControl settingsHardwareControl">
                    <select id="settings-hardware" className="select settingsSelect" value={selectedHardware} onChange={handleHardwareChange} disabled={saving || hardwareChecking}>
                      {hardwareSummary.map(({ backend, selectable, reason }) => (
                        <option key={backend} value={backend} disabled={!selectable} title={reason || backendLabel(backend, lang)}>
                          {backendLabel(backend, lang)}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/security.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label>{t.security}<InfoTip text={t.tooltips.security} /></label>
                    <span>{t.securityText}</span>
                  </div>
                  <div className="settingsRowControl settingsRowControlMeta">
                    <button className="button secondary small settingsUsersAddButton" onClick={() => setSecurityModalOpen(true)}>
                      {t.open}
                    </button>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/settings.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label>{t.maintenance}</label>
                    <span>{t.maintenanceText}</span>
                    <small>{t.maintenanceNoApply}</small>
                  </div>
                  <div className="settingsRowControl settingsRowControlMeta">
                    <button className="button secondary small settingsUsersAddButton" onClick={openMaintenanceModal} disabled={!canManageMaintenance}>
                      {t.open}
                    </button>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/users.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label>{t.users}<InfoTip text={t.tooltips.users} /></label>
                    <span>{t.usersText}</span>
                  </div>
                  <div className="settingsRowControl settingsRowControlMeta">
                    <button className="button secondary small settingsUsersAddButton" onClick={openUsersModal} disabled={!canManageUsers || usersLoading || userBusy}>
                      {t.open}
                    </button>
                  </div>
                </div>
              </section>
            </div>
          )}
        </div>

        {usersModalOpen ? (
          <div className="settingsModalOverlay" role="presentation">
            <div className="settingsUsersModal" role="dialog" aria-modal="true" aria-label={t.users}>
              <div className="settingsUserModalHeader">
                <h2>{t.users}</h2>
                <button type="button" className="settingsModalClose" onClick={() => setUsersModalOpen(false)} aria-label={t.close}>×</button>
              </div>

              <div className="settingsSecuritySummary">
                <div>
                  <span>{t.currentUser}</span>
                  <strong>{currentUser?.username || "-"}</strong>
                  <small>{currentUser?.display_name || currentUser?.full_name || "-"} · {roleLabel(currentUser?.role, t)}</small>
                </div>
                <div>
                  <span>{t.session}</span>
                  <strong>24:00</strong>
                  <small>{t.sessionPolicy}</small>
                </div>
              </div>

              <div className="settingsModalToolbar">
                <button className="button small" onClick={openCreateUser} disabled={!canManageUsers || usersLoading || userBusy}>
                  {t.addUser}
                </button>
              </div>

              <div className="settingsUsersTableWrap">
                <table className="settingsUsersTable">
                  <thead>
                    <tr>
                      <th>{lang === "en" ? "Login" : "Логин"}</th>
                      <th>{lang === "en" ? "Role" : "Роль"}</th>
                      <th>{lang === "en" ? "Status" : "Статус"}</th>
                      <th>{lang === "en" ? "Management" : "Управление"}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedUsers.map((user) => (
                      <tr key={user.id}>
                        <td>
                          <strong>{user.username}</strong>
                          {user.display_name ? <small>{user.display_name}</small> : null}
                        </td>
                        <td>{roleLabel(user.role, t)}</td>
                        <td>
                          <span className={`settingsUserStatus ${user.is_active ? "active" : "inactive"}`}>
                            {user.is_active ? t.active : t.inactive}
                          </span>
                        </td>
                        <td>
                          <div className="settingsUserActions">
                            <button className="settingsUserIconButton" onClick={() => openEditUser(user)} disabled={userBusy || !(userCanBeManaged(currentUser, user) || user.id === currentUser?.id)} title="Изменить" aria-label="Изменить">
                              {"\u270e"}
                            </button>
                            <button className="settingsUserIconButton" onClick={() => toggleUserActive(user)} disabled={userBusy || !userCanBeManaged(currentUser, user) || user.id === currentUser?.id || user.role === "owner"} title={user.is_active ? "Отключить" : "Включить"} aria-label={user.is_active ? "Отключить" : "Включить"}>
                              {user.is_active ? "\u23fb" : "\u2713"}
                            </button>
                            <button className="settingsUserIconButton danger" onClick={() => requestDeleteUser(user)} disabled={userBusy || !userCanBeDeleted(currentUser, user, users)} title="Удалить" aria-label="Удалить">
                              {"\ud83d\uddd1"}
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        ) : null}

        {userDeleteTarget ? (
          <div className="settingsModalOverlay settingsConfirmOverlay" role="presentation">
            <div className="settingsConfirmModal" role="dialog" aria-modal="true" aria-label={lang === "en" ? "Delete user" : "Удалить пользователя"}>
              <button type="button" className="settingsModalClose settingsConfirmClose" onClick={() => setUserDeleteTarget(null)} aria-label={t.close}>×</button>
              <p>
                <span>{lang === "en" ? "Delete user" : "Удалить пользователя"}</span>
                <strong>{userDeleteTarget.username}?</strong>
              </p>
              <div className="settingsModalActions">
                <button type="button" className="button secondary small" onClick={() => setUserDeleteTarget(null)} disabled={userBusy}>{t.cancel}</button>
                <button type="button" className="button small dangerButton" onClick={() => deleteUser(userDeleteTarget)} disabled={userBusy}>
                  {userBusy ? (lang === "en" ? "Deleting..." : "Удаляем...") : (lang === "en" ? "Delete" : "Удалить")}
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {userModal ? (
          <div className="settingsModalOverlay" role="presentation">
            <form className="settingsUserModal" onSubmit={submitUserModal}>
              <div className="settingsUserModalHeader">
                <h2>{userModal.mode === "create" ? t.addUser : t.editUser}</h2>
                <button type="button" className="settingsModalClose" onClick={() => setUserModal(null)} aria-label={t.close}>×</button>
              </div>

              <label className="settingsModalField">
                <span>{t.username}</span>
                <input className="input" value={userModal.username} onChange={(event) => patchUserModal("username", event.target.value)} disabled={userBusy} />
              </label>

              <label className="settingsModalField">
                <span>{t.displayName}</span>
                <input className="input" value={userModal.display_name} onChange={(event) => patchUserModal("display_name", event.target.value)} disabled={userBusy} />
              </label>

              <label className="settingsModalField">
                <span>{userModal.mode === "create" ? t.password : userModal.id === currentUser?.id ? t.passwordOptional : t.resetPasswordLabel}</span>
                <input className="input" type="password" value={userModal.password} onChange={(event) => patchUserModal("password", event.target.value)} disabled={userBusy} autoComplete="new-password" />
                <small className="settingsModalHint">{passwordHint(lang)}</small>
                {userModal.fieldErrors?.password ? <small className="settingsModalFieldError">{userModal.fieldErrors.password}</small> : null}
              </label>

              <label className="settingsModalField">
                <span>{lang === "en" ? "Repeat password" : "Повторите пароль"}</span>
                <input className="input" type="password" value={userModal.password_confirm || ""} onChange={(event) => patchUserModal("password_confirm", event.target.value)} disabled={userBusy || (userModal.mode === "edit" && !userModal.password)} autoComplete="new-password" />
                {userModal.fieldErrors?.password_confirm ? <small className="settingsModalFieldError">{userModal.fieldErrors.password_confirm}</small> : null}
              </label>

              {userModal.mode === "edit" && userModal.id === currentUser?.id && userModal.password ? (
                <label className="settingsModalField">
                  <span>{t.currentPasswordRequired}</span>
                  <input className="input" type="password" value={userModal.current_password} onChange={(event) => patchUserModal("current_password", event.target.value)} disabled={userBusy} autoComplete="current-password" />
                </label>
              ) : null}

              <div className="settingsModalGrid">
                <label className="settingsModalField">
                  <span>{t.role}</span>
                  <select className="select" value={userModal.role} onChange={(event) => patchUserModal("role", event.target.value)} disabled={userBusy || userModal.is_owner || userModal.id === currentUser?.id}>
                    {(userModal.id === currentUser?.id ? [currentUser.role] : userModal.is_owner ? ["owner"] : roleOptionsFor(currentUser)).map((role) => (
                      <option key={role} value={role}>{roleLabel(role, t)}</option>
                    ))}
                  </select>
                </label>

                <label className="settingsModalCheck">
                  <input type="checkbox" checked={userModal.is_active} onChange={(event) => patchUserModal("is_active", event.target.checked)} disabled={userBusy || userModal.id === currentUser?.id} />
                  <span>{t.active}</span>
                </label>
              </div>

              {userModal.error ? <div className="settingsModalError">{userModal.error}</div> : null}

              <div className="settingsModalActions">
                <button type="button" className="button secondary small" onClick={() => setUserModal(null)} disabled={userBusy}>{t.cancel}</button>
                <button type="submit" className="button small" disabled={userBusy}>{userModal.mode === "create" ? t.create : t.update}</button>
              </div>
            </form>
          </div>
        ) : null}

        {maintenanceModalOpen ? (
          <div className="settingsModalOverlay" role="presentation">
            <div className="settingsMaintenanceModal" role="dialog" aria-modal="true" aria-label={t.maintenanceOverview}>
              <div className="settingsUserModalHeader">
                <h2>{t.maintenanceOverview}</h2>
                <button type="button" className="settingsModalClose" onClick={closeMaintenanceModal} aria-label={t.close}>×</button>
              </div>

              <div className="settingsMaintenanceToolbar">
                <button type="button" className="button secondary small" onClick={loadMaintenanceOverview} disabled={maintenanceLoading || Boolean(maintenanceBusy)}>
                  {maintenanceLoading ? t.checking : t.maintenanceRefresh}
                </button>
                <button type="button" className="button secondary small" onClick={viewMaintenanceReport} disabled={Boolean(maintenanceBusy)}>
                  {maintenanceBusy === "report" ? t.checking : t.maintenanceReportView}
                </button>
                <button type="button" className="button secondary small" onClick={downloadMaintenanceReport} disabled={Boolean(maintenanceBusy)}>
                  {maintenanceBusy === "report-download" ? t.checking : t.maintenanceReportDownload}
                </button>
              </div>

              {maintenanceError ? <div className="settingsJournalEmpty error">{maintenanceError}</div> : null}
              {maintenanceLoading && !maintenanceOverview ? <div className="settingsJournalEmpty">{t.checking}</div> : null}

              {maintenanceOverview ? (
                <>
                  <div className="settingsMaintenanceGrid">
                    {maintenanceFlowRows(maintenanceOverview).map((flow) => (
                      <article className="settingsMaintenanceFlow" key={flow.key}>
                        <div className="settingsMaintenanceFlowHead">
                          <h3>{t.maintenanceFlows?.[flow.key] || flow.key}</h3>
                          <span className={`settingsMaintenanceStatus ${maintenanceStatusClass(flow.status)}`}>
                            {maintenanceStatusText(flow.status, t)}
                          </span>
                        </div>
                        <p>{flow.reason || t.maintenanceUnsupported}</p>
                        <dl>
                          {maintenanceDetailRows(flow, t).map(([label, value]) => (
                            <div key={label}>
                              <dt>{label}</dt>
                              <dd>{String(value)}</dd>
                            </div>
                          ))}
                        </dl>
                        <button type="button" className="button secondary small" onClick={() => runMaintenanceDryRun(flow.key)} disabled={Boolean(maintenanceBusy)}>
                          {maintenanceBusy === flow.key ? t.checking : t.maintenanceDryRun}
                        </button>
                      </article>
                    ))}
                  </div>

                  <section className="settingsMaintenanceReport">
                    <div>
                      <span>{t.maintenanceReport}</span>
                      <strong>{maintenanceOverview.upgrade_report?.status || "-"}</strong>
                      <small>
                        {t.maintenanceGeneratedAt}: {maintenanceOverview.upgrade_report?.generated_at || "-"} · {t.maintenanceWarnings}: {maintenanceOverview.upgrade_report?.warnings_count ?? 0}
                      </small>
                    </div>
                    <div>
                      <span>{t.maintenanceLastAction}</span>
                      <strong>{maintenanceOverview.history?.last_action?.available ? maintenanceOverview.history.last_action.status : t.maintenanceNoHistory}</strong>
                      <small>{maintenanceOverview.history?.last_action?.reason || t.maintenanceLimitedHistory}</small>
                    </div>
                  </section>

                  <section className="settingsUpdateApplyPanel">
                    <div className="settingsUpdateApplyHead">
                      <div>
                        <h3>{t.updateApplyTitle}</h3>
                        <p>{updateApplyTransientError || t.updateApplyConnection}</p>
                      </div>
                      <span className={`settingsMaintenanceStatus ${maintenanceStatusClass(updateApplyStatus?.status || updateStatus?.status)}`}>
                        {maintenanceStatusText(updateApplyStatus?.status || updateStatus?.status, t)}
                      </span>
                    </div>
                    <dl className="settingsUpdateApplyFacts">
                      <div><dt>{t.updateCurrent}</dt><dd>{updateInstalled.app_version || updateStatus?.installed?.installed_version || "-"}</dd></div>
                      <div><dt>{t.updateLatest}</dt><dd>{updateLatest.version || updateLatest.latest_version || "-"}</dd></div>
                      <div><dt>{t.updateSource}</dt><dd>{updateLatest.git_ref || updateLatest.source_ref || updateStatus?.source_channel?.source_channel_id || "-"}</dd></div>
                    </dl>
                    <div className="settingsUpdateApplyActions">
                      <button type="button" className="button secondary small" onClick={() => runMaintenanceDryRun("update")} disabled={Boolean(maintenanceBusy)}>
                        {maintenanceBusy === "update" ? t.checking : t.updateApplyCheck}
                      </button>
                      <button type="button" className="button primary small" onClick={startUpdateApply} disabled={!updateApplyAllowed}>
                        {maintenanceBusy === "update-apply" || updateApplyRunning ? t.checking : t.updateApplyStart}
                      </button>
                    </div>
                    {updateApplyStatus?.error?.message ? <small className="settingsUpdateApplyError">{updateApplyStatus.error.message}</small> : null}
                  </section>

                  {maintenanceActionResult ? (
                    <div className={`settingsMaintenanceResult ${maintenanceStatusClass(maintenanceActionResult.status)}`}>
                      <strong>{t.maintenanceFlows?.[maintenanceActionResult.flowKey] || maintenanceActionResult.flowKey}: {maintenanceStatusText(maintenanceActionResult.status, t)}</strong>
                      {maintenanceActionResult.reason ? <span>{maintenanceActionResult.reason}</span> : null}
                    </div>
                  ) : null}

                  {maintenanceReport ? (
                    <section className="settingsMaintenanceReportPreview">
                      <h3>{t.maintenanceReport}</h3>
                      <dl>
                        <div><dt>{t.maintenanceLabels?.reportId}</dt><dd>{maintenanceReport.report_id || "-"}</dd></div>
                        <div><dt>{t.maintenanceGeneratedAt}</dt><dd>{maintenanceReport.generated_at || "-"}</dd></div>
                        <div><dt>{t.maintenanceWarnings}</dt><dd>{(maintenanceReport.warnings || []).length}</dd></div>
                        <div><dt>{t.maintenanceFlows?.db_adoption}</dt><dd>{maintenanceStatusText(maintenanceReport.db_adoption?.status, t)}</dd></div>
                        <div><dt>{t.maintenanceFlows?.migration}</dt><dd>{maintenanceStatusText(maintenanceReport.migration_maintenance?.status, t)}</dd></div>
                        <div><dt>{t.maintenanceFlows?.restore}</dt><dd>{maintenanceStatusText(maintenanceReport.restore_maintenance?.status, t)}</dd></div>
                        <div><dt>{t.maintenanceFlows?.update}</dt><dd>{maintenanceStatusText(maintenanceReport.update_maintenance?.status, t)}</dd></div>
                      </dl>
                    </section>
                  ) : null}
                </>
              ) : null}
            </div>
          </div>
        ) : null}

        {securityModalOpen ? (
          <div className="settingsModalOverlay" role="presentation">
            <div className="settingsSecurityModal" role="dialog" aria-modal="true" aria-label={t.security}>
              <div className="settingsUserModalHeader">
                <h2>{t.security}</h2>
                <button type="button" className="settingsModalClose" onClick={closeSecurityModal} aria-label={t.close}>×</button>
              </div>

              <section className="settingsSecurityModalSection">
                <div className="settingsSecurityModalSectionHead">
                  <h3>{t.logJournal}</h3>
                </div>
                <div className="settingsAuditFilters" aria-label={t.journalFilters}>
                  <label>
                    <span>{t.journalCategory}</span>
                    <select className="select" value={auditFilters.category} onChange={(event) => patchAuditFilter("category", event.target.value)}>
                      <option value="">{t.journalAll}</option>
                      {AUDIT_CATEGORIES.map((category) => (
                        <option key={category} value={category}>{auditLabel("category", category, lang)}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>{t.journalSeverity}</span>
                    <select className="select" value={auditFilters.severity} onChange={(event) => patchAuditFilter("severity", event.target.value)}>
                      <option value="">{t.journalAll}</option>
                      {AUDIT_SEVERITIES.map((severity) => (
                        <option key={severity} value={severity}>{auditLabel("severity", severity, lang)}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>{t.journalSince}</span>
                    <select className="select" value={auditFilters.since} onChange={(event) => patchAuditFilter("since", event.target.value)}>
                      <option value="60">{t.journalSince60}</option>
                      <option value="360">{t.journalSince360}</option>
                      <option value="1440">{t.journalSince1440}</option>
                      <option value="">{t.journalSinceAll}</option>
                    </select>
                  </label>
                  <label>
                    <span>{t.journalActor}</span>
                    <input className="input" value={auditFilters.actor} onChange={(event) => patchAuditFilter("actor", event.target.value)} />
                  </label>
                  <label>
                    <span>{t.journalTarget}</span>
                    <input className="input" value={auditFilters.target} onChange={(event) => patchAuditFilter("target", event.target.value)} />
                  </label>
                  <label>
                    <span>{t.journalSearch}</span>
                    <input className="input" value={auditFilters.q} onChange={(event) => patchAuditFilter("q", event.target.value)} />
                  </label>
                </div>
                {auditLoading ? (
                  <div className="settingsJournalEmpty">{t.journalLoading || "Loading journal..."}</div>
                ) : auditError ? (
                  <div className="settingsJournalEmpty error">{auditError}</div>
                ) : auditEvents.length ? (
                  <>
                    <div className="settingsAuditList">
                      {auditEvents.map((event) => {
                        const metadataRows = safeMetadataRows(event.metadata);
                        return (
                          <article className={`settingsAuditItem severity-${event.severity || "info"} category-${event.category || "system"}`} key={event.id}>
                            <div className="settingsAuditMeta">
                              <time>{formatAuditTimestamp(event.created_at, lang)}</time>
                              <span>{event.actor_username || t.journalSystemActor || "system"}</span>
                              <span>{auditLabel("category", event.category, lang)}</span>
                              <span>{auditLabel("severity", event.severity, lang)}</span>
                              <span>{auditTarget(event, t)}</span>
                            </div>
                            <div className="settingsAuditMessage">{auditMessage(event, lang) || event.event_type}</div>
                            <div className="settingsAuditEventType">{t.journalEventType}: {event.event_type}</div>
                            {metadataRows.length ? (
                              <details className="settingsAuditMetadata">
                                <summary>{t.journalMetadata}</summary>
                                <dl>
                                  {metadataRows.map((row) => (
                                    <div key={row.key}>
                                      <dt>{row.key}</dt>
                                      <dd>{row.value}</dd>
                                    </div>
                                  ))}
                                </dl>
                              </details>
                            ) : null}
                          </article>
                        );
                      })}
                    </div>
                    {auditHasMore ? (
                      <button type="button" className="button secondary small settingsAuditLoadMore" onClick={() => loadAuditEvents(auditOffset)} disabled={auditLoading}>
                        {t.journalLoadMore}
                      </button>
                    ) : null}
                  </>
                ) : (
                  <div className="settingsJournalEmpty">{t.journalEmpty}</div>
                )}
              </section>

              <section className="settingsSecurityModalSection">
                <div className="settingsSecurityModalSectionHead">
                  <h3>{t.bugReport}</h3>
                </div>
                <button className="button secondary small settingsSecurityModalButton" onClick={() => setDiagnosticChoiceOpen(true)} disabled={securityBusy}>
                  {t.createDiagnosticArchive}
                </button>
                <textarea
                  className="input settingsBugReportTextarea"
                  value={bugReportText}
                  onChange={(event) => patchBugReportText(event.target.value)}
                  placeholder={t.bugReportPlaceholder}
                  disabled={securityBusy}
                />
                {diagnosticArchive ? (
                  <div className="settingsAttachmentBadge">
                    <span>✓</span>
                    <strong>{t.diagnosticArchiveReady}</strong>
                    <small>{diagnosticArchive.filename}</small>
                  </div>
                ) : null}
                <div className="settingsSecurityNote">{t.reportSendingPending}</div>
                <button
                  className="button small settingsSecurityModalButton"
                  onClick={submitBugReport}
                  disabled={securityBusy || !diagnosticArchive || !bugReportText.trim()}
                >
                  {t.sendBugReport}
                </button>
              </section>

              {diagnosticChoiceOpen ? (
                <div className="settingsDiagnosticChoiceOverlay" role="presentation">
                  <div className="settingsDiagnosticChoice" role="dialog" aria-modal="true" aria-label={t.diagnosticArchiveQuestion}>
                    <h3>{t.diagnosticArchiveQuestion}</h3>
                    <div className="settingsDiagnosticChoiceActions">
                      <button
                        type="button"
                        className="button secondary small settingsDiagnosticChoiceButton"
                        onClick={() => downloadLogArchive("normal")}
                        disabled={securityBusy}
                      >
                        {t.diagnosticArchiveNormal}
                      </button>
                      <button
                        type="button"
                        className="button small settingsDiagnosticChoiceButton"
                        onClick={() => downloadLogArchive("extended")}
                        disabled={securityBusy}
                      >
                        {t.diagnosticArchiveExtended}
                      </button>
                    </div>
                    <button type="button" className="settingsModalClose settingsDiagnosticChoiceClose" onClick={() => setDiagnosticChoiceOpen(false)} aria-label={t.close}>×</button>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        ) : null}
      </div>
    </Layout>
  );
}
