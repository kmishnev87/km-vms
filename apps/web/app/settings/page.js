"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Layout from "../../components/Layout";
import { CheckIcon, EditIcon, PowerIcon, TrashIcon } from "../../components/CompactActionIcons";
import { OperationDialog, OperationToast } from "../../components/OperationFeedback";
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
  formatMaintenanceMessage,
  hardwareOptionState,
  humanErrorText,
  languageOf,
  maintenanceBackupDetailModel,
  maintenanceBackupOverviewModel,
  maintenanceBackupOperationResultText,
  maintenanceBackupValidOffset,
  maintenanceDatabaseOverviewModel,
  maintenanceOverallHealthModel,
  maintenanceStatusClass,
  maintenanceStatusText,
  maintenanceWarningModel,
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
  UPDATE_APPLY_POLL_INTERVAL_MS,
  UPDATE_APPLY_PENDING_STORAGE_KEY,
  BACKUP_OPERATION_PENDING_STORAGE_KEY,
  backupOperationWithinAdmissionGrace,
  createBackupOperationPending,
  createUpdateApplyPending,
  reconcileUpdateApplyPending,
  restoreUpdateApplyPending,
  restoreBackupOperationPending,
  sanitizeBackupOperationPending,
  sanitizeUpdateApplyPending,
  shortCommit,
  updateApplyCandidateSnapshot,
  updateApplyErrorMessages,
  updateApplyOperatorModel,
  updateApplyButtonText,
  updateApplyIsRunning,
  updateApplyReconnectTiming,
  userCanBeDeleted,
  userCanBeManaged,
} from "../../lib/settingsPageHelpers";

configureSettingsPageHelpers({ normalizeLocale, translateText });

const CREDENTIALS_CHANGED_NOTICE_KEY = "km_vms_credentials_changed_notice";
const MAINTENANCE_BACKUP_PAGE_SIZE = 5;
const MAINTENANCE_BACKUP_POLL_INTERVAL_MS = 3000;
const CURRENT_RESTORE_PENDING_STORAGE_KEY = "km_vms_current_restore_pending_v1";
const CURRENT_RESTORE_CONFIRMATION_PHRASE = "RESTORE KM VMS";
const CURRENT_RESTORE_OPERATIONAL_PHASES = [
  "preflight",
  "pre_restore_backup",
  "writers_paused",
  "restore_running",
  "services_starting",
  "post_restore_check",
];
const CURRENT_RESTORE_LEGACY_REASON_PHASES = {
  pre_restore_backup_verification_failed: "pre_restore_backup",
  restore_writer_isolation_failed: "writers_paused",
  automatic_rollback_isolation_failed: "writers_paused",
  pg_restore_failed: "restore_running",
  pre_restore_backup_missing: "restore_running",
  restore_interrupted_after_mutation: "restore_running",
  automatic_rollback_database_failed: "restore_running",
  restore_api_health_failed: "services_starting",
  automatic_rollback_api_recovery_failed: "services_starting",
  automatic_rollback_validation_failed: "post_restore_check",
  post_restore_actor_access_invalid: "post_restore_check",
  post_restore_schema_invalid: "post_restore_check",
  post_restore_metadata_invalid: "post_restore_check",
  post_restore_tables_missing: "post_restore_check",
  restore_recorder_start_failed: "post_restore_check",
  automatic_rollback_recorder_recovery_failed: "post_restore_check",
};

let settingsBodyScrollLockCount = 0;
let settingsBodyPreviousOverflow = "";

function acquireSettingsBodyScrollLock() {
  if (typeof document === "undefined") return () => {};
  if (settingsBodyScrollLockCount === 0) {
    settingsBodyPreviousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
  }
  settingsBodyScrollLockCount += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    settingsBodyScrollLockCount = Math.max(0, settingsBodyScrollLockCount - 1);
    if (settingsBodyScrollLockCount === 0) {
      document.body.style.overflow = settingsBodyPreviousOverflow;
      settingsBodyPreviousOverflow = "";
    }
  };
}

function monotonicWallNow() {
  if (typeof performance !== "undefined" && Number.isFinite(performance.timeOrigin) && typeof performance.now === "function") {
    return performance.timeOrigin + performance.now();
  }
  return Date.now();
}

function focusableElements(container) {
  if (!container) return [];
  return Array.from(container.querySelectorAll(
    'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])',
  )).filter((element) => !element.hasAttribute("hidden") && element.getAttribute("aria-hidden") !== "true");
}

function updateApplyRequestIsAmbiguous(error) {
  return error?.category === "network_unavailable" ||
    error?.category === "temporarily_unavailable" ||
    Number(error?.status || 0) === 0 ||
    Number(error?.status || 0) >= 500;
}

function createUpdateApplySubmissionId() {
  const browserCrypto = globalThis.crypto;
  if (typeof browserCrypto?.randomUUID === "function") {
    return browserCrypto.randomUUID();
  }
  if (typeof browserCrypto?.getRandomValues !== "function") return "";
  const bytes = browserCrypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
  return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
}

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
    maintenanceText: "Обновление, обслуживание: БД, миграций, восстановления и отчёта.",
    maintenanceOverview: "Обзор обслуживания",
    maintenanceOverallHealthy: "Система работает нормально",
    maintenanceOverallHealthyText: "Обновление, база данных, резервные копии и диагностика доступны.",
    maintenanceOverallAttention: "Есть пункты, требующие внимания",
    maintenanceOverallAttentionText: "Основные функции доступны, но один из разделов обслуживания требует проверки.",
    maintenanceOverallNoBackupText: "Система работает, но резервных копий базы данных пока нет.",
    maintenanceOverallBlocked: "Обслуживание требует действий",
    maintenanceOverallBlockedText: "Одна из важных операций недоступна. Откройте соответствующий раздел или скачайте отчёт.",
    maintenanceOverallUnknown: "Состояние обслуживания уточняется",
    maintenanceOverallUnknownText: "Не все данные получены. Обновите обзор или повторите попытку позже.",
    maintenanceRefresh: "Обновить",
    maintenanceLoadError: "Обзор обслуживания недоступен.",
    maintenanceLimitedHistory: "Долговременная история ограничена: показаны текущий статус и последний безопасный отчёт.",
    maintenanceReport: "Отчёт обслуживания",
    maintenanceReportDownload: "Скачать отчёт",
    maintenanceReportReady: "Отчёт подготовлен. Конфиденциальные данные скрыты.",
    maintenanceReportUnavailable: "Отчёт недоступен.",
    maintenanceBackupCreate: "Создать резервную копию БД",
    maintenanceBackupCreating: "Создание копии...",
    maintenanceBackupCreateConfirm: "Создать резервную копию базы данных KM VMS? Видеоархивы и записи камер в эту копию не входят.",
    maintenanceBackupCreated: "Резервная копия БД создана.",
    maintenanceBackupCreateFailed: "Не удалось создать резервную копию БД.",
    maintenanceBackupScope: "Резервная копия включает базу данных и служебные метаданные. Видеоархивы и записи камер не копируются.",
    maintenanceBackupResult: "Последняя резервная копия",
    maintenanceBackupsTitle: "Резервные копии",
    maintenanceBackupsText: "Создание, проверка и удаление копий базы и служебных метаданных.",
    maintenanceDatabaseTitle: "База данных",
    maintenanceBackupOpenList: "Открыть список",
    maintenanceBackupBackToOverview: "К обзору",
    maintenanceLastUpdate: "Последнее обновление",
    maintenanceBackupCreateShort: "Создать",
    maintenanceBackupCheck: "Проверить",
    maintenanceBackupCheckPassedTitle: "Проверка пройдена",
    maintenanceBackupCheckPassedText: "Копия доступна, цела и совместима; пробное восстановление пройдено.",
    maintenanceBackupDelete: "Удалить",
    maintenanceBackupDeleting: "Удаление...",
    maintenanceBackupDeleted: "Резервная копия удалена.",
    maintenanceBackupDeleteFailed: "Не удалось удалить резервную копию.",
    maintenanceBackupDeleteConfirm: "Удалить резервную копию от {date}? Это удалит только продуктовые файлы этой копии. Видеоархивы не затрагиваются.",
    maintenanceBackupRestore: "Восстановить",
    maintenanceCurrentRestoreAction: "Восстановить рабочую базу",
    maintenanceCurrentRestoreTitle: "Восстановление рабочей базы данных",
    maintenanceCurrentRestoreIntro: "Выбрана копия от {date}. Перед запуском система ещё раз проверит именно эту копию.",
    maintenanceCurrentRestoreChanges: "Пользователи, камеры, настройки и служебные метаданные вернутся к состоянию этой копии.",
    maintenanceCurrentRestoreVideoSafe: "Видеоархив и файлы записей не восстанавливаются и не удаляются.",
    maintenanceCurrentRestoreBackupFirst: "Перед заменой базы система создаст и проверит страховочную копию её текущего состояния. При неуспехе выбранного восстановления база автоматически вернётся из этой копии; видеоархив в неё не входит и не изменяется.",
    maintenanceCurrentRestoreInterruption: "Во время операции API и запись камер будут кратковременно остановлены.",
    maintenanceCurrentRestoreActor: "Текущий администратор должен существовать и быть активным в выбранной копии.",
    maintenanceCurrentRestorePhraseLabel: "Для подтверждения введите RESTORE KM VMS",
    maintenanceCurrentRestoreConfirm: "Восстановить",
    maintenanceCurrentRestorePreflight: "Проверяем копию и условия восстановления...",
    maintenanceCurrentRestoreBlocked: "Эта копия сейчас не готова к восстановлению.",
    maintenanceCurrentRestoreReconnect: "Сервисы перезапускаются. Статус будет проверен автоматически.",
    maintenanceCurrentRestoreCompleted: "Рабочая база восстановлена. При необходимости войдите в систему снова.",
    maintenanceCurrentRestoreRolledBack: "Восстановление не завершилось; исходная рабочая база автоматически возвращена из предохранительной копии.",
    maintenanceCurrentRestoreRecoveryRequired: "Автоматическое возвращение базы не подтверждено. Не запускайте операцию повторно и обратитесь в поддержку.",
    maintenanceCurrentRestoreClose: "Закрыть",
    maintenanceCurrentRestoreCancel: "Отмена",
    maintenanceCurrentRestoreTerminalPhase: "Результат",
    maintenanceCurrentRestorePhraseMismatch: "Введите фразу подтверждения точно как указано.",
    maintenanceCurrentRestoreRequestRejected: "Запуск восстановления отклонён.",
    maintenanceCurrentRestorePhases: {
      preflight: "Проверка условий",
      pre_restore_backup: "Подготовка резервной копии текущей базы",
      writers_paused: "Службы записи остановлены",
      restore_running: "Восстановление базы",
      services_starting: "Запуск служб",
      post_restore_check: "Итоговая проверка",
      completed: "Завершено",
      blocked: "Операция заблокирована",
      failed_rolled_back: "Исходная база возвращена",
      failed_recovery_required: "Требуется восстановление с поддержкой",
    },
    maintenanceCurrentRestoreReasons: {
      artifact_invalid: "Некорректная копия.",
      artifact_unavailable: "Файлы копии недоступны.",
      artifact_integrity_not_verified: "Сначала проверьте целостность копии.",
      migration_required: "Схема этой копии требует миграции и здесь не восстанавливается.",
      newer_than_supported: "Схема копии новее текущей версии приложения.",
      restore_not_supported: "Восстановление рабочей базы сейчас недоступно.",
      initiating_actor_missing_or_inactive_in_backup: "Текущий администратор отсутствует или неактивен в этой копии.",
      insufficient_space_for_pre_restore_backup: "Недостаточно места для предохранительной копии текущей базы.",
      backup_root_not_persistent: "Каталог резервных копий не подтверждён как постоянный.",
      update_operation_active: "Сначала дождитесь завершения обновления.",
      backup_operation_active: "Сначала дождитесь завершения операции с резервной копией.",
      schema_operation_active: "Сначала дождитесь завершения изменения схемы.",
      restore_helper_unavailable: "Служба восстановления пока недоступна.",
      restore_status_reader_unavailable: "Служба безопасного статуса восстановления пока недоступна.",
      postgresql_required: "Восстановление рабочей базы поддерживается только для PostgreSQL.",
      schema_migration_required: "Текущая схема базы требует миграции.",
      schema_newer_than_supported: "Текущая схема базы новее этой версии приложения.",
      artifact_backend_unsupported: "Копия создана для неподдерживаемого типа базы.",
      artifact_schema_migration_required: "Схема копии требует миграции.",
      artifact_schema_newer: "Схема копии новее этой версии приложения.",
      artifact_integrity_evidence_stale: "Копия изменилась после проверки целостности.",
      temporary_restore_validation_required: "Сначала выполните пробное восстановление этой копии.",
      temporary_restore_validation_failed: "Пробное восстановление этой копии не прошло.",
      restore_operation_active: "Другое восстановление уже выполняется.",
      submission_binding_conflict: "Этот идентификатор запуска уже связан с другой операцией.",
      artifact_evidence_changed: "Копия изменилась после предварительной проверки.",
      confirmation_required: "Требуется явное подтверждение восстановления.",
      confirmation_phrase_invalid: "Фраза подтверждения введена неверно.",
      audit_unavailable: "Не удалось надёжно записать событие аудита. Восстановление не запущено.",
      current_actor_access_changed: "Доступ текущего администратора изменился. Повторите предварительную проверку.",
      artifact_fingerprint_changed: "Копия изменилась после принятия операции.",
      current_schema_not_exact: "Текущая схема базы больше не соответствует поддерживаемой версии.",
      pre_restore_backup_verification_failed: "Не удалось создать и проверить предохранительную копию текущей базы.",
      restore_writer_isolation_failed: "Не удалось безопасно остановить службы, записывающие данные.",
      pg_restore_failed: "Восстановление базы завершилось с ошибкой.",
      restore_api_health_failed: "После восстановления API не прошёл проверку здоровья.",
      restore_recorder_start_failed: "После проверки базы не удалось запустить запись.",
      automatic_rollback_failed: "Автоматический возврат исходной базы не завершился. Обратитесь в поддержку.",
      automatic_rollback_isolation_failed: "Не удалось безопасно остановить службы записи для автоматического возврата базы. Возврат базы не выполнен; обратитесь в поддержку.",
      automatic_rollback_database_failed: "Автоматический возврат базы не завершён или не подтверждён. Не повторяйте операцию и обратитесь в поддержку.",
      automatic_rollback_api_recovery_failed: "Страховочная копия базы возвращена, но API не запустился. Полная готовность приложения не подтверждена.",
      automatic_rollback_validation_failed: "Страховочная копия базы возвращена, но итоговая проверка не завершилась. Полная готовность приложения не подтверждена.",
      automatic_rollback_recorder_recovery_failed: "База возвращена и API проверен, но служба записи камер не восстановилась. Полная готовность приложения не подтверждена.",
      restore_interrupted_before_mutation: "Операция прервалась до изменения рабочей базы.",
      restore_interrupted_after_mutation: "Операция прервалась после начала изменения; выполнен автоматический возврат.",
      restore_helper_exception: "Служба восстановления завершила операцию с внутренней ошибкой.",
      post_restore_actor_access_invalid: "В восстановленной базе не подтверждён доступ администратора.",
      post_restore_schema_invalid: "Восстановленная база имеет неподдерживаемую схему.",
      post_restore_metadata_invalid: "Проверка основных данных восстановленной базы не пройдена.",
      post_restore_tables_missing: "В восстановленной базе отсутствуют обязательные таблицы.",
    },
    maintenanceBackupStatusEmpty: "Резервных копий пока нет.",
    maintenanceBackupStatusReady: "Резервных копий: {count}",
    maintenanceBackupCopyOne: "копия",
    maintenanceBackupCopyMany: "копий",
    maintenanceBackupNoCopies: "Нет копий",
    maintenanceBackupLatest: "Последняя копия",
    maintenanceBackupProblems: "Проблемных",
    maintenanceBackupSize: "Размер",
    maintenanceBackupSchema: "Схема",
    maintenanceBackupList: "Последние копии",
    maintenanceBackupNothingToCheck: "Проверять пока нечего",
    maintenanceBackupCheckConfirm: "Проверить копию от {date} реальным восстановлением во временную базу? Рабочая база и видеоархив не изменятся.",
    maintenanceBackupChecking: "Проверяем резервную копию во временной базе…",
    maintenanceBackupRecovering: "Уточняем состояние ранее начатой проверки…",
    maintenanceBackupTotalSize: "Общий размер",
    maintenanceBackupPage: "{start}–{end} из {total}",
    maintenanceBackupPrevious: "Назад",
    maintenanceBackupNext: "Далее",
    maintenanceBackupIntegrity: "Целостность",
    maintenanceBackupCompatibility: "Совместимость",
    maintenanceBackupValidation: "Пробное восстановление",
    maintenanceBackupAvailability: "Доступность",
    maintenanceBackupCheckedAt: "Проверено: {date}",
    maintenanceBackupValidatedAt: "Восстановление проверено: {date}",
    maintenanceBackupAvailabilityStatuses: {
      available: "Доступна",
      incomplete: "Неполная",
      missing: "Файл отсутствует",
      unsafe: "Небезопасная структура",
      unknown: "Неизвестно",
    },
    maintenanceBackupIntegrityStatuses: {
      not_checked: "Не проверена",
      verified: "Цела",
      failed: "Повреждена",
      stale_evidence: "Нужна повторная проверка",
      unknown: "Неизвестно",
    },
    maintenanceBackupCompatibilityStatuses: {
      compatible: "Совместима",
      migration_required: "Нужна миграция",
      newer_than_supported: "Создана более новой версией",
      unsupported_backend: "Неподдерживаемый тип БД",
      unknown: "Неизвестно",
    },
    maintenanceBackupValidationStatuses: {
      not_performed: "Не выполнялось",
      passed: "Пройдена",
      failed: "Не пройдена",
      stale_evidence: "Нужна повторная проверка",
      unknown: "Неизвестно",
    },
    maintenanceBackupOperationLabels: {
      check: "Проверка",
      create: "Создание",
      delete: "Удаление",
    },
    maintenanceBackupStatuses: {
      valid: "Готова",
      verified: "Готова",
      available: "Доступна",
      no_artifacts: "Нет копий",
      blocked: "Проверка заблокирована",
      invalid: "Проблема",
      check_failed: "Проверка не прошла",
      validated: "Пробное восстановление пройдено",
      passed: "Пробное восстановление пройдено",
      interrupted: "Проверка прервана; её можно повторить",
      problem: "Проблема",
    },
    maintenanceBackupCheckStatuses: {
      valid: "Проверка пройдена",
      verified: "Проверка пройдена",
      validated: "Копия доступна, цела и совместима; пробное восстановление пройдено.",
      passed: "Копия доступна, цела и совместима; пробное восстановление пройдено.",
      completed: "Проверка завершена",
      available: "Копия доступна для проверки",
      no_artifacts: "Проверять пока нечего",
      blocked: "Проверка заблокирована",
      invalid: "Копия повреждена или неполная",
      check_failed: "Проверка не прошла",
      fallback: "Статус проверки получен",
    },
    maintenanceBackupCheckOutcomes: {
      fully_validated: "Копия доступна, цела и совместима; пробное восстановление пройдено.",
      integrity_verified_migration_required: "Целостность подтверждена. Пробное восстановление не выполнялось: для этой копии требуется совместимая миграция.",
      integrity_failed: "Целостность копии не подтверждена: копия повреждена или неполная.",
      restore_failed: "Целостность копии подтверждена, но пробное восстановление не прошло.",
    },
    maintenanceBackupCreateStatuses: {
      created: "Резервная копия создана",
      verified: "Резервная копия создана",
      valid: "Резервная копия создана",
      completed: "Резервная копия создана",
      blocked: "Создание заблокировано",
      failed: "Не удалось создать резервную копию",
      fallback: "Статус создания получен",
    },
    maintenanceBackupDeleteStatuses: {
      deleted: "Резервная копия удалена",
      deleted_with_missing_files: "Резервная копия удалена; часть файлов уже отсутствовала",
      blocked: "Удаление заблокировано",
      failed: "Не удалось удалить резервную копию",
      partial_retryable: "Удаление завершено частично; его можно повторить",
      delete_failed: "Не удалось удалить резервную копию",
      not_found: "Резервная копия уже не найдена",
      fallback: "Статус удаления получен",
    },
    maintenanceDbAdoptionApply: "Принять базу",
    maintenanceDbAdoptionApplyConfirm: "Принять существующую базу данных как рабочую базу KM VMS? Действие выполнится только после встроенных проверок и резервной копии.",
    maintenanceDbAdoptionApplied: "Существующая база данных принята.",
    maintenanceDbAdoptionApplyFailed: "Не удалось принять существующую базу данных.",
    maintenanceDryRun: "Проверить",
    maintenanceDryRunResult: "Проверка выполнена.",
    maintenanceNoApply: "Обновление применяется только через подтверждённый helper-процесс.",
    maintenanceBackupRequired: "Требуется резервная копия",
    maintenanceBackupNotRequired: "Резервная копия не требуется",
    maintenanceConfirmationRequired: "Нужно подтверждение",
    maintenanceUnsupported: "Действие заблокировано или не поддерживается",
    yes: "Да",
    no: "Нет",
    maintenanceLastAction: "Последнее действие",
    maintenanceNoHistory: "История действий не найдена",
    maintenanceGeneratedAt: "Сформирован",
    maintenanceWarnings: "Диагностика",
    maintenanceWarningDetails: "Показать детали",
    maintenanceWarningHide: "Скрыть детали",
    maintenanceSupportStatusOk: "Без критичных проблем",
    maintenanceWarningActionable: "Требуют внимания",
    maintenanceWarningInfo: "Информационные ограничения",
    maintenanceWarningSupport: "Для поддержки",
    maintenanceWarningNone: "Активных предупреждений нет.",
    maintenanceSupportReportAction: "Скачайте отчёт и передайте поддержке.",
    maintenanceWarningGeneric: {
      actionable: { title: "Требуется проверка", summary: "Есть состояние, которое может мешать обслуживанию.", action: "Скачайте отчёт и устраните причину по его данным." },
      informational: { title: "Ограничение диагностики", summary: "Это не авария: отчёт честно фиксирует, что часть проверки не выполнялась автоматически.", action: "Ничего делать не нужно, если система работает штатно." },
      support: { title: "Деталь для поддержки", summary: "Пункт важен для диагностики, но не всегда требует действий пользователя.", action: "Скачайте отчёт, если нужна помощь." },
    },
    maintenanceWarningLabels: {
      backup_status_source_unavailable: { title: "Нет подключённого источника статуса резервной копии", summary: "Отчёт не нашёл отдельный безопасный источник, который подтверждает состояние резервной копии в диагностике только для чтения.", action: "Используйте блок резервных копий для создания или проверки копии." },
      restore_validation_status_source_unavailable: { title: "Нет источника проверки восстановления", summary: "Диагностика не получила отдельное подтверждение, что резервную копию уже проверяли во временной базе.", action: "Запустите проверку копии в блоке резервных копий." },
      backup_root_persistence_unknown: { title: "Папка резервных копий не проверялась записью", summary: "Отчёт не выполнял пробную запись в папку резервных копий, чтобы не менять систему без команды.", action: "Это информационное ограничение отчёта." },
      video_archive_restore_not_covered: { title: "Видеоархивы не входят в резервную копию БД", summary: "Резервная копия защищает базу и служебные метаданные, но не копирует записи камер.", action: "Хранение видеоархива проверяется отдельно." },
      production_adoption_deferred: { title: "Служебное принятие БД отложено", summary: "Диагностика работает только для чтения и не меняет служебные метаданные без отдельного действия.", action: "Если система работает штатно, действий не требуется." },
      installed_build_development_fallback: { title: "Метаданные сборки неполные", summary: "Установленная среда не дала полный источник метаданных сборки.", action: "Проверьте идентичность релиза при следующем обновлении." },
    },
    maintenanceMessageFallback: "Статус получен, подробности недоступны.",
    maintenanceActionFallback: "Действие сейчас недоступно. Проверьте состояние системы и повторите позже.",
    updateApplyTitle: "Обновление KM VMS",
    updateApplyCheck: "Проверить обновление",
    updateApplyStart: "Обновить KM VMS",
    updateApplyConfirm: "Установить доступное обновление KM VMS?",
    updateApplyConfirmRestart: "Сервисы могут временно перезапуститься; статус продолжит обновляться после восстановления API.",
    updateApplyModalTitle: "Обновление KM VMS",
    updateApplyModalConfirm: "Обновить KM VMS",
    updateApplyLaunchChecking: "Проверяем запуск обновления",
    updateApplyLaunchCheckingText: "Ответ сервера временно недоступен. KM VMS проверяет статус и не отправляет повторный запрос.",
    updateApplyLaunchUnknown: "Результат запуска пока не подтверждён. Проверка продолжается автоматически; повторное применение заблокировано.",
    updateApplyLaunchConflict: "Обнаружена другая операция обновления. Повторное применение заблокировано до получения итогового статуса.",
    updateApplyLaunchNotAccepted: "Сервер подтвердил, что запрос не был принят. Для повторного запуска откройте новое подтверждение.",
    updateApplyLaunchRejected: "Сервер отклонил запуск обновления.",
    updateApplyLocked: "Проверяем запуск",
    updateApplyPeerCheckUnavailable: "Проверка опубликованного релиза временно недоступна; статус применения обновления получен.",
    updateApplyTransportErrors: {
      network_unavailable: "Нет связи с сервисом обновления. Проверка продолжится автоматически.",
      temporarily_unavailable: "Сервис обновления перезапускается или временно недоступен. Проверка продолжится автоматически.",
      unauthorized: "Для проверки обновления требуется повторный вход.",
      permission_denied: "Недостаточно прав для управления обновлением.",
      request_failed: "Не удалось получить статус обновления. Проверка продолжится автоматически.",
      typed_backend_error: "Сервис обновления сообщил об ошибке.",
    },
    updateApplyQueued: "Запрос на обновление KM VMS передан helper.",
    updateApplyUnavailable: "Действие сейчас недоступно. Проверьте сообщение в этом блоке и повторите проверку обновления.",
    updateApplyConnection: "Сервис может временно перезапускаться; опрос статуса продолжится автоматически.",
    updateApplyRecoveryAvailable: "Проверьте целевую версию и commit, затем запустите применение.",
    updateApplyRecoveryBlocked: "Устраните блокировку в trusted release или настройках сервера и повторите проверку.",
    updateApplyRecoveryCommitMismatch: "Установленный commit не совпал с trusted release commit. Считайте обновление неуспешным и повторите после проверки серверного источника.",
    updateApplyRecoveryCompleted: "Обновление завершено, установленный commit подтверждён.",
    updateApplyRecoveryCurrent: "Установленная версия соответствует trusted release.",
    updateApplyRecoveryFailed: "Обновление завершилось ошибкой. Проверьте статус обновления и повторите после устранения причины.",
    updateApplyRecoveryRolledBack: "Новая версия не прошла проверку. Приложение автоматически вернуло предыдущую рабочую версию; устраните причину перед повтором.",
    updateApplyRecoveryCheckFailed: "Проверка обновления не завершилась. Повторите проверку или скачайте отчёт для поддержки.",
    updateApplyRecoveryLiveCheckFailedWithSnapshot: "Последняя live-проверка не завершилась, но свежая trusted-проверка ещё доступна для безопасного применения.",
    updateApplyRecoveryRefreshRequired: "Release изменился или проверка устарела. Запустите «Проверить обновление» ещё раз.",
    updateApplyRecoveryMissingCommit: "У релиза нет trusted commit evidence. Обновление нельзя применить безопасно.",
    updateApplyRecoveryReconnecting: "Сервисы могут перезапускаться. Интерфейс продолжит опрос и перечитает статус после восстановления API.",
    updateApplyRecoveryRunning: "Helper выполняет обновление. Не закрывайте питание NAS и дождитесь итогового статуса.",
    updateApplyRecoveryStalled: "Статус обновления давно не менялся. Не запускайте повторно, пока helper или lock могут быть активны; проверьте состояние сервера.",
    updateApplyRecoveryUnknown: "Статус обновления пока неизвестен. Обновите проверку или дождитесь ответа API.",
    updateApplyRecoveryIdentity: "Метаданные установки неполные или расходятся с текущим кодом. Обновление заблокировано до принятия release identity.",
    updateApplyRecoveryProvider: "Источник релиза временно недоступен. Повторите проверку; свежая успешная trusted-проверка может использоваться только ограниченное время.",
    updateApplyRecoveryInstalledNewer: "Установленная версия новее опубликованной. Применение заблокировано, чтобы не откатить систему назад.",
    updateApplyTechnicalDetails: "Технические детали",
    updateApplyProgress: "Ход обновления",
    updateApplyButtonRunning: "Идёт обновление",
    updateApplyButtonRebuilding: "Пересборка",
    updateApplyButtonHealth: "Тестирование",
    updateApplyButtonVerification: "Проверка commit",
    updateApplyHeadlines: {
      current: "Система актуальна",
      available: "Доступно обновление",
      running: "Обновление выполняется",
      completed: "Завершено успешно",
      blocked: "Требуется внимание",
      unknown: "Статус обновления временно неизвестен",
    },
    updateApplySummaries: {
      current: "Установленная версия совпадает с опубликованным релизом.",
      available: "Проверьте релиз и запустите применение, когда будете готовы.",
      running: "Helper применяет обновление и обновляет прогресс автоматически.",
      completed: "Обновление установлено и подтверждено.",
      blocked: "Применение сейчас заблокировано; подробности есть в диагностике.",
      unknown: "Последняя полученная информация устарела. Проверка статуса продолжается автоматически.",
    },
    updateApplyResults: {
      completedVerified: "Завершено успешно",
      current: "Актуально",
      available: "Доступно обновление",
      running: "Выполняется",
      blocked: "Требуется внимание",
      unknown: "Статус временно неизвестен",
    },
    updateApplyReleaseChanges: "Что изменилось в этом релизе",
    updateApplyReleaseTitleFallback: "Релиз KM VMS",
    updateApplyReleaseSummaryFallback: "Описание релиза отсутствует.",
    updateApplyLastState: "Последний прогресс",
    updateApplyStepDone: "Готово",
    updateApplyTimelineCurrent: "Текущая версия",
    updateApplyHistoryLimited: "Детальная история шагов для прошлого применения не записана.",
    updateApplyDiagnostics: "Диагностика",
    updateApplySupportTitle: "Нужна помощь поддержки?",
    updateApplySupportText: "Скачайте диагностический отчёт и передайте его поддержке. Технические детали включены в отчёт.",
    updateApplySupportAction: "Скачать диагностический отчёт",
    updateCommitPending: "Ожидает подтверждения",
    updateCommitUnavailable: "Нет данных",
    updateCommitVerified: "Commit подтверждён",
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
      apply: "Обновление",
      installedCommit: "Установленный commit",
      reportId: "ID отчёта",
      source: "Источник",
      targetCommit: "Целевой commit",
      verification: "Проверка commit",
      currentStep: "Текущий шаг",
      lastProgress: "Последний прогресс",
      elapsed: "Прошло",
      completedAt: "Завершено",
      releaseIdentity: "Релиз подтверждён",
      releaseTitle: "Релиз",
      releaseSummary: "Изменения",
      status: "Обновление",
      gitHead: "Git HEAD",
      metadataSource: "Метаданные",
      provider: "Провайдер",
    },
    maintenanceFlows: {
      db_adoption: "Принятие БД",
      migration: "Миграции",
      restore: "Проверка резервной копии",
      update: "Обновление",
    },
    maintenanceReadinessTitle: "Готовность базы и восстановления",
    maintenanceReadinessText: "Состояние служебных операций, которые нужны только при обновлении, переносе или восстановлении системы.",
    maintenanceSupportTitle: "Диагностика для поддержки",
    maintenanceSupportText: "Если обслуживание выглядит непонятно или заблокировано, скачайте безопасный отчёт и передайте его поддержке.",
    maintenanceReadinessTitles: {
      db_identity: "База данных",
      db_schema: "Структура БД",
      backup_restore_check: "Проверка резервной копии",
    },
    maintenanceOperatorStatuses: {
      ok: "В порядке",
      attention: "Проверьте",
      blocked: "Требуется внимание",
      unavailable: "Нет данных",
      action_available: "Можно выполнить",
    },
    maintenanceOperatorSummaries: {
      db_identity_ok: "Приложение распознаёт эту базу как свою. Дополнительные действия не нужны.",
      db_identity_adoptable: "Базу можно безопасно принять после резервной копии и явного подтверждения.",
      db_identity_blocked: "База не готова к принятию. Нужна диагностика перед любыми действиями.",
      db_schema_current: "Структура базы актуальна для текущей версии приложения.",
      db_schema_pending: "Для этой версии приложения ожидаются изменения структуры базы.",
      db_schema_blocked: "Проверка структуры базы заблокирована. Нужна диагностика.",
      backup_restore_no_artifacts: "В настроенной папке нет проверенных резервных копий для восстановления.",
      backup_restore_artifacts_available: "Есть резервные копии, которые можно проверить во временной базе.",
      backup_restore_validation_available: "Можно выполнить безопасную проверку восстановления во временной базе.",
      backup_restore_blocked: "Проверка резервной копии заблокирована. Нужна диагностика.",
    },
    maintenanceOperatorActions: {
      db_identity_check_optional: "Обычно этот пункт не требует действий.",
      db_identity_apply_requires_confirmation: "Принятие меняет только служебные метаданные и требует отдельного подтверждения.",
      migration_check_optional: "Обычно этот пункт не требует действий.",
      migration_apply_requires_confirmation: "Миграции применяются только после резервной копии и отдельного подтверждения.",
      backup_restore_create_backup_first: "Сначала должна появиться валидная резервная копия.",
      backup_restore_check_available: "Проверка выполняется отдельно от рабочей базы.",
      download_support_report: "Скачайте диагностический отчёт для поддержки.",
      check_status: "Можно выполнить безопасную проверку состояния.",
    },
    maintenanceCheckActions: {
      db_adoption: "Проверить БД",
      migration: "Проверить миграции",
      restore: "Проверить копию",
    },
    maintenanceFactLabels: {
      metadata_present: "Метаданные",
      already_adopted: "Принята",
      current_version: "Текущая",
      target_version: "Целевая",
      pending_count: "Ожидают",
      valid_artifacts: "Копии",
      temporary_validation: "Временная проверка",
      current_product_restore: "Восстановление в рабочую БД",
    },
    maintenanceStatuses: {
      ok: "OK",
      current: "Актуально",
      available: "Доступно",
      adoptable: "Можно принять",
      adopted: "Принято",
      already_adopted: "Уже принято",
      blocked: "Заблокировано",
      cancelled: "Отменено",
      checking: "Проверка",
      check_failed: "Проверка не прошла",
      complete: "Завершено",
      completed: "Завершено",
      valid: "Проверено",
      verified: "Проверено",
      compose_config: "Проверка compose",
      commit_verification: "Проверка commit",
      drift_known_safe: "Без критичных проблем",
      draft_known_safe: "Известный безопасный черновик",
      downloading: "Загрузка",
      extracting: "Распаковка",
      failed: "Ошибка",
      health_check: "Тестирование",
      request: "Запрос",
      applying: "Обновление",
      apply: "Обновление",
      acquire_source: "Получение источника",
      overlay: "Накатка файлов",
      no_artifacts: "Нет артефактов",
      not_configured: "Не настроено",
      not_cancelable: "Отмена недоступна",
      preflight: "Предпроверка",
      queued: "В очереди",
      rebuilding: "Пересборка",
      preparing: "Подготовка",
      staging: "Подготовка версии",
      activating: "Активация",
      reconnecting: "Переподключение",
      rolling_back: "Возврат предыдущей версии",
      failed_rolled_back: "Предыдущая версия восстановлена",
      restarting: "Перезапуск",
      running: "Выполняется",
      pending: "Ожидает",
      stalled: "Зависло",
      starting_helper: "Запуск helper",
      update_available: "Есть обновление",
      identity_incomplete: "Неполная идентичность",
      installed_identity_drift: "Расхождение установки",
      metadata_stale: "Устаревшие метаданные",
      provider_unavailable: "Источник недоступен",
      no_release_published: "Релиз не опубликован",
      installed_newer_than_available: "Установленная версия новее",
      validating_source: "Проверка источника",
      limited: "Ограничено",
      unknown: "Неизвестно",
    },
    maintenanceMessageLabels: {
      schema_metadata_valid: "Метаданные схемы уже в порядке.",
      schema_current_no_pending_migrations: "Схема актуальна, ожидающих миграций нет.",
      schema_update_failed: "Подготовка схемы базы данных завершилась ошибкой.",
      schema_update_retry_after_cause_resolved: "Устраните причину ошибки подготовки базы данных и только затем повторите обновление.",
      slot_adoption_conflict: "Сохранённый предыдущий релиз больше не совпадает с текущей установкой.",
      slot_adoption_conflict_action: "Перед повторной попыткой проверьте установленный код и состояние сервисов.",
      restore_no_valid_artifacts: "В настроенной папке резервных копий нет подходящих артефактов восстановления.",
      update_apply_not_available_for_release: "Применение этого релиза из интерфейса недоступно.",
      maintenance_history_limited: "Долговременная история ограничена: показаны текущий статус и последний безопасный отчёт.",
      drift_known_safe: "Без критичных проблем.",
      draft_known_safe: "Известный безопасный черновик.",
      complete: "Завершено.",
      completed: "Завершено.",
    },
    updateWarningGeneric: "Есть предупреждение обновления. Подробности недоступны в безопасном виде.",
    updateWarningLabels: {
      source_metadata_invalid: "Метаданные установленного источника недоступны или повреждены.",
      source_metadata_missing: "Метаданные установленного источника не найдены.",
      source_metadata_unavailable: "Метаданные установленного источника недоступны.",
      source_metadata_unsupported_schema: "Схема метаданных установленного источника не поддерживается.",
      update_metadata_invalid: "Метаданные последнего обновления недоступны или повреждены.",
      update_metadata_missing: "Метаданные последнего обновления не найдены.",
      update_metadata_unavailable: "Метаданные последнего обновления недоступны.",
      update_metadata_unsupported_schema: "Схема метаданных последнего обновления не поддерживается.",
      installed_commit_invalid: "Установленный commit имеет некорректный формат.",
      trusted_commit_missing: "У опубликованного релиза нет подтверждённого commit.",
      identity_incomplete: "Метаданные установленного релиза неполные.",
      installed_identity_drift: "Метаданные релиза расходятся с текущим Git HEAD.",
      no_release_published: "Публичный release descriptor не найден.",
      installed_newer_than_available: "Установленная версия новее опубликованной.",
      trusted_manifest_not_configured: "Trusted release manifest не настроен на сервере.",
      check_failed: "Проверка обновления не завершилась штатно.",
      update_check_already_running: "Проверка обновления уже выполняется. Дождитесь завершения текущей проверки.",
      manual_update_check_rate_limited: "Проверка обновления уже выполнялась недавно. Повторите проверку сейчас или обновите статус.",
      commit_mismatch: "Установленный commit не совпадает с trusted release commit.",
      token_not_configured: "Серверный token для приватного источника не настроен.",
      requires_migration: "Release требует миграции, которая не запускается этим экраном.",
      requires_backup: "Release требует резервной копии перед применением.",
      requires_manual_action: "Release требует ручного действия оператора.",
      blocked: "Обновление заблокировано текущими условиями.",
      unsupported: "Это действие сейчас не поддерживается.",
      rollback_unsupported: "Rollback не поддерживается текущим update helper.",
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
    diagnosticArchiveQuestion: "Выберите диагностический архив",
    diagnosticArchiveNormal: "Обычный",
    diagnosticArchiveExtended: "Расширенный",
    diagnosticArchiveMessage: "Оба варианта содержат полный диагностический архив. Отличается только временной диапазон Docker-логов и аудита.",
    diagnosticArchiveNormalDescription: "Полная диагностика и текущее состояние; Docker-логи и аудит за последние 10 минут.",
    diagnosticArchiveExtendedDescription: "Та же полная диагностика; Docker-логи и аудит за последние 30 минут.",
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
    delete: "Удалить",
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
    maintenanceText: "Updates and maintenance: database, migrations, restore, and reports.",
    maintenanceOverview: "Maintenance overview",
    maintenanceOverallHealthy: "The system is operating normally",
    maintenanceOverallHealthyText: "Updates, database maintenance, backups, and diagnostics are available.",
    maintenanceOverallAttention: "Some items need attention",
    maintenanceOverallAttentionText: "Core functions are available, but one maintenance area needs review.",
    maintenanceOverallNoBackupText: "The system is operating, but there are no database backups yet.",
    maintenanceOverallBlocked: "Maintenance action is required",
    maintenanceOverallBlockedText: "An important operation is unavailable. Review its section or download a report.",
    maintenanceOverallUnknown: "Maintenance status is being checked",
    maintenanceOverallUnknownText: "Not all data is available. Refresh the overview or try again later.",
    maintenanceRefresh: "Refresh",
    maintenanceLoadError: "Maintenance overview is unavailable.",
    maintenanceLimitedHistory: "Durable history is limited: current status and the latest safe report are shown.",
    maintenanceReport: "Maintenance report",
    maintenanceReportDownload: "Download report",
    maintenanceReportReady: "Report is ready. Sensitive data is hidden.",
    maintenanceReportUnavailable: "Report is unavailable.",
    maintenanceBackupCreate: "Create DB backup",
    maintenanceBackupCreating: "Creating backup...",
    maintenanceBackupCreateConfirm: "Create a KM VMS database backup? Camera recordings and video archives are not included.",
    maintenanceBackupCreated: "DB backup was created.",
    maintenanceBackupCreateFailed: "Failed to create DB backup.",
    maintenanceBackupScope: "The backup includes the database and service metadata only. Camera recordings and video archives are not copied.",
    maintenanceBackupResult: "Latest backup",
    maintenanceBackupsTitle: "Backups",
    maintenanceBackupsText: "Create, check, and delete database and service metadata backups.",
    maintenanceDatabaseTitle: "Database",
    maintenanceBackupOpenList: "Open list",
    maintenanceBackupBackToOverview: "Back to overview",
    maintenanceLastUpdate: "Last update",
    maintenanceBackupCreateShort: "Create",
    maintenanceBackupCheck: "Check",
    maintenanceBackupCheckPassedTitle: "Check passed",
    maintenanceBackupCheckPassedText: "The backup is available, intact, and compatible; the trial restore passed.",
    maintenanceBackupDelete: "Delete",
    maintenanceBackupDeleting: "Deleting...",
    maintenanceBackupDeleted: "Backup was deleted.",
    maintenanceBackupDeleteFailed: "Failed to delete backup.",
    maintenanceBackupDeleteConfirm: "Delete the backup from {date}? Only product-owned files for this backup are removed. Video archives are not affected.",
    maintenanceBackupRestore: "Restore",
    maintenanceCurrentRestoreAction: "Restore current database",
    maintenanceCurrentRestoreTitle: "Restore current database",
    maintenanceCurrentRestoreIntro: "Backup from {date} is selected. The exact artifact will be checked again before start.",
    maintenanceCurrentRestoreChanges: "Users, cameras, settings, and service metadata will return to the state in this backup.",
    maintenanceCurrentRestoreVideoSafe: "The video archive and recording files are neither restored nor deleted.",
    maintenanceCurrentRestoreBackupFirst: "Before replacement, the system creates and verifies a safety backup of the current database. If the selected restore fails, the database is returned automatically from that backup; the video archive is neither included nor changed.",
    maintenanceCurrentRestoreInterruption: "The API and camera recording will briefly stop during the operation.",
    maintenanceCurrentRestoreActor: "The current administrator must exist and remain active in the selected backup.",
    maintenanceCurrentRestorePhraseLabel: "Type RESTORE KM VMS to confirm",
    maintenanceCurrentRestoreConfirm: "Restore",
    maintenanceCurrentRestorePreflight: "Checking the backup and restore conditions...",
    maintenanceCurrentRestoreBlocked: "This backup is not ready for current-database restore.",
    maintenanceCurrentRestoreReconnect: "Services are restarting. Status will continue to refresh automatically.",
    maintenanceCurrentRestoreCompleted: "The current database was restored. Sign in again if requested.",
    maintenanceCurrentRestoreRolledBack: "Restore did not complete; the original current database was automatically restored from the safety backup.",
    maintenanceCurrentRestoreRecoveryRequired: "Automatic database recovery was not proven. Do not retry and contact support.",
    maintenanceCurrentRestoreClose: "Close",
    maintenanceCurrentRestoreCancel: "Cancel",
    maintenanceCurrentRestoreTerminalPhase: "Result",
    maintenanceCurrentRestorePhraseMismatch: "Type the confirmation phrase exactly as shown.",
    maintenanceCurrentRestoreRequestRejected: "The restore request was rejected.",
    maintenanceCurrentRestorePhases: {
      preflight: "Checking conditions",
      pre_restore_backup: "Preparing current database safety backup",
      writers_paused: "Database writers paused",
      restore_running: "Restoring database",
      services_starting: "Starting services",
      post_restore_check: "Final verification",
      completed: "Completed",
      blocked: "Operation blocked",
      failed_rolled_back: "Original database restored",
      failed_recovery_required: "Support recovery required",
    },
    maintenanceCurrentRestoreReasons: {
      artifact_invalid: "The backup identity is invalid.",
      artifact_unavailable: "Backup files are unavailable.",
      artifact_integrity_not_verified: "Verify backup integrity first.",
      migration_required: "This backup schema requires migration and cannot be restored here.",
      newer_than_supported: "The backup schema is newer than this application.",
      restore_not_supported: "Current database restore is unavailable.",
      initiating_actor_missing_or_inactive_in_backup: "The current administrator is missing or inactive in this backup.",
      insufficient_space_for_pre_restore_backup: "There is not enough space for the current database safety backup.",
      backup_root_not_persistent: "The backup directory is not verified as persistent.",
      update_operation_active: "Wait for the update to finish first.",
      backup_operation_active: "Wait for the backup operation to finish first.",
      schema_operation_active: "Wait for the schema operation to finish first.",
      restore_helper_unavailable: "The restore helper is not available yet.",
      restore_status_reader_unavailable: "The safe restore status service is not available yet.",
      postgresql_required: "Current database restore is supported only for PostgreSQL.",
      schema_migration_required: "The current database schema requires migration.",
      schema_newer_than_supported: "The current database schema is newer than this application.",
      artifact_backend_unsupported: "The backup uses an unsupported database type.",
      artifact_schema_migration_required: "The backup schema requires migration.",
      artifact_schema_newer: "The backup schema is newer than this application.",
      artifact_integrity_evidence_stale: "The backup changed after its integrity check.",
      temporary_restore_validation_required: "Run a trial restore for this backup first.",
      temporary_restore_validation_failed: "The trial restore for this backup failed.",
      restore_operation_active: "Another restore is already running.",
      submission_binding_conflict: "This launch identifier is already bound to another operation.",
      artifact_evidence_changed: "The backup changed after preflight.",
      confirmation_required: "Explicit restore confirmation is required.",
      confirmation_phrase_invalid: "The confirmation phrase is incorrect.",
      audit_unavailable: "The audit event could not be recorded reliably. Restore was not started.",
      current_actor_access_changed: "The current administrator access changed. Run preflight again.",
      artifact_fingerprint_changed: "The backup changed after the operation was accepted.",
      current_schema_not_exact: "The current database schema no longer matches the supported version.",
      pre_restore_backup_verification_failed: "The current database safety backup could not be created and verified.",
      restore_writer_isolation_failed: "Services writing database data could not be stopped safely.",
      pg_restore_failed: "Database restore failed.",
      restore_api_health_failed: "The API health check failed after restore.",
      restore_recorder_start_failed: "Recording could not be started after the database check.",
      automatic_rollback_failed: "Automatic recovery of the original database failed. Contact support.",
      automatic_rollback_isolation_failed: "Recording services could not be stopped safely for automatic database recovery. The database was not returned; contact support.",
      automatic_rollback_database_failed: "Automatic database recovery did not complete or could not be verified. Do not retry and contact support.",
      automatic_rollback_api_recovery_failed: "The safety database backup was restored, but the API did not start. Full application readiness is not confirmed.",
      automatic_rollback_validation_failed: "The safety database backup was restored, but final validation did not complete. Full application readiness is not confirmed.",
      automatic_rollback_recorder_recovery_failed: "The database was restored and the API was verified, but camera recording did not recover. Full application readiness is not confirmed.",
      restore_interrupted_before_mutation: "The operation stopped before the working database was changed.",
      restore_interrupted_after_mutation: "The operation stopped after changes began; automatic recovery was performed.",
      restore_helper_exception: "The restore service ended the operation with an internal error.",
      post_restore_actor_access_invalid: "Administrator access was not confirmed in the restored database.",
      post_restore_schema_invalid: "The restored database has an unsupported schema.",
      post_restore_metadata_invalid: "The restored database failed its core data check.",
      post_restore_tables_missing: "Required tables are missing from the restored database.",
    },
    maintenanceBackupStatusEmpty: "No backups yet.",
    maintenanceBackupStatusReady: "Backups: {count}",
    maintenanceBackupCopyOne: "backup",
    maintenanceBackupCopyMany: "backups",
    maintenanceBackupNoCopies: "No backups",
    maintenanceBackupLatest: "Latest backup",
    maintenanceBackupProblems: "Problematic",
    maintenanceBackupSize: "Size",
    maintenanceBackupSchema: "Schema",
    maintenanceBackupList: "Recent backups",
    maintenanceBackupNothingToCheck: "Nothing to check yet",
    maintenanceBackupCheckConfirm: "Check the backup from {date} by restoring it into a temporary database? The working database and video archive will not change.",
    maintenanceBackupChecking: "Checking the backup in a temporary database…",
    maintenanceBackupRecovering: "Checking the state of a previously started check…",
    maintenanceBackupTotalSize: "Total size",
    maintenanceBackupPage: "{start}–{end} of {total}",
    maintenanceBackupPrevious: "Previous",
    maintenanceBackupNext: "Next",
    maintenanceBackupIntegrity: "Integrity",
    maintenanceBackupCompatibility: "Compatibility",
    maintenanceBackupValidation: "Trial restore",
    maintenanceBackupAvailability: "Availability",
    maintenanceBackupCheckedAt: "Checked: {date}",
    maintenanceBackupValidatedAt: "Restore checked: {date}",
    maintenanceBackupAvailabilityStatuses: {
      available: "Available",
      incomplete: "Incomplete",
      missing: "File missing",
      unsafe: "Unsafe structure",
      unknown: "Unknown",
    },
    maintenanceBackupIntegrityStatuses: {
      not_checked: "Not checked",
      verified: "Intact",
      failed: "Damaged",
      stale_evidence: "Recheck required",
      unknown: "Unknown",
    },
    maintenanceBackupCompatibilityStatuses: {
      compatible: "Compatible",
      migration_required: "Migration required",
      newer_than_supported: "Created by a newer version",
      unsupported_backend: "Unsupported database type",
      unknown: "Unknown",
    },
    maintenanceBackupValidationStatuses: {
      not_performed: "Not performed",
      passed: "Passed",
      failed: "Failed",
      stale_evidence: "Recheck required",
      unknown: "Unknown",
    },
    maintenanceBackupOperationLabels: {
      check: "Check",
      create: "Create",
      delete: "Delete",
    },
    maintenanceBackupStatuses: {
      valid: "Ready",
      verified: "Ready",
      available: "Available",
      no_artifacts: "No backups",
      blocked: "Check blocked",
      invalid: "Problem",
      check_failed: "Check failed",
      validated: "Trial restore passed",
      passed: "Trial restore passed",
      interrupted: "Check was interrupted and can be retried",
      problem: "Problem",
    },
    maintenanceBackupCheckStatuses: {
      valid: "Check passed",
      verified: "Check passed",
      validated: "The backup is available, intact, and compatible; the trial restore passed.",
      passed: "The backup is available, intact, and compatible; the trial restore passed.",
      completed: "Check completed",
      available: "Backup is available to check",
      no_artifacts: "Nothing to check yet",
      blocked: "Check is blocked",
      invalid: "Backup is damaged or incomplete",
      check_failed: "Check failed",
      fallback: "Check status received",
    },
    maintenanceBackupCheckOutcomes: {
      fully_validated: "The backup is available, intact, and compatible; the trial restore passed.",
      integrity_verified_migration_required: "Integrity is verified. Trial restore was not run because this backup requires a compatible migration.",
      integrity_failed: "Backup integrity could not be verified: the backup is damaged or incomplete.",
      restore_failed: "Backup integrity is verified, but the trial restore failed.",
    },
    maintenanceBackupCreateStatuses: {
      created: "Backup was created",
      verified: "Backup was created",
      valid: "Backup was created",
      completed: "Backup was created",
      blocked: "Create is blocked",
      failed: "Failed to create backup",
      fallback: "Create status received",
    },
    maintenanceBackupDeleteStatuses: {
      deleted: "Backup was deleted",
      deleted_with_missing_files: "Backup was deleted; some files were already missing",
      blocked: "Delete is blocked",
      failed: "Failed to delete backup",
      partial_retryable: "Deletion is partial and can be retried",
      delete_failed: "Failed to delete backup",
      not_found: "Backup is no longer found",
      fallback: "Delete status received",
    },
    maintenanceDbAdoptionApply: "Adopt database",
    maintenanceDbAdoptionApplyConfirm: "Adopt the existing database as the KM VMS working database? The action runs only after the built-in checks and backup gate pass.",
    maintenanceDbAdoptionApplied: "The existing database was adopted.",
    maintenanceDbAdoptionApplyFailed: "Failed to adopt the existing database.",
    maintenanceDryRun: "Check",
    maintenanceDryRunResult: "Check completed.",
    maintenanceNoApply: "Updates apply only through the confirmed helper process.",
    maintenanceBackupRequired: "Backup required",
    maintenanceBackupNotRequired: "Backup not required",
    maintenanceConfirmationRequired: "Confirmation required",
    maintenanceUnsupported: "Action blocked or unsupported",
    yes: "Yes",
    no: "No",
    maintenanceLastAction: "Last action",
    maintenanceNoHistory: "No action history found",
    maintenanceGeneratedAt: "Generated",
    maintenanceWarnings: "Diagnostics",
    maintenanceWarningDetails: "Show details",
    maintenanceWarningHide: "Hide details",
    maintenanceSupportStatusOk: "No critical issues",
    maintenanceWarningActionable: "Need attention",
    maintenanceWarningInfo: "Informational limitations",
    maintenanceWarningSupport: "For support",
    maintenanceWarningNone: "No active warnings.",
    maintenanceSupportReportAction: "Download the report and send it to support.",
    maintenanceWarningGeneric: {
      actionable: { title: "Check required", summary: "A condition may affect maintenance.", action: "Download the report and fix the cause using its details." },
      informational: { title: "Diagnostic limitation", summary: "This is not an outage: the report records that a check did not run automatically.", action: "No action is required if the system works normally." },
      support: { title: "Support detail", summary: "This item helps diagnostics but does not always require user action.", action: "Download the report if support is needed." },
    },
    maintenanceWarningLabels: {
      backup_status_source_unavailable: { title: "No connected backup status source", summary: "The report did not find a separate safe source proving backup status in read-only diagnostics.", action: "Use the backup block to create or check a backup." },
      restore_validation_status_source_unavailable: { title: "No restore-check source", summary: "Diagnostics did not receive separate evidence that a backup was checked in a temporary database.", action: "Run backup check in the backup block." },
      backup_root_persistence_unknown: { title: "Backup folder was not write-probed", summary: "The report did not write-probe the backup folder because it must not mutate the system without command.", action: "This is an informational report limitation." },
      video_archive_restore_not_covered: { title: "Video archive is not in DB backup", summary: "The backup protects database and service metadata, but not camera recordings.", action: "Video archive storage is checked separately." },
      production_adoption_deferred: { title: "Service DB adoption is deferred", summary: "Diagnostics are read-only and do not change service metadata without a separate action.", action: "No action is required if the system works normally." },
      installed_build_development_fallback: { title: "Build metadata is incomplete", summary: "The installed environment did not provide the full build metadata source.", action: "Check release identity during the next update." },
    },
    maintenanceMessageFallback: "Status received; details are unavailable.",
    maintenanceActionFallback: "The action is currently unavailable. Check system status and try again later.",
    updateApplyTitle: "KM VMS update",
    updateApplyCheck: "Check update",
    updateApplyStart: "Update KM VMS",
    updateApplyConfirm: "Install the available KM VMS update?",
    updateApplyConfirmRestart: "Services may restart temporarily; status polling will resume after the API is available.",
    updateApplyModalTitle: "KM VMS update",
    updateApplyModalConfirm: "Update KM VMS",
    updateApplyLaunchChecking: "Checking whether the update started",
    updateApplyLaunchCheckingText: "The server response is temporarily unavailable. KM VMS is checking status and will not send a second request.",
    updateApplyLaunchUnknown: "The launch result is not confirmed yet. Checking continues automatically and another apply is locked.",
    updateApplyLaunchConflict: "Another update operation was detected. Another apply is locked until its final status is known.",
    updateApplyLaunchNotAccepted: "The server proved that the request was not accepted. Open a new confirmation before trying again.",
    updateApplyLaunchRejected: "The server rejected the update launch.",
    updateApplyLocked: "Checking launch",
    updateApplyPeerCheckUnavailable: "Published release checking is temporarily unavailable; the update apply status was received.",
    updateApplyTransportErrors: {
      network_unavailable: "The update service connection is unavailable. Checking will continue automatically.",
      temporarily_unavailable: "The update service is restarting or temporarily unavailable. Checking will continue automatically.",
      unauthorized: "Sign in again to check the update.",
      permission_denied: "You do not have permission to manage updates.",
      request_failed: "Update status could not be received. Checking will continue automatically.",
      typed_backend_error: "The update service reported an error.",
    },
    updateApplyQueued: "The KM VMS update request was handed to the helper.",
    updateApplyUnavailable: "Update cannot start now. Review the message in this panel and run Check update again if needed.",
    updateApplyConnection: "Services may restart temporarily; status polling will continue automatically.",
    updateApplyRecoveryAvailable: "Check the target version and commit, then start apply.",
    updateApplyRecoveryBlocked: "Fix the trusted release or server-side configuration blocker and run check again.",
    updateApplyRecoveryCommitMismatch: "Installed commit does not match the trusted release commit. Treat the update as failed and retry after checking the server-side source.",
    updateApplyRecoveryCompleted: "Update completed and the installed commit is verified.",
    updateApplyRecoveryCurrent: "Installed version matches the trusted release.",
    updateApplyRecoveryFailed: "Update failed. Review the update status and retry after fixing the cause.",
    updateApplyRecoveryRolledBack: "The target release failed verification. KM VMS restored the previous working release; resolve the cause before retrying.",
    updateApplyRecoveryCheckFailed: "Update check did not complete. Run the check again or download the report for support.",
    updateApplyRecoveryLiveCheckFailedWithSnapshot: "The latest live check failed, but a fresh trusted check is still available for safe apply.",
    updateApplyRecoveryRefreshRequired: "The release changed or the check is too old. Run Check update again.",
    updateApplyRecoveryMissingCommit: "The release is missing trusted commit evidence. Update cannot be applied safely.",
    updateApplyRecoveryReconnecting: "Services may be restarting. The UI will continue polling and reread status when the API returns.",
    updateApplyRecoveryRunning: "The helper is applying the update. Keep the NAS powered and wait for the final status.",
    updateApplyRecoveryStalled: "Update status has not changed recently. Do not retry while the helper or lock may still be active; check server status first.",
    updateApplyRecoveryUnknown: "Update status is not known yet. Refresh the check or wait for the API response.",
    updateApplyRecoveryIdentity: "Installed release metadata is incomplete or does not match the current code. Apply is blocked until release identity is adopted.",
    updateApplyRecoveryProvider: "Release source is temporarily unavailable. Try again; a fresh successful trusted check can be used only for a limited time.",
    updateApplyRecoveryInstalledNewer: "Installed version is newer than the published release. Apply is blocked to avoid downgrading the system.",
    updateApplyTechnicalDetails: "Technical details",
    updateApplyProgress: "Update progress",
    updateApplyButtonRunning: "Updating",
    updateApplyButtonRebuilding: "Rebuilding",
    updateApplyButtonHealth: "Testing",
    updateApplyButtonVerification: "Commit check",
    updateApplyHeadlines: {
      current: "System is current",
      available: "Update available",
      running: "Update is running",
      completed: "Completed successfully",
      blocked: "Attention required",
      unknown: "Update status is temporarily unknown",
    },
    updateApplySummaries: {
      current: "Installed version matches the published release.",
      available: "Review the release and apply it when ready.",
      running: "The helper is applying the update and refreshing progress automatically.",
      completed: "Update is installed and verified.",
      blocked: "Apply is currently blocked; diagnostics contain safe details.",
      unknown: "The last received information is stale. Status checking continues automatically.",
    },
    updateApplyResults: {
      completedVerified: "Completed successfully",
      current: "Current",
      available: "Update available",
      running: "Running",
      blocked: "Attention required",
      unknown: "Status temporarily unknown",
    },
    updateApplyReleaseChanges: "What changed in this release",
    updateApplyReleaseTitleFallback: "KM VMS release",
    updateApplyReleaseSummaryFallback: "Release notes are unavailable.",
    updateApplyLastState: "Latest progress",
    updateApplyStepDone: "Done",
    updateApplyTimelineCurrent: "Current version",
    updateApplyHistoryLimited: "Detailed step history was not recorded for the previous apply.",
    updateApplyDiagnostics: "Diagnostics",
    updateApplySupportTitle: "Need support?",
    updateApplySupportText: "Download the diagnostic report and send it to support. Technical details are included in the report.",
    updateApplySupportAction: "Download diagnostic report",
    updateCommitPending: "Pending verification",
    updateCommitUnavailable: "No data",
    updateCommitVerified: "Commit verified",
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
      installedCommit: "Installed commit",
      reportId: "Report ID",
      source: "Source",
      targetCommit: "Target commit",
      verification: "Commit check",
      currentStep: "Current step",
      lastProgress: "Last progress",
      elapsed: "Elapsed",
      completedAt: "Completed",
      releaseIdentity: "Release verified",
      releaseTitle: "Release",
      releaseSummary: "Changes",
      status: "Update",
      gitHead: "Git HEAD",
      metadataSource: "Metadata",
      provider: "Provider",
    },
    maintenanceFlows: {
      db_adoption: "DB adoption",
      migration: "Migrations",
      restore: "Backup restore check",
      update: "Update",
    },
    maintenanceReadinessTitle: "Database and recovery readiness",
    maintenanceReadinessText: "Service operations used only during updates, migration, transfer, or recovery.",
    maintenanceSupportTitle: "Diagnostics for support",
    maintenanceSupportText: "If maintenance looks unclear or blocked, download the safe report and send it to support.",
    maintenanceReadinessTitles: {
      db_identity: "Database",
      db_schema: "DB structure",
      backup_restore_check: "Backup restore check",
    },
    maintenanceOperatorStatuses: {
      ok: "Healthy",
      attention: "Check",
      blocked: "Needs attention",
      unavailable: "No data",
      action_available: "Action available",
    },
    maintenanceOperatorSummaries: {
      db_identity_ok: "The app recognizes this database as its own. No action is needed.",
      db_identity_adoptable: "The database can be adopted safely after a backup and explicit confirmation.",
      db_identity_blocked: "The database is not ready for adoption. Diagnostics are required before any action.",
      db_schema_current: "The database structure is current for this app version.",
      db_schema_pending: "This app version has pending database structure changes.",
      db_schema_blocked: "Database structure check is blocked. Diagnostics are required.",
      backup_restore_no_artifacts: "No verified backup copies are available in the configured backup folder.",
      backup_restore_artifacts_available: "Backup copies are available and can be checked in a temporary database.",
      backup_restore_validation_available: "A safe restore check can run in a temporary database.",
      backup_restore_blocked: "Backup restore check is blocked. Diagnostics are required.",
    },
    maintenanceOperatorActions: {
      db_identity_check_optional: "This item usually needs no action.",
      db_identity_apply_requires_confirmation: "Adoption changes only service metadata and requires separate confirmation.",
      migration_check_optional: "This item usually needs no action.",
      migration_apply_requires_confirmation: "Migrations apply only after a backup and separate confirmation.",
      backup_restore_create_backup_first: "A valid backup copy must exist first.",
      backup_restore_check_available: "The check runs separately from the working database.",
      download_support_report: "Download the diagnostic report for support.",
      check_status: "A safe status check can be run.",
    },
    maintenanceCheckActions: {
      db_adoption: "Check DB",
      migration: "Check migrations",
      restore: "Check backup",
    },
    maintenanceFactLabels: {
      metadata_present: "Metadata",
      already_adopted: "Adopted",
      current_version: "Current",
      target_version: "Target",
      pending_count: "Pending",
      valid_artifacts: "Copies",
      temporary_validation: "Temporary check",
      current_product_restore: "Restore to working DB",
    },
    maintenanceStatuses: {
      ok: "OK",
      current: "Current",
      available: "Available",
      adoptable: "Adoptable",
      adopted: "Adopted",
      already_adopted: "Already adopted",
      blocked: "Blocked",
      cancelled: "Cancelled",
      checking: "Checking",
      check_failed: "Check failed",
      complete: "Complete",
      completed: "Completed",
      valid: "Verified",
      verified: "Verified",
      compose_config: "Compose check",
      commit_verification: "Commit check",
      drift_known_safe: "No critical issues",
      draft_known_safe: "Known-safe draft",
      downloading: "Downloading",
      extracting: "Extracting",
      failed: "Failed",
      health_check: "Testing",
      request: "Request",
      applying: "Updating",
      apply: "Update",
      acquire_source: "Acquire source",
      overlay: "Overlay files",
      no_artifacts: "No artifacts",
      not_configured: "Not configured",
      not_cancelable: "Not cancelable",
      preflight: "Preflight",
      queued: "Queued",
      rebuilding: "Rebuilding",
      preparing: "Preparing",
      staging: "Staging release",
      activating: "Activating",
      reconnecting: "Reconnecting",
      rolling_back: "Restoring previous release",
      failed_rolled_back: "Previous release restored",
      restarting: "Restarting",
      running: "Running",
      pending: "Pending",
      stalled: "Stalled",
      starting_helper: "Starting helper",
      update_available: "Update available",
      identity_incomplete: "Identity incomplete",
      installed_identity_drift: "Install drift",
      metadata_stale: "Stale metadata",
      provider_unavailable: "Source unavailable",
      no_release_published: "No release published",
      installed_newer_than_available: "Installed is newer",
      validating_source: "Validating source",
      limited: "Limited",
      unknown: "Unknown",
    },
    maintenanceMessageLabels: {
      schema_metadata_valid: "Schema metadata is already valid.",
      schema_current_no_pending_migrations: "Schema is current; no pending migrations.",
      schema_update_failed: "Database schema preparation failed.",
      schema_update_retry_after_cause_resolved: "Resolve the database schema preparation failure before retrying the update.",
      slot_adoption_conflict: "The preserved previous release no longer matches the current installation.",
      slot_adoption_conflict_action: "Verify the installed source and runtime state before retrying.",
      restore_no_valid_artifacts: "No valid restore artifacts are available in the configured backup root.",
      update_apply_not_available_for_release: "In-app apply is not available for this release.",
      maintenance_history_limited: "Durable history is limited: current status and the latest safe report are shown.",
      drift_known_safe: "No critical issues.",
      draft_known_safe: "Known-safe draft.",
      complete: "Complete.",
      completed: "Completed.",
    },
    updateWarningGeneric: "An update warning is present. Safe details are unavailable.",
    updateWarningLabels: {
      source_metadata_invalid: "Installed source metadata is unavailable or invalid.",
      source_metadata_missing: "Installed source metadata is missing.",
      source_metadata_unavailable: "Installed source metadata is unavailable.",
      source_metadata_unsupported_schema: "Installed source metadata schema is unsupported.",
      update_metadata_invalid: "Last update metadata is unavailable or invalid.",
      update_metadata_missing: "Last update metadata is missing.",
      update_metadata_unavailable: "Last update metadata is unavailable.",
      update_metadata_unsupported_schema: "Last update metadata schema is unsupported.",
      installed_commit_invalid: "Installed commit value is not valid.",
      trusted_commit_missing: "Published release has no verified commit.",
      identity_incomplete: "Installed release metadata is incomplete.",
      installed_identity_drift: "Release metadata does not match the current Git HEAD.",
      no_release_published: "Public release descriptor was not found.",
      installed_newer_than_available: "Installed version is newer than the published release.",
      trusted_manifest_not_configured: "Trusted release manifest is not configured on the server.",
      check_failed: "Update check did not complete successfully.",
      update_check_already_running: "An update check is already in progress. Wait for the current check to finish.",
      manual_update_check_rate_limited: "The update check was recently requested. Run the check again or refresh status.",
      commit_mismatch: "Installed commit does not match the trusted release commit.",
      token_not_configured: "Server-side token for the private source is not configured.",
      requires_migration: "Release requires migration support that this screen does not run.",
      requires_backup: "Release requires a backup before apply.",
      requires_manual_action: "Release requires manual operator action.",
      blocked: "Update is blocked by current conditions.",
      unsupported: "This action is not supported right now.",
      rollback_unsupported: "Rollback is not supported by the current update helper.",
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
    diagnosticArchiveQuestion: "Choose a diagnostic archive",
    diagnosticArchiveNormal: "Normal",
    diagnosticArchiveExtended: "Extended",
    diagnosticArchiveMessage: "Both options contain the full diagnostic archive. Only the Docker log and audit time range differs.",
    diagnosticArchiveNormalDescription: "Full diagnostics and current state; Docker logs and audit for the last 10 minutes.",
    diagnosticArchiveExtendedDescription: "The same full diagnostics; Docker logs and audit for the last 30 minutes.",
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
    delete: "Delete",
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
  edit: "编辑",
  deactivate: "停用",
  activate: "启用",
  delete: "删除",
  maintenance: "维护",
  maintenanceText: "更新与维护：数据库、迁移、恢复和报告。",
  maintenanceOverview: "维护概览",
  maintenanceOverallHealthy: "系统运行正常",
  maintenanceOverallHealthyText: "更新、数据库维护、备份和诊断均可用。",
  maintenanceOverallAttention: "有项目需要注意",
  maintenanceOverallAttentionText: "核心功能可用，但有一个维护区域需要检查。",
  maintenanceOverallNoBackupText: "系统正在运行，但目前还没有数据库备份。",
  maintenanceOverallBlocked: "维护需要处理",
  maintenanceOverallBlockedText: "一项重要操作不可用。请检查相应区域或下载报告。",
  maintenanceOverallUnknown: "正在确认维护状态",
  maintenanceOverallUnknownText: "尚未获取全部数据。请刷新概览或稍后重试。",
  maintenanceRefresh: "刷新",
  maintenanceLoadError: "维护概览不可用。",
  maintenanceLimitedHistory: "持久历史记录有限：仅显示当前状态和最新安全报告。",
  maintenanceReport: "维护报告",
  maintenanceReportDownload: "下载报告",
  maintenanceReportReady: "报告已准备好，敏感数据已隐藏。",
  maintenanceReportUnavailable: "报告不可用。",
  maintenanceBackupCreate: "创建数据库备份",
  maintenanceBackupCreating: "正在创建备份...",
  maintenanceBackupCreateConfirm: "创建 KM VMS 数据库备份？摄像机录像和视频归档不会包含在此备份中。",
  maintenanceBackupCreated: "数据库备份已创建。",
  maintenanceBackupCreateFailed: "无法创建数据库备份。",
  maintenanceBackupScope: "备份仅包含数据库和服务元数据，不会复制摄像机录像和视频归档。",
  maintenanceBackupResult: "最新备份",
  maintenanceBackupsTitle: "备份",
  maintenanceBackupsText: "创建、检查和删除数据库及服务元数据备份。",
  maintenanceDatabaseTitle: "数据库",
  maintenanceBackupOpenList: "打开列表",
  maintenanceBackupBackToOverview: "返回概览",
  maintenanceLastUpdate: "最近更新",
  maintenanceBackupCreateShort: "创建",
  maintenanceBackupCheck: "检查",
  maintenanceBackupCheckPassedTitle: "检查已通过",
  maintenanceBackupCheckPassedText: "备份可用、完整且兼容；试恢复已通过。",
  maintenanceBackupDelete: "删除",
  maintenanceBackupDeleting: "正在删除...",
  maintenanceBackupDeleted: "备份已删除。",
  maintenanceBackupDeleteFailed: "无法删除备份。",
  maintenanceBackupDeleteConfirm: "删除 {date} 的备份？只会删除此备份的产品文件，不会影响视频归档。",
  maintenanceBackupRestore: "恢复",
  maintenanceCurrentRestoreAction: "恢复当前数据库",
  maintenanceCurrentRestoreTitle: "恢复当前数据库",
  maintenanceCurrentRestoreIntro: "已选择 {date} 的备份。开始前会再次验证该准确备份。",
  maintenanceCurrentRestoreChanges: "用户、摄像机、设置和服务元数据将恢复到此备份的状态。",
  maintenanceCurrentRestoreVideoSafe: "视频归档和录像文件不会被恢复或删除。",
  maintenanceCurrentRestoreBackupFirst: "替换前，系统会创建并验证当前数据库的安全备份。如果所选恢复失败，数据库将自动从该备份返回；视频归档不会包含在内，也不会被更改。",
  maintenanceCurrentRestoreInterruption: "操作期间 API 和摄像机录像会短暂停止。",
  maintenanceCurrentRestoreActor: "当前管理员必须存在于所选备份中并保持启用。",
  maintenanceCurrentRestorePhraseLabel: "输入 RESTORE KM VMS 以确认",
  maintenanceCurrentRestoreConfirm: "恢复",
  maintenanceCurrentRestorePreflight: "正在检查备份和恢复条件...",
  maintenanceCurrentRestoreBlocked: "此备份目前不具备恢复条件。",
  maintenanceCurrentRestoreReconnect: "服务正在重启，状态将自动继续刷新。",
  maintenanceCurrentRestoreCompleted: "当前数据库已恢复。如有提示，请重新登录。",
  maintenanceCurrentRestoreRolledBack: "恢复未完成；原当前数据库已从安全备份自动还原。",
  maintenanceCurrentRestoreRecoveryRequired: "无法确认自动数据库恢复。请勿重试并联系支持人员。",
  maintenanceCurrentRestoreClose: "关闭",
  maintenanceCurrentRestoreCancel: "取消",
  maintenanceCurrentRestoreTerminalPhase: "结果",
  maintenanceCurrentRestorePhraseMismatch: "请完全按提示输入确认短语。",
  maintenanceCurrentRestoreRequestRejected: "恢复请求已被拒绝。",
  maintenanceCurrentRestorePhases: {
    preflight: "检查条件",
    pre_restore_backup: "准备当前数据库安全备份",
    writers_paused: "数据库写入服务已暂停",
    restore_running: "正在恢复数据库",
    services_starting: "正在启动服务",
    post_restore_check: "最终检查",
    completed: "已完成",
    blocked: "操作已阻止",
    failed_rolled_back: "原数据库已恢复",
    failed_recovery_required: "需要支持恢复",
  },
  maintenanceCurrentRestoreReasons: {
    artifact_invalid: "备份标识无效。",
    artifact_unavailable: "备份文件不可用。",
    artifact_integrity_not_verified: "请先验证备份完整性。",
    migration_required: "此备份架构需要迁移，无法在此恢复。",
    newer_than_supported: "备份架构比当前应用更新。",
    restore_not_supported: "当前数据库恢复不可用。",
    initiating_actor_missing_or_inactive_in_backup: "当前管理员在此备份中缺失或未启用。",
    insufficient_space_for_pre_restore_backup: "空间不足，无法创建当前数据库安全备份。",
    backup_root_not_persistent: "备份目录未确认是持久目录。",
    update_operation_active: "请先等待更新完成。",
    backup_operation_active: "请先等待备份操作完成。",
    schema_operation_active: "请先等待架构操作完成。",
    restore_helper_unavailable: "恢复 helper 暂不可用。",
    restore_status_reader_unavailable: "安全恢复状态服务暂不可用。",
    postgresql_required: "当前数据库恢复仅支持 PostgreSQL。",
    schema_migration_required: "当前数据库架构需要迁移。",
    schema_newer_than_supported: "当前数据库架构比此应用版本更新。",
    artifact_backend_unsupported: "此备份使用不支持的数据库类型。",
    artifact_schema_migration_required: "备份架构需要迁移。",
    artifact_schema_newer: "备份架构比此应用版本更新。",
    artifact_integrity_evidence_stale: "备份在完整性检查后发生了变化。",
    temporary_restore_validation_required: "请先对此备份执行试恢复。",
    temporary_restore_validation_failed: "此备份的试恢复失败。",
    restore_operation_active: "另一个恢复操作正在运行。",
    submission_binding_conflict: "此启动标识已绑定到另一个操作。",
    artifact_evidence_changed: "备份在预检查后发生了变化。",
    confirmation_required: "需要明确确认恢复。",
    confirmation_phrase_invalid: "确认短语不正确。",
    audit_unavailable: "无法可靠记录审计事件，恢复未启动。",
    current_actor_access_changed: "当前管理员权限已更改，请重新执行预检查。",
    artifact_fingerprint_changed: "操作被接受后，备份发生了变化。",
    current_schema_not_exact: "当前数据库架构不再匹配受支持版本。",
    pre_restore_backup_verification_failed: "无法创建并验证当前数据库的安全备份。",
    restore_writer_isolation_failed: "无法安全停止写入数据库的服务。",
    pg_restore_failed: "数据库恢复失败。",
    restore_api_health_failed: "恢复后 API 健康检查失败。",
    restore_recorder_start_failed: "数据库检查后无法启动录像服务。",
    automatic_rollback_failed: "自动恢复原数据库失败，请联系支持人员。",
    automatic_rollback_isolation_failed: "无法安全停止录像服务以自动返回数据库。数据库尚未返回，请联系支持人员。",
    automatic_rollback_database_failed: "自动返回数据库未完成或无法确认。请勿重试，并联系支持人员。",
    automatic_rollback_api_recovery_failed: "安全数据库备份已恢复，但 API 未启动，无法确认应用已完全就绪。",
    automatic_rollback_validation_failed: "安全数据库备份已恢复，但最终检查未完成，无法确认应用已完全就绪。",
    automatic_rollback_recorder_recovery_failed: "数据库已恢复且 API 已验证，但摄像机录像服务未恢复，无法确认应用已完全就绪。",
    restore_interrupted_before_mutation: "工作数据库更改前，操作已中断。",
    restore_interrupted_after_mutation: "更改开始后操作中断；已执行自动恢复。",
    restore_helper_exception: "恢复服务因内部错误结束了操作。",
    post_restore_actor_access_invalid: "未能在恢复后的数据库中确认管理员权限。",
    post_restore_schema_invalid: "恢复后的数据库架构不受支持。",
    post_restore_metadata_invalid: "恢复后的数据库未通过核心数据检查。",
    post_restore_tables_missing: "恢复后的数据库缺少必需表。",
  },
  maintenanceBackupStatusEmpty: "还没有备份。",
  maintenanceBackupStatusReady: "备份数量：{count}",
  maintenanceBackupCopyOne: "个备份",
  maintenanceBackupCopyMany: "个备份",
  maintenanceBackupNoCopies: "无备份",
  maintenanceBackupLatest: "最新备份",
  maintenanceBackupProblems: "问题项",
  maintenanceBackupSize: "大小",
  maintenanceBackupSchema: "结构",
  maintenanceBackupList: "最近备份",
  maintenanceBackupNothingToCheck: "暂无可检查的备份",
  maintenanceBackupCheckConfirm: "通过恢复到临时数据库来检查 {date} 的备份？工作数据库和视频归档不会更改。",
  maintenanceBackupChecking: "正在临时数据库中检查备份…",
  maintenanceBackupRecovering: "正在确认之前启动的检查状态…",
  maintenanceBackupTotalSize: "总大小",
  maintenanceBackupPage: "第 {start}–{end} 项，共 {total} 项",
  maintenanceBackupPrevious: "上一页",
  maintenanceBackupNext: "下一页",
  maintenanceBackupIntegrity: "完整性",
  maintenanceBackupCompatibility: "兼容性",
  maintenanceBackupValidation: "试恢复",
  maintenanceBackupAvailability: "可用性",
  maintenanceBackupCheckedAt: "检查时间：{date}",
  maintenanceBackupValidatedAt: "恢复检查时间：{date}",
  maintenanceBackupAvailabilityStatuses: {
    available: "可用",
    incomplete: "不完整",
    missing: "文件缺失",
    unsafe: "结构不安全",
    unknown: "未知",
  },
  maintenanceBackupIntegrityStatuses: {
    not_checked: "未检查",
    verified: "完整",
    failed: "已损坏",
    stale_evidence: "需要重新检查",
    unknown: "未知",
  },
  maintenanceBackupCompatibilityStatuses: {
    compatible: "兼容",
    migration_required: "需要迁移",
    newer_than_supported: "由较新版本创建",
    unsupported_backend: "不支持的数据库类型",
    unknown: "未知",
  },
  maintenanceBackupValidationStatuses: {
    not_performed: "未执行",
    passed: "已通过",
    failed: "未通过",
    stale_evidence: "需要重新检查",
    unknown: "未知",
  },
  maintenanceBackupOperationLabels: {
    check: "检查",
    create: "创建",
    delete: "删除",
  },
  maintenanceBackupStatuses: {
    valid: "就绪",
    verified: "就绪",
    available: "可用",
    no_artifacts: "无备份",
    blocked: "检查被阻止",
    invalid: "有问题",
    check_failed: "检查失败",
    validated: "试恢复已通过",
    passed: "试恢复已通过",
    interrupted: "检查已中断，可以重试",
    problem: "有问题",
  },
  maintenanceBackupCheckStatuses: {
    valid: "检查通过",
    verified: "检查通过",
    validated: "备份可用、完整且兼容；试恢复已通过。",
    passed: "备份可用、完整且兼容；试恢复已通过。",
    completed: "检查已完成",
    available: "备份可检查",
    no_artifacts: "暂无可检查的备份",
    blocked: "检查被阻止",
    invalid: "备份已损坏或不完整",
    check_failed: "检查失败",
    fallback: "已收到检查状态",
  },
  maintenanceBackupCheckOutcomes: {
    fully_validated: "备份可用、完整且兼容；试恢复已通过。",
    integrity_verified_migration_required: "完整性已确认。未执行试恢复：此备份需要兼容迁移。",
    integrity_failed: "无法确认备份完整性：备份已损坏或不完整。",
    restore_failed: "备份完整性已确认，但试恢复失败。",
  },
  diagnosticArchiveQuestion: "选择诊断归档",
  diagnosticArchiveNormal: "普通",
  diagnosticArchiveExtended: "扩展",
  diagnosticArchiveMessage: "两个选项都包含完整诊断归档，区别仅在 Docker 日志和审计的时间范围。",
  diagnosticArchiveNormalDescription: "完整诊断和当前状态；最近 10 分钟的 Docker 日志与审计。",
  diagnosticArchiveExtendedDescription: "相同的完整诊断；最近 30 分钟的 Docker 日志与审计。",
  maintenanceBackupCreateStatuses: {
    created: "备份已创建",
    verified: "备份已创建",
    valid: "备份已创建",
    completed: "备份已创建",
    blocked: "创建被阻止",
    failed: "无法创建备份",
    fallback: "已收到创建状态",
  },
  maintenanceBackupDeleteStatuses: {
    deleted: "备份已删除",
    deleted_with_missing_files: "备份已删除；部分文件此前已不存在",
    blocked: "删除被阻止",
    failed: "无法删除备份",
    partial_retryable: "删除只完成了一部分，可以重试",
    delete_failed: "无法删除备份",
    not_found: "备份已不存在",
    fallback: "已收到删除状态",
  },
  maintenanceDbAdoptionApply: "采用数据库",
  maintenanceDbAdoptionApplyConfirm: "将现有数据库采用为 KM VMS 工作数据库？仅在内置检查和备份条件通过后执行。",
  maintenanceDbAdoptionApplied: "已采用现有数据库。",
  maintenanceDbAdoptionApplyFailed: "无法采用现有数据库。",
  maintenanceDryRun: "检查",
  maintenanceDryRunResult: "检查已完成。",
  maintenanceNoApply: "更新只会通过已确认的 helper 流程应用。",
  maintenanceBackupRequired: "需要备份",
  maintenanceBackupNotRequired: "不需要备份",
  maintenanceConfirmationRequired: "需要确认",
  maintenanceUnsupported: "操作被阻止或不受支持",
  yes: "是",
  no: "否",
  maintenanceLastAction: "最近操作",
  maintenanceNoHistory: "未找到操作历史",
  maintenanceGeneratedAt: "生成时间",
  maintenanceWarnings: "诊断",
  maintenanceWarningDetails: "显示详情",
  maintenanceWarningHide: "隐藏详情",
  maintenanceSupportStatusOk: "无关键问题",
  maintenanceWarningActionable: "需要处理",
  maintenanceWarningInfo: "信息限制",
  maintenanceWarningSupport: "给支持人员",
  maintenanceWarningNone: "没有活动警告。",
  maintenanceSupportReportAction: "下载报告并发送给支持人员。",
  maintenanceWarningGeneric: {
    actionable: { title: "需要检查", summary: "某个状态可能影响维护。", action: "下载报告，并根据其中的信息修复原因。" },
    informational: { title: "诊断限制", summary: "这不是故障：报告只是说明部分检查未自动执行。", action: "系统正常时无需操作。" },
    support: { title: "支持信息", summary: "此项有助于诊断，但不一定需要用户操作。", action: "需要支持时请下载报告。" },
  },
  maintenanceWarningLabels: {
    backup_status_source_unavailable: { title: "没有连接的备份状态来源", summary: "只读诊断未找到可证明备份状态的独立安全来源。", action: "请在备份区创建或检查备份。" },
    restore_validation_status_source_unavailable: { title: "没有恢复检查来源", summary: "诊断未收到备份已在临时数据库中检查的独立证据。", action: "请在备份区运行备份检查。" },
    backup_root_persistence_unknown: { title: "备份目录未执行写入探测", summary: "报告没有写入测试备份目录，因为不能在没有命令时改变系统。", action: "这是报告的信息性限制。" },
    video_archive_restore_not_covered: { title: "视频归档不在数据库备份内", summary: "备份保护数据库和服务元数据，但不复制摄像机录像。", action: "视频归档存储需单独检查。" },
    production_adoption_deferred: { title: "服务数据库接管已延后", summary: "诊断是只读的，不会在没有单独操作时更改服务元数据。", action: "系统正常时无需操作。" },
    installed_build_development_fallback: { title: "Build 元数据不完整", summary: "已安装环境没有提供完整 build 元数据来源。", action: "下次更新时检查 release identity。" },
  },
  maintenanceMessageFallback: "已收到状态，详细信息不可用。",
  maintenanceActionFallback: "该操作当前不可用。请检查系统状态后重试。",
  updateApplyTitle: "KM VMS 更新",
  updateApplyCheck: "检查更新",
  updateApplyStart: "更新 KM VMS",
  updateApplyConfirm: "安装可用的 KM VMS 更新？",
  updateApplyConfirmRestart: "服务可能会短暂重启；API 恢复后状态轮询会继续。",
  updateApplyModalTitle: "KM VMS 更新",
  updateApplyModalConfirm: "更新 KM VMS",
  updateApplyLaunchChecking: "正在检查更新是否已启动",
  updateApplyLaunchCheckingText: "服务器响应暂时不可用。KM VMS 正在检查状态，不会发送第二次请求。",
  updateApplyLaunchUnknown: "尚未确认启动结果。系统会自动继续检查，并阻止再次应用。",
  updateApplyLaunchConflict: "检测到另一个更新操作。在获得最终状态前，无法再次应用。",
  updateApplyLaunchNotAccepted: "服务器已确认请求未被接受。再次尝试前需重新确认。",
  updateApplyLaunchRejected: "服务器拒绝了更新启动请求。",
  updateApplyLocked: "正在检查启动",
  updateApplyPeerCheckUnavailable: "暂时无法检查已发布版本，但已收到更新应用状态。",
  updateApplyTransportErrors: {
    network_unavailable: "暂时无法连接更新服务。系统会自动继续检查。",
    temporarily_unavailable: "更新服务正在重启或暂时不可用。系统会自动继续检查。",
    unauthorized: "请重新登录后检查更新。",
    permission_denied: "您没有管理更新的权限。",
    request_failed: "无法获取更新状态。系统会自动继续检查。",
    typed_backend_error: "更新服务报告了错误。",
  },
  updateApplyQueued: "KM VMS 更新请求已交给 helper。",
  updateApplyUnavailable: "现在无法启动更新。请查看此面板中的消息，必要时重新检查更新。",
  updateApplyConnection: "服务可能会短暂重启；状态轮询会自动继续。",
  updateApplyRecoveryAvailable: "检查目标版本和 commit，然后启动应用。",
  updateApplyRecoveryBlocked: "修复受信任版本或服务器配置阻塞项后重新检查。",
  updateApplyRecoveryCommitMismatch: "已安装 commit 与受信任版本 commit 不一致。请将更新视为失败，并检查服务器端来源后重试。",
  updateApplyRecoveryCompleted: "更新已完成，已安装 commit 已验证。",
  updateApplyRecoveryCurrent: "已安装版本与受信任版本一致。",
  updateApplyRecoveryFailed: "更新失败。请查看更新状态，并在修复原因后重试。",
  updateApplyRecoveryRolledBack: "目标版本未通过验证。KM VMS 已恢复到之前可用的版本；请在重试前解决原因。",
  updateApplyRecoveryCheckFailed: "更新检查未完成。请重新检查，或下载报告并发送给支持人员。",
  updateApplyRecoveryLiveCheckFailedWithSnapshot: "最新实时检查失败，但仍有新鲜的可信检查可用于安全应用。",
  updateApplyRecoveryRefreshRequired: "版本已变化或检查已过期。请重新运行检查更新。",
  updateApplyRecoveryMissingCommit: "该版本缺少可信 commit 证据，无法安全应用更新。",
  updateApplyRecoveryReconnecting: "服务可能正在重启。界面会继续轮询，并在 API 恢复后重新读取状态。",
  updateApplyRecoveryRunning: "Helper 正在应用更新。请保持 NAS 供电并等待最终状态。",
  updateApplyRecoveryStalled: "更新状态已经较久没有变化。helper 或锁可能仍处于活动状态时不要重复启动，请先检查服务器状态。",
  updateApplyRecoveryUnknown: "更新状态暂时未知。请刷新检查或等待 API 响应。",
  updateApplyRecoveryIdentity: "已安装版本元数据不完整或与当前代码不一致。在接管 release identity 前无法应用更新。",
  updateApplyRecoveryProvider: "版本来源暂时不可用。请重试；最近一次成功的可信检查只能在有限时间内用于应用更新。",
  updateApplyRecoveryInstalledNewer: "已安装版本高于已发布版本。为避免降级，应用更新已被阻止。",
  updateApplyTechnicalDetails: "技术详情",
  updateApplyProgress: "更新进度",
  updateApplyButtonRunning: "正在更新",
  updateApplyButtonRebuilding: "重建中",
  updateApplyButtonHealth: "测试",
  updateApplyButtonVerification: "Commit 检查",
  updateApplyHeadlines: {
    current: "系统已是最新",
    available: "有可用更新",
    running: "正在应用更新",
    completed: "已成功完成",
    blocked: "需要处理",
    unknown: "更新状态暂时未知",
  },
  updateApplySummaries: {
    current: "已安装版本与已发布版本一致。",
    available: "请检查版本信息，准备好后应用更新。",
    running: "Helper 正在应用更新，并会自动刷新进度。",
    completed: "更新已安装并验证。",
    blocked: "当前无法应用；诊断中包含安全详情。",
    unknown: "最后收到的信息已过期。系统会自动继续检查状态。",
  },
  updateApplyResults: {
    completedVerified: "已成功完成",
    current: "当前",
    available: "有可用更新",
    running: "进行中",
    blocked: "需要处理",
    unknown: "状态暂时未知",
  },
  updateApplyReleaseChanges: "此版本的变更",
  updateApplyReleaseTitleFallback: "KM VMS 版本",
  updateApplyReleaseSummaryFallback: "版本说明缺失。",
  updateApplyLastState: "最新进度",
  updateApplyStepDone: "完成",
  updateApplyTimelineCurrent: "当前版本",
  updateApplyHistoryLimited: "此前应用未记录详细步骤历史。",
  updateApplyDiagnostics: "诊断",
  updateApplySupportTitle: "需要支持帮助？",
  updateApplySupportText: "下载诊断报告并发送给支持人员。技术详情已包含在报告中。",
  updateApplySupportAction: "下载诊断报告",
  updateCommitPending: "等待验证",
  updateCommitUnavailable: "无数据",
  updateCommitVerified: "Commit 已验证",
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
    installedCommit: "已安装 commit",
    reportId: "报告 ID",
    source: "来源",
    targetCommit: "目标 commit",
    verification: "Commit 检查",
    currentStep: "当前步骤",
    lastProgress: "最近进度",
    elapsed: "已用时间",
    completedAt: "完成时间",
    releaseIdentity: "版本已确认",
    releaseTitle: "版本",
    releaseSummary: "变更",
    status: "更新",
    gitHead: "Git HEAD",
    metadataSource: "元数据",
    provider: "提供方",
  },
  maintenanceFlows: {
    db_adoption: "数据库接管",
    migration: "迁移",
    restore: "备份恢复检查",
    update: "更新",
  },
  maintenanceReadinessTitle: "数据库与恢复准备状态",
  maintenanceReadinessText: "仅在更新、迁移、迁移安装或恢复时使用的维护状态。",
  maintenanceSupportTitle: "支持诊断",
  maintenanceSupportText: "如果维护状态不清楚或被阻止，请下载安全报告并发送给支持人员。",
  maintenanceReadinessTitles: {
    db_identity: "数据库",
    db_schema: "数据库结构",
    backup_restore_check: "备份恢复检查",
  },
  maintenanceOperatorStatuses: {
    ok: "正常",
    attention: "需要检查",
    blocked: "需要处理",
    unavailable: "无数据",
    action_available: "可执行操作",
  },
  maintenanceOperatorSummaries: {
    db_identity_ok: "应用已识别此数据库为当前系统数据库，无需额外操作。",
    db_identity_adoptable: "可在备份和明确确认后安全接管数据库。",
    db_identity_blocked: "数据库尚未准备好接管，执行任何操作前需要诊断。",
    db_schema_current: "数据库结构已适配当前应用版本。",
    db_schema_pending: "当前应用版本有待执行的数据库结构变更。",
    db_schema_blocked: "数据库结构检查被阻止，需要诊断。",
    backup_restore_no_artifacts: "配置的备份目录中没有可验证的恢复备份。",
    backup_restore_artifacts_available: "已有备份，可在临时数据库中检查恢复。",
    backup_restore_validation_available: "可在临时数据库中执行安全恢复检查。",
    backup_restore_blocked: "备份恢复检查被阻止，需要诊断。",
  },
  maintenanceOperatorActions: {
    db_identity_check_optional: "此项通常不需要操作。",
    db_identity_apply_requires_confirmation: "接管只会更改服务元数据，并需要单独确认。",
    migration_check_optional: "此项通常不需要操作。",
    migration_apply_requires_confirmation: "迁移只会在备份后并经过单独确认才会执行。",
    backup_restore_create_backup_first: "需要先有有效备份。",
    backup_restore_check_available: "检查会在工作数据库之外执行。",
    download_support_report: "下载诊断报告并发送给支持人员。",
    check_status: "可以执行安全状态检查。",
  },
  maintenanceCheckActions: {
    db_adoption: "检查数据库",
    migration: "检查迁移",
    restore: "检查备份",
  },
  maintenanceFactLabels: {
    metadata_present: "元数据",
    already_adopted: "已接管",
    current_version: "当前",
    target_version: "目标",
    pending_count: "待处理",
    valid_artifacts: "备份",
    temporary_validation: "临时检查",
    current_product_restore: "恢复到工作数据库",
  },
  maintenanceStatuses: {
    ok: "正常",
    current: "当前",
    available: "可用",
    adoptable: "可接管",
    adopted: "已接管",
    already_adopted: "已接管",
    blocked: "已阻止",
    cancelled: "已取消",
    checking: "检查中",
    check_failed: "检查失败",
    complete: "已完成",
    completed: "已完成",
    valid: "已验证",
    verified: "已验证",
    compose_config: "Compose 检查",
    commit_verification: "Commit 检查",
    drift_known_safe: "无关键问题",
    draft_known_safe: "已知安全的草稿",
    downloading: "下载中",
    extracting: "解压中",
    failed: "失败",
    health_check: "测试",
    request: "请求",
    applying: "更新中",
    apply: "更新",
    acquire_source: "获取来源",
    overlay: "覆盖文件",
    no_artifacts: "无工件",
    not_configured: "未配置",
    not_cancelable: "不可取消",
    preflight: "预检查",
    queued: "排队中",
    rebuilding: "重建中",
    preparing: "准备中",
    staging: "准备版本",
    activating: "激活中",
    reconnecting: "重新连接",
    rolling_back: "恢复之前版本",
    failed_rolled_back: "已恢复之前版本",
    restarting: "重启中",
    running: "进行中",
    pending: "等待中",
    stalled: "停滞",
    starting_helper: "启动 helper",
    update_available: "有可用更新",
    identity_incomplete: "身份信息不完整",
    installed_identity_drift: "安装存在差异",
    metadata_stale: "元数据已过期",
    provider_unavailable: "来源不可用",
    no_release_published: "未发布版本",
    installed_newer_than_available: "已安装版本更高",
    validating_source: "验证来源",
    limited: "受限",
    unknown: "未知",
  },
  maintenanceMessageLabels: {
    schema_metadata_valid: "架构元数据已有效。",
    schema_current_no_pending_migrations: "架构已是最新，没有待执行的迁移。",
    schema_update_failed: "数据库架构准备失败。",
    schema_update_retry_after_cause_resolved: "解决数据库架构准备失败的原因后再重试更新。",
    slot_adoption_conflict: "保存的上一版本与当前安装不再匹配。",
    slot_adoption_conflict_action: "重试前请检查已安装源代码和服务运行状态。",
    restore_no_valid_artifacts: "配置的备份根目录中没有可用的恢复工件。",
    update_apply_not_available_for_release: "此版本不支持在界面内应用。",
    maintenance_history_limited: "持久历史记录有限：仅显示当前状态和最新安全报告。",
    drift_known_safe: "无关键问题。",
    draft_known_safe: "已知安全的草稿。",
    complete: "已完成。",
    completed: "已完成。",
  },
  updateWarningGeneric: "存在更新告警。安全详情不可用。",
  updateWarningLabels: {
    source_metadata_invalid: "已安装来源元数据不可用或已损坏。",
    source_metadata_missing: "未找到已安装来源元数据。",
    source_metadata_unavailable: "已安装来源元数据不可用。",
    source_metadata_unsupported_schema: "不支持已安装来源元数据结构。",
    update_metadata_invalid: "最近更新元数据不可用或已损坏。",
    update_metadata_missing: "未找到最近更新元数据。",
    update_metadata_unavailable: "最近更新元数据不可用。",
    update_metadata_unsupported_schema: "不支持最近更新元数据结构。",
    installed_commit_invalid: "已安装 commit 格式无效。",
    trusted_commit_missing: "已发布版本缺少已验证 commit。",
    identity_incomplete: "已安装版本元数据不完整。",
    installed_identity_drift: "版本元数据与当前 Git HEAD 不一致。",
    no_release_published: "未找到公共 release descriptor。",
    installed_newer_than_available: "已安装版本高于已发布版本。",
    trusted_manifest_not_configured: "服务器未配置 trusted release manifest。",
    check_failed: "更新检查未正常完成。",
    update_check_already_running: "更新检查已在进行中。请等待当前检查完成。",
    manual_update_check_rate_limited: "最近已请求更新检查。可再次运行检查或刷新状态。",
    commit_mismatch: "已安装 commit 与 trusted release commit 不一致。",
    token_not_configured: "私有来源的服务器端 token 未配置。",
    requires_migration: "该 release 需要迁移支持，此界面不会运行迁移。",
    requires_backup: "该 release 需要在应用前创建备份。",
    requires_manual_action: "该 release 需要操作员手动处理。",
    blocked: "当前条件阻止更新。",
    unsupported: "当前不支持此操作。",
    rollback_unsupported: "当前 update helper 不支持 rollback。",
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

function MaintenanceCheckIcon() {
  return <span aria-hidden="true" className="storageOpsCheckIcon">✓</span>;
}

function MaintenanceBackupDimensionStatus({ tone, label }) {
  if (!["ok", "problem"].includes(tone)) {
    return <strong className={`settingsMaintenanceBackupStatusPill is-${tone}`}>{label}</strong>;
  }
  return (
    <strong
      className={`settingsMaintenanceBackupStatusPill is-${tone} is-symbol`}
      role="img"
      aria-label={label}
      title={label}
    >
      {tone === "ok"
        ? <MaintenanceCheckIcon />
        : <span aria-hidden="true" className="storageOpsCheckIcon">×</span>}
    </strong>
  );
}

function MaintenanceTrashIcon() {
  return (
    <svg className="recordingsUiIcon recordingsTrashIcon recordingsRowSvgIcon storageOpsTrashIcon" viewBox="0 1 24 24" aria-hidden="true" focusable="false">
      <path d="M4.2 6.8h15.6"></path>
      <path d="M8.9 6.8V4.5h6.2v2.3"></path>
      <path d="M6.7 7.2 7.6 19c.1 1.05 1 1.9 2.05 1.9h4.7c1.05 0 1.95-.85 2.05-1.9l.9-11.8"></path>
      <path d="M10.1 10.7v6.6"></path>
      <path d="M13.9 10.7v6.6"></path>
    </svg>
  );
}

function MaintenanceRestoreIcon() {
  return (
    <svg className="settingsMaintenanceRestoreIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <ellipse cx="12" cy="5.5" rx="6.2" ry="2.6" />
      <path d="M5.8 5.5v5.2c0 1.4 2.8 2.6 6.2 2.6 1.1 0 2.1-.1 3-.3" />
      <path d="M5.8 10.7v5.2c0 1.4 2.8 2.6 6.2 2.6" />
      <path d="M19 13.3a4.2 4.2 0 1 0 .5 4.7" />
      <path d="M19.1 10.9v3.5h-3.5" />
    </svg>
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
  const [maintenanceBackupDetail, setMaintenanceBackupDetail] = useState(null);
  const [maintenanceBackupDetailOpen, setMaintenanceBackupDetailOpen] = useState(false);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceError, setMaintenanceError] = useState("");
  const [maintenanceBusy, setMaintenanceBusy] = useState("");
  const [maintenanceActionResult, setMaintenanceActionResult] = useState(null);
  const [maintenanceBackupResult, setMaintenanceBackupResult] = useState(null);
  const [maintenanceConfirm, setMaintenanceConfirm] = useState(null);
  const [maintenanceBackupPending, setMaintenanceBackupPending] = useState(null);
  const [currentRestoreDialog, setCurrentRestoreDialog] = useState(null);
  const [currentRestorePending, setCurrentRestorePending] = useState(null);
  const [currentRestoreStatus, setCurrentRestoreStatus] = useState(null);
  const [updateStatus, setUpdateStatus] = useState(null);
  const [updateApplyStatus, setUpdateApplyStatus] = useState(null);
  const [updateTransportErrors, setUpdateTransportErrors] = useState({ update: null, apply: null });
  const [updateApplyReconnectSnapshot, setUpdateApplyReconnectSnapshot] = useState(null);
  const [updateApplyClockMs, setUpdateApplyClockMs] = useState(() => Date.now());
  const [updateApplyDialog, setUpdateApplyDialog] = useState(null);
  const [updateApplyPending, setUpdateApplyPending] = useState(null);
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
  const updatePollInFlightRef = useRef(false);
  const updateApplyPendingRef = useRef(null);
  const maintenanceBackupPendingRef = useRef(null);
  const maintenanceBackupRecoveryRef = useRef(false);
  const maintenanceBackupAdmissionRef = useRef(null);
  const maintenanceBackupPollInFlightRef = useRef(false);
  const maintenanceBackupDetailRef = useRef(null);
  const currentRestorePendingRef = useRef(null);
  const currentRestorePollInFlightRef = useRef(false);
  const currentRestoreDialogRef = useRef(null);
  const updateApplyDialogRef = useRef(null);
  const maintenanceChildDialogOpenRef = useRef(false);
  const maintenanceDialogRef = useRef(null);
  const maintenanceTriggerRef = useRef(null);
  const maintenanceBusyRef = useRef("");
  const lang = languageOf(draft || savedDraft);
  const t = settingsTextFor(lang);
  const dirty = Boolean(draft && savedDraft && !samePayload(draft, savedDraft));
  const anyBusy = saving || hardwareChecking;
  const canManageMaintenance = Boolean(currentUser?.permissions?.includes("manage_settings"));
  const canManageUsers = Boolean(currentUser?.permissions?.includes("manage_users"));
  const sortedUsers = useMemo(() => sortedUsersForTable(users), [users]);
  const languageIcon = lang === "en" ? "/assets/icons/ui/language-en.png" : "/assets/icons/ui/language-ru.png";
  const updateApplyHasUnknownLaunch = Boolean(updateApplyPending);
  const updateApplyOperator = updateApplyOperatorModel(updateStatus, updateApplyStatus, t, lang, {
    updateError: updateTransportErrors.update,
    applyError: updateTransportErrors.apply,
    reconnectTiming: updateApplyReconnectSnapshot,
    nowMs: updateApplyClockMs,
    unresolvedSubmission: updateApplyHasUnknownLaunch,
  });
  const updateApplyRunning = updateApplyIsRunning(updateApplyStatus?.status || "") && !updateApplyOperator.stateUnknown;
  const updateApplyAllowed = Boolean(
    updateApplyOperator.canApply
    && !updateApplyPending
    && !updateApplyDialog
    && !maintenanceBusy
    && !currentRestorePending,
  );
  const updateApplyPrimaryText = updateApplyPending || updateApplyOperator.stateUnknown
      ? t.updateApplyLocked
      : updateApplyButtonText(updateApplyStatus, t);
  const updateApplyErrors = updateApplyErrorMessages(updateApplyStatus?.error, t, lang);
  const maintenanceBackupOverview = useMemo(
    () => maintenanceBackupOverviewModel(maintenanceOverview, t, lang),
    [maintenanceOverview, t, lang],
  );
  const maintenanceBackupManager = useMemo(
    () => maintenanceBackupDetailModel(maintenanceBackupDetail, t, lang),
    [maintenanceBackupDetail, t, lang],
  );
  const maintenanceDatabase = useMemo(
    () => maintenanceDatabaseOverviewModel(maintenanceOverview, t),
    [maintenanceOverview, t],
  );
  const maintenanceWarnings = useMemo(() => maintenanceWarningModel(maintenanceOverview, t), [maintenanceOverview, t]);
  const maintenanceChildDialogOpen = Boolean(
    maintenanceConfirm
    || currentRestoreDialog
    || updateApplyDialog
    || diagnosticChoiceOpen
  );
  const maintenanceOverall = useMemo(
    () => maintenanceOverallHealthModel({
      overview: maintenanceOverview,
      updateOperator: updateApplyOperator,
      database: maintenanceDatabase,
      backup: maintenanceBackupOverview,
      warnings: maintenanceWarnings,
      loading: maintenanceLoading,
      loadError: Boolean(maintenanceError),
      t,
    }),
    [
      maintenanceOverview,
      updateApplyOperator,
      maintenanceDatabase,
      maintenanceBackupOverview,
      maintenanceWarnings,
      maintenanceLoading,
      maintenanceError,
      t,
    ],
  );
  const maintenanceBackupResultModel = useMemo(() => (
    maintenanceBackupResult ? maintenanceBackupOperationResultText(maintenanceBackupResult, t) : null
  ), [maintenanceBackupResult, t]);
  const maintenanceBackupProgressKind = String(
    maintenanceBackupPending?.kind || maintenanceBackupResult?.kind || "check",
  );
  const maintenanceBackupProgressText = maintenanceBackupResult?.recovering
    ? t.maintenanceBackupRecovering
    : maintenanceBackupProgressKind === "create"
      ? t.maintenanceBackupCreating
      : maintenanceBackupProgressKind === "delete"
        ? t.maintenanceBackupDeleting
        : t.maintenanceBackupChecking;
  const updateApplyLaunchNotice = updateApplyPending ? t.updateApplyLaunchUnknown : "";

  maintenanceBusyRef.current = maintenanceBusy;
  updateApplyDialogRef.current = updateApplyDialog;
  updateApplyPendingRef.current = updateApplyPending;
  maintenanceBackupPendingRef.current = maintenanceBackupPending;
  maintenanceBackupDetailRef.current = maintenanceBackupDetail;
  currentRestorePendingRef.current = currentRestorePending;
  currentRestoreDialogRef.current = currentRestoreDialog;
  maintenanceChildDialogOpenRef.current = maintenanceChildDialogOpen;

  useEffect(() => {
    load();
    function onLanguage(event) {
      if (event.detail) patch("language", normalizeLocale(event.detail));
    }
    window.addEventListener("km-vms-language", onLanguage);
    return () => {
      window.removeEventListener("km-vms-language", onLanguage);
    };
  }, []);

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(BACKUP_OPERATION_PENDING_STORAGE_KEY);
      const restored = restoreBackupOperationPending(raw, Date.now());
      if (restored) {
        maintenanceBackupRecoveryRef.current = true;
        maintenanceBackupPendingRef.current = restored;
        setMaintenanceBackupPending(restored);
        setMaintenanceBackupResult({
          kind: restored.kind,
          status: "running",
          state: "running",
          recovering: true,
        });
      } else if (raw !== null) {
        window.sessionStorage.removeItem(BACKUP_OPERATION_PENDING_STORAGE_KEY);
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(UPDATE_APPLY_PENDING_STORAGE_KEY);
      const restored = restoreUpdateApplyPending(raw, Date.now());
      if (restored) {
        updateApplyPendingRef.current = restored;
        setUpdateApplyPending(restored);
      }
    } catch {}
  }, []);

  useEffect(() => {
    try {
      const raw = window.sessionStorage.getItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      const value = raw ? JSON.parse(raw) : null;
      const valid = value
        && typeof value === "object"
        && typeof value.submissionId === "string"
        && typeof value.artifactId === "string"
        && Number.isFinite(value.createdAt)
        && Date.now() - value.createdAt < 24 * 60 * 60 * 1000;
      if (valid) {
        currentRestorePendingRef.current = value;
        setCurrentRestorePending(value);
        setCurrentRestoreDialog({
          artifact: { id: value.artifactId, createdAt: "-" },
          phrase: CURRENT_RESTORE_CONFIRMATION_PHRASE,
          preflight: { can_restore: true, reason_codes: [] },
          preflightBusy: false,
          accepted: true,
          reconnecting: true,
          error: "",
        });
      } else if (raw !== null) {
        window.sessionStorage.removeItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      }
    } catch {
      try {
        window.sessionStorage.removeItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      } catch {}
    }
  }, []);

  useEffect(() => {
    if (!canManageMaintenance || !currentRestorePending) return undefined;
    let cancelled = false;
    let timer = null;
    let delay = 1500;
    const tick = async () => {
      const result = await pollCurrentRestoreStatus();
      if (cancelled || result?.terminal_result) return;
      delay = result ? 2500 : Math.min(10000, Math.max(2500, delay * 2));
      timer = window.setTimeout(tick, delay);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [canManageMaintenance, currentRestorePending?.submissionId]);

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
    const active = updateApplyIsRunning(updateApplyStatus?.status || "");
    if (!canManageMaintenance || (!maintenanceModalOpen && !active && !updateApplyPending)) return undefined;
    const timer = window.setInterval(() => loadUpdateApplySurface({ silent: true }), UPDATE_APPLY_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [maintenanceModalOpen, canManageMaintenance, updateApplyStatus?.status, Boolean(updateApplyPending)]);

  useEffect(() => {
    if (!canManageMaintenance || !maintenanceBackupPending) return undefined;
    reconcilePendingBackupOperation();
    const timer = window.setInterval(
      reconcilePendingBackupOperation,
      MAINTENANCE_BACKUP_POLL_INTERVAL_MS,
    );
    return () => window.clearInterval(timer);
  }, [canManageMaintenance, maintenanceBackupPending?.submissionId]);

  useEffect(() => {
    if (!maintenanceModalOpen) return undefined;
    return acquireSettingsBodyScrollLock();
  }, [maintenanceModalOpen]);

  useEffect(() => {
    if (!maintenanceModalOpen) return undefined;
    const container = maintenanceDialogRef.current;
    const initial = focusableElements(container)[0];
    initial?.focus();
    function onKeyDown(event) {
      if (event.defaultPrevented || maintenanceChildDialogOpenRef.current) return;
      if (event.key === "Escape" && !maintenanceBusyRef.current) {
        event.preventDefault();
        closeMaintenanceModal();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = focusableElements(container);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (!maintenanceChildDialogOpenRef.current) maintenanceTriggerRef.current?.focus();
    };
  }, [maintenanceModalOpen]);

  function showToast(nextToast) {
    setToast({
      ...nextToast,
      tone: nextToast?.tone || nextToast?.variant || "info",
      message: nextToast?.message ?? nextToast?.text ?? "",
    });
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
    if (maintenanceChildDialogOpenRef.current) return;
    setMaintenanceModalOpen(false);
    setMaintenanceBackupDetailOpen(false);
    setMaintenanceActionResult(null);
    setMaintenanceBackupResult(null);
    setMaintenanceConfirm(null);
    setMaintenanceError("");
  }

  function safeUpdateTransportError(error, fallback) {
    const category = String(error?.category || "request_failed");
    return {
      category,
      status: Number(error?.status || 0),
      message: t.updateApplyTransportErrors?.[category] || fallback,
    };
  }

  function safeUpdateLaunchError(error) {
    const code = String(error?.code || "");
    if (code === "update_already_running") return t.updateApplyLaunchConflict;
    return safeUpdateTransportError(error, t.updateApplyLaunchRejected).message;
  }

  function commitUpdateApplyPending(nextRecord) {
    const safeRecord = nextRecord
      ? sanitizeUpdateApplyPending(nextRecord, Date.now())
      : null;
    if (nextRecord && !safeRecord) return null;
    updateApplyPendingRef.current = safeRecord;
    setUpdateApplyPending(safeRecord);
    try {
      if (safeRecord) {
        window.sessionStorage.setItem(UPDATE_APPLY_PENDING_STORAGE_KEY, JSON.stringify(safeRecord));
      } else {
        window.sessionStorage.removeItem(UPDATE_APPLY_PENDING_STORAGE_KEY);
      }
    } catch {}
    return safeRecord;
  }

  function commitBackupOperationPending(nextRecord) {
    const safeRecord = nextRecord
      ? sanitizeBackupOperationPending(nextRecord, Date.now())
      : null;
    if (nextRecord && !safeRecord) return null;
    try {
      if (safeRecord) {
        window.sessionStorage.setItem(
          BACKUP_OPERATION_PENDING_STORAGE_KEY,
          JSON.stringify(safeRecord),
        );
      } else {
        window.sessionStorage.removeItem(BACKUP_OPERATION_PENDING_STORAGE_KEY);
      }
    } catch {
      if (safeRecord) return null;
    }
    maintenanceBackupPendingRef.current = safeRecord;
    setMaintenanceBackupPending(safeRecord);
    return safeRecord;
  }

  function commitCurrentRestorePending(nextRecord) {
    const safeRecord = nextRecord
      && typeof nextRecord === "object"
      && typeof nextRecord.submissionId === "string"
      && typeof nextRecord.artifactId === "string"
      && Number.isFinite(nextRecord.createdAt)
      ? {
          submissionId: nextRecord.submissionId,
          artifactId: nextRecord.artifactId,
          createdAt: nextRecord.createdAt,
        }
      : null;
    currentRestorePendingRef.current = safeRecord;
    setCurrentRestorePending(safeRecord);
    try {
      if (safeRecord) {
        window.sessionStorage.setItem(
          CURRENT_RESTORE_PENDING_STORAGE_KEY,
          JSON.stringify(safeRecord),
        );
      } else {
        window.sessionStorage.removeItem(CURRENT_RESTORE_PENDING_STORAGE_KEY);
      }
    } catch {}
    return safeRecord;
  }

  function currentRestoreReasonCode(value) {
    const detail = value?.detail && typeof value.detail === "object"
      ? value.detail
      : value?.data?.detail && typeof value.data.detail === "object"
        ? value.data.detail
        : {};
    return String(
      value?.reason_code
      || value?.code
      || detail.reason_code
      || detail.code
      || detail.preflight?.reason_codes?.[0]
      || "",
    ).trim();
  }

  function currentRestoreReasonText(
    code,
    fallback = t.maintenanceCurrentRestoreRequestRejected,
  ) {
    const normalized = String(code || "").trim();
    const aliases = {
      artifact_schema_migration_required: "artifact_schema_migration_required",
      artifact_schema_newer: "artifact_schema_newer",
      schema_migration_required: "schema_migration_required",
      schema_newer_than_supported: "schema_newer_than_supported",
      compatibility_migration_required: "artifact_schema_migration_required",
      migration_required: "artifact_schema_migration_required",
      newer_than_supported: "artifact_schema_newer",
      unsupported_backend: "artifact_backend_unsupported",
      unknown: "artifact_invalid",
    };
    const key = aliases[normalized] || normalized;
    return t.maintenanceCurrentRestoreReasons?.[key]
      || fallback;
  }

  function currentRestoreFailedPhase(status) {
    const exact = String(status?.failed_phase || "");
    if (CURRENT_RESTORE_OPERATIONAL_PHASES.includes(exact)) {
      return exact;
    }
    return CURRENT_RESTORE_LEGACY_REASON_PHASES[
      String(status?.reason_code || "")
    ] || "";
  }

  function currentRestoreTerminal(status = currentRestoreStatus) {
    return ["completed", "blocked", "failed_rolled_back", "failed_recovery_required"]
      .includes(String(status?.terminal_result || ""));
  }

  function currentRestoreTerminalText(status = currentRestoreStatus) {
    const terminal = String(status?.terminal_result || "");
    if (terminal === "completed") return t.maintenanceCurrentRestoreCompleted;
    if (terminal === "failed_rolled_back") return t.maintenanceCurrentRestoreRolledBack;
    if (terminal === "failed_recovery_required") {
      return currentRestoreReasonText(
        status?.reason_code,
        t.maintenanceCurrentRestoreRecoveryRequired,
      );
    }
    if (terminal === "blocked") {
      return currentRestoreReasonText(status?.reason_code);
    }
    return "";
  }

  function closeCurrentRestoreDialog() {
    if (!currentRestoreDialog) return;
    if (currentRestoreDialog.accepted && !currentRestoreTerminal()) return;
    setCurrentRestoreDialog(null);
    if (currentRestoreTerminal()) setCurrentRestoreStatus(null);
  }

  async function pollCurrentRestoreStatus(pendingOverride = null) {
    const pending = pendingOverride || currentRestorePendingRef.current;
    if (!pending || currentRestorePollInFlightRef.current) return null;
    currentRestorePollInFlightRef.current = true;
    try {
      const result = await apiFetch("/system/restore/current/status");
      const resultSubmissionId = String(result?.submission_id || "");
      if (
        String(result?.status || "") !== "idle"
        && resultSubmissionId !== pending.submissionId
      ) {
        setCurrentRestoreDialog((current) => ({
          ...(current || {}),
          artifact: current?.artifact || {
            id: pending.artifactId,
            createdAt: "-",
          },
          accepted: true,
          reconnecting: true,
          error: "",
        }));
        return null;
      }
      if (String(result?.status || "") === "idle") {
        setCurrentRestoreDialog((current) => current ? {
          ...current,
          accepted: true,
          reconnecting: true,
          error: "",
        } : current);
        return null;
      }
      setCurrentRestoreStatus(result);
      const terminal = currentRestoreTerminal(result);
      const sourceArtifact = result?.artifact || {};
      setCurrentRestoreDialog((current) => {
        const artifact = current?.artifact || {
          id: pending.artifactId,
          createdAt: sourceArtifact.artifact_created_at
            ? formatAuditTimestamp(sourceArtifact.artifact_created_at, lang)
            : "-",
        };
        return {
          ...(current || {}),
          artifact,
          phrase: current?.phrase || CURRENT_RESTORE_CONFIRMATION_PHRASE,
          preflight: current?.preflight || { can_restore: true, reason_codes: [] },
          preflightBusy: false,
          accepted: true,
          reconnecting: false,
          error: "",
        };
      });
      if (terminal) {
        commitCurrentRestorePending(null);
        await refreshMaintenanceBackupProjections();
      }
      return result;
    } catch (error) {
      const expectedRestart = updateApplyRequestIsAmbiguous(error);
      setCurrentRestoreDialog((current) => ({
        ...(current || {}),
        artifact: current?.artifact || {
          id: pending.artifactId,
          createdAt: "-",
        },
        phrase: current?.phrase || CURRENT_RESTORE_CONFIRMATION_PHRASE,
        preflight: current?.preflight || { can_restore: true, reason_codes: [] },
        preflightBusy: false,
        accepted: true,
        reconnecting: expectedRestart,
        error: expectedRestart
          ? ""
          : currentRestoreReasonText(currentRestoreReasonCode(error)),
      }));
      return null;
    } finally {
      currentRestorePollInFlightRef.current = false;
    }
  }

  async function requestCurrentDatabaseRestore(artifact) {
    if (
      !artifact?.id
      || !artifact.canRestore
      || maintenanceBusy
      || maintenanceBackupPendingRef.current
      || currentRestorePendingRef.current
    ) return;
    setCurrentRestoreStatus(null);
    setCurrentRestoreDialog({
      artifact,
      phrase: "",
      preflight: null,
      preflightBusy: true,
      accepted: false,
      reconnecting: false,
      error: "",
    });
    try {
      const preflight = await apiFetch("/system/restore/current/preflight", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ artifact_id: artifact.id }),
      });
      setCurrentRestoreDialog((current) => (
        current?.artifact?.id === artifact.id
          ? {
              ...current,
              preflight,
              preflightBusy: false,
              error: preflight?.can_restore
                ? ""
                : currentRestoreReasonText(preflight?.reason_codes?.[0]),
            }
          : current
      ));
    } catch (error) {
      const detail = error?.data?.detail;
      const preflight = detail && typeof detail === "object" ? detail.preflight : null;
      setCurrentRestoreDialog((current) => (
        current?.artifact?.id === artifact.id
          ? {
              ...current,
              preflight,
              preflightBusy: false,
              error: currentRestoreReasonText(currentRestoreReasonCode(error)),
            }
          : current
      ));
    }
  }

  async function confirmCurrentDatabaseRestore() {
    const dialog = currentRestoreDialog;
    if (
      !dialog
      || dialog.preflightBusy
      || dialog.accepted
      || dialog.preflight?.can_restore !== true
      || dialog.phrase !== CURRENT_RESTORE_CONFIRMATION_PHRASE
      || currentRestorePendingRef.current
    ) return;
    const pending = commitCurrentRestorePending({
      submissionId: createUpdateApplySubmissionId(),
      artifactId: dialog.artifact.id,
      createdAt: Date.now(),
    });
    if (!pending) return;
    setCurrentRestoreStatus({
      submission_id: pending.submissionId,
      status: "queued",
      phase: "preflight",
      terminal_result: null,
    });
    setCurrentRestoreDialog((current) => current ? {
      ...current,
      accepted: true,
      reconnecting: false,
      error: "",
    } : current);
    try {
      const receipt = await apiFetch("/system/restore/current/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          artifact_id: pending.artifactId,
          submission_id: pending.submissionId,
          confirm: true,
          confirmation_phrase: CURRENT_RESTORE_CONFIRMATION_PHRASE,
        }),
      });
      const restoreStatus = receipt?.restore_status || null;
      if (restoreStatus) setCurrentRestoreStatus(restoreStatus);
      void pollCurrentRestoreStatus(pending);
    } catch (error) {
      if (updateApplyRequestIsAmbiguous(error)) {
        setCurrentRestoreDialog((current) => current ? {
          ...current,
          accepted: true,
          reconnecting: true,
          error: "",
        } : current);
        void pollCurrentRestoreStatus(pending);
      } else {
        commitCurrentRestorePending(null);
        setCurrentRestoreStatus(null);
        setCurrentRestoreDialog((current) => current ? {
          ...current,
          accepted: false,
          reconnecting: false,
          error: currentRestoreReasonText(currentRestoreReasonCode(error)),
        } : current);
      }
    }
  }

  function currentRestoreDialogContent() {
    if (!currentRestoreDialog) return null;
    const status = currentRestoreStatus;
    const terminal = currentRestoreTerminal(status);
    const phase = String(status?.phase || "preflight");
    const phaseOrder = CURRENT_RESTORE_OPERATIONAL_PHASES;
    const activeIndex = phaseOrder.indexOf(phase);
    const failedPhase = terminal
      ? currentRestoreFailedPhase(status)
      : "";
    const failedIndex = phaseOrder.indexOf(failedPhase);
    const terminalLabel = terminal
      ? t.maintenanceCurrentRestorePhases?.[status.terminal_result]
      : t.maintenanceCurrentRestoreTerminalPhase;
    return (
      <div className="settingsCurrentRestoreContent">
        {currentRestoreDialog.preflightBusy ? (
          <div className="settingsCurrentRestorePreflight" role="status">
            <span aria-hidden="true" />
            {t.maintenanceCurrentRestorePreflight}
          </div>
        ) : null}
        {currentRestoreDialog.accepted ? (
          <ol className="settingsCurrentRestoreTimeline">
            {phaseOrder.map((item, index) => {
              const state = terminal
                ? status?.terminal_result === "completed"
                  ? "complete"
                  : status?.terminal_result === "failed_rolled_back"
                    ? index === failedIndex
                      ? "failed"
                      : "complete"
                    : failedIndex < 0
                      ? "pending"
                      : index < failedIndex
                        ? "complete"
                        : index === failedIndex
                          ? "failed"
                          : "pending"
                : index < activeIndex
                  ? "complete"
                  : index === activeIndex
                    ? "active"
                    : "pending";
              return (
                <li className={`is-${state}`} key={item}>
                  <span aria-hidden="true">
                    {state === "complete"
                      ? "✓"
                      : state === "failed"
                        ? "!"
                        : index + 1}
                  </span>
                  {t.maintenanceCurrentRestorePhases?.[item]}
                </li>
              );
            })}
            {(() => {
              const resultState = !terminal
                ? "pending"
                : status?.terminal_result === "completed"
                  ? "complete"
                  : status?.terminal_result === "failed_rolled_back"
                    ? "rolled-back"
                    : "failed";
              return (
                <li className={`is-${resultState}`}>
                  <span aria-hidden="true">
                    {resultState === "complete"
                      ? "✓"
                      : resultState === "rolled-back"
                        ? "↩"
                        : resultState === "failed"
                          ? "!"
                          : "7"}
                  </span>
                  {terminalLabel}
                </li>
              );
            })()}
          </ol>
        ) : (
          <label className="settingsCurrentRestorePhrase">
            <span>{t.maintenanceCurrentRestorePhraseLabel}</span>
            <input
              className="input"
              value={currentRestoreDialog.phrase}
              onChange={(event) => setCurrentRestoreDialog((current) => current ? {
                ...current,
                phrase: event.target.value,
                error: "",
              } : current)}
              autoComplete="off"
              spellCheck="false"
              disabled={currentRestoreDialog.preflightBusy}
            />
            {currentRestoreDialog.phrase
              && currentRestoreDialog.phrase !== CURRENT_RESTORE_CONFIRMATION_PHRASE
              ? <small>{t.maintenanceCurrentRestorePhraseMismatch}</small>
              : null}
          </label>
        )}
        {currentRestoreDialog.reconnecting && !terminal ? (
          <div className="settingsCurrentRestoreReconnect">{t.maintenanceCurrentRestoreReconnect}</div>
        ) : null}
        {terminal ? (
          <div
            className={`settingsCurrentRestoreTerminal is-${status.terminal_result}`}
            role={status.terminal_result === "completed" ? "status" : "alert"}
          >
            {currentRestoreTerminalText(status)}
          </div>
        ) : null}
        {currentRestoreDialog.error ? (
          <div className="settingsCurrentRestoreError" role="alert">
            {currentRestoreDialog.error}
          </div>
        ) : null}
      </div>
    );
  }

  function backupOperationFallback(kind) {
    if (kind === "create") return t.maintenanceBackupCreateFailed;
    if (kind === "delete") return t.maintenanceBackupDeleteFailed;
    return t.maintenanceBackupCheckStatuses?.check_failed || t.maintenanceLoadError;
  }

  function backupOperationSuccess(kind) {
    if (kind === "create") return t.maintenanceBackupCreated;
    if (kind === "delete") return t.maintenanceBackupDeleted;
    return t.maintenanceBackupCheckStatuses?.validated || t.maintenanceDryRunResult;
  }

  async function acceptBackupOperationReceipt(receipt, pendingRecord, { recovered = false } = {}) {
    const state = String(receipt?.state || "");
    const kind = String(receipt?.kind || pendingRecord?.kind || "check");
    const terminal = ["completed", "failed", "interrupted"].includes(state);
    const resultStatus = receipt?.result?.status || state || "failed";
    const presentationResult = {
      kind,
      status: resultStatus,
      state,
      phase: receipt?.phase || "",
      reason: receipt?.reason_code || "",
      result: receipt?.result || null,
      recovering: recovered,
    };
    const presentation = maintenanceBackupOperationResultText(presentationResult, t);
    setMaintenanceBackupResult(presentationResult);
    if (!terminal) {
      maintenanceBackupRecoveryRef.current = recovered;
      setMaintenanceBusy(`backup-${kind}`);
      return false;
    }
    maintenanceBackupRecoveryRef.current = false;
    commitBackupOperationPending(null);
    setMaintenanceBusy((current) => current.startsWith("backup-") ? "" : current);
    if (presentation.successful) {
      showToast({
        variant: "success",
        title: presentation.title || t.maintenanceBackupOperationLabels?.[kind] || t.maintenanceBackupsTitle,
        text: presentation.text || backupOperationSuccess(kind),
      });
    } else {
      showToast({
        variant: "warning",
        title: presentation.title || t.maintenanceBackupOperationLabels?.[kind] || t.maintenanceBackupsTitle,
        text: presentation.text || backupOperationFallback(kind),
      });
    }
    await refreshMaintenanceBackupProjections({
      clampInvalid: kind === "delete",
    });
    return true;
  }

  async function reconcilePendingBackupOperation() {
    if (maintenanceBackupPollInFlightRef.current) return;
    const pending = sanitizeBackupOperationPending(
      maintenanceBackupPendingRef.current,
      Date.now(),
    );
    if (!pending) {
      if (maintenanceBackupPendingRef.current) commitBackupOperationPending(null);
      return;
    }
    if (
      maintenanceBackupAdmissionRef.current
      === pending.submissionId
    ) return;
    maintenanceBackupPollInFlightRef.current = true;
    try {
      const receipt = await apiFetch(
        `/system/backup/operations/${encodeURIComponent(pending.submissionId)}`,
      );
      await acceptBackupOperationReceipt(receipt, pending, {
        recovered: maintenanceBackupRecoveryRef.current,
      });
    } catch (err) {
      if (Number(err?.status || 0) === 404) {
        if (backupOperationWithinAdmissionGrace(pending, Date.now())) {
          setMaintenanceBusy(`backup-${pending.kind}`);
          setMaintenanceBackupResult({
            kind: pending.kind,
            status: "running",
            state: "running",
            reason: "",
            recovering: maintenanceBackupRecoveryRef.current,
          });
          return;
        }
        commitBackupOperationPending(null);
        setMaintenanceBusy((current) => current.startsWith("backup-") ? "" : current);
        setMaintenanceBackupResult({
          kind: pending.kind,
          status: "failed",
          state: "failed",
          reason: "receipt_not_found",
        });
        showToast({
          variant: "warning",
          title: t.maintenanceBackupOperationLabels?.[pending.kind] || t.maintenanceBackupsTitle,
          text: backupOperationFallback(pending.kind),
        });
      } else {
        maintenanceBackupRecoveryRef.current = true;
        setMaintenanceBusy(`backup-${pending.kind}`);
        setMaintenanceBackupResult({
          kind: pending.kind,
          status: "running",
          state: "running",
          reason: "",
          recovering: true,
        });
      }
    } finally {
      maintenanceBackupPollInFlightRef.current = false;
    }
  }

  function closeUpdateApplyDialog() {
    if (!updateApplyDialogRef.current) return;
    setUpdateApplyDialog(null);
  }

  function reconcilePendingUpdateApply(applyData, observedAtMs) {
    const current = updateApplyPendingRef.current;
    if (!current) return "none";
    const result = reconcileUpdateApplyPending(current, applyData, observedAtMs);
    if (result.outcome === "accepted") {
      commitUpdateApplyPending(null);
      setUpdateApplyDialog(null);
      setMaintenanceBusy((value) => value === "update-apply" ? "" : value);
      showToast({ variant: "success", title: t.updateApplyTitle, text: t.updateApplyQueued });
      return result.outcome;
    }
    if (result.outcome === "conflict") {
      setMaintenanceBusy((value) => value === "update-apply" ? "" : value);
      setMaintenanceActionResult({
        flowKey: "update",
        status: "blocked",
        reason: "update_launch_conflict",
        displayReason: t.updateApplyLaunchConflict,
      });
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyLaunchConflict });
      return result.outcome;
    }
    if (result.outcome === "not_accepted") {
      commitUpdateApplyPending(null);
      setMaintenanceBusy((value) => value === "update-apply" ? "" : value);
      setMaintenanceActionResult({
        flowKey: "update",
        status: "blocked",
        reason: "update_launch_not_accepted",
        displayReason: t.updateApplyLaunchNotAccepted,
      });
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyLaunchNotAccepted });
      return result.outcome;
    }
    return result.outcome;
  }

  async function refreshMaintenanceSurface() {
    const tasks = [
      loadMaintenanceOverview(),
      loadUpdateApplySurface({ silent: true }),
    ];
    if (maintenanceBackupDetailOpen || maintenanceBackupDetailRef.current) {
      tasks.push(loadMaintenanceBackupPage(
        maintenanceBackupDetailRef.current?.offset || 0,
        { allowClamp: true, silent: true },
      ));
    }
    await Promise.allSettled(tasks);
  }

  async function loadMaintenanceOverview() {
    if (!canManageMaintenance) return;
    setMaintenanceLoading(true);
    setMaintenanceError("");
    try {
      const overview = await apiFetch("/system/maintenance/overview");
      setMaintenanceOverview(overview);
    } catch (err) {
      setMaintenanceError(humanErrorText(String(err?.message || ""), t.maintenanceLoadError));
    } finally {
      setMaintenanceLoading(false);
    }
  }

  async function loadMaintenanceBackupPage(
    offset,
    { allowClamp = false, silent = false } = {},
  ) {
    if (!canManageMaintenance) return null;
    const safeOffset = Math.max(0, Number(offset || 0));
    if (!silent) setMaintenanceBusy("backup-page");
    try {
      let backupStatus = await apiFetch(
        `/system/restore/status?offset=${safeOffset}&limit=${MAINTENANCE_BACKUP_PAGE_SIZE}`,
      );
      const validOffset = maintenanceBackupValidOffset(
        backupStatus?.total_count,
        backupStatus?.limit || MAINTENANCE_BACKUP_PAGE_SIZE,
        safeOffset,
      );
      if (allowClamp && validOffset !== safeOffset) {
        backupStatus = await apiFetch(
          `/system/restore/status?offset=${validOffset}&limit=${MAINTENANCE_BACKUP_PAGE_SIZE}`,
        );
      }
      setMaintenanceBackupDetail(backupStatus);
      return backupStatus;
    } catch (err) {
      showToast({
        variant: "warning",
        title: t.maintenanceBackupsTitle,
        text: humanErrorText(String(err?.message || ""), t.maintenanceLoadError),
      });
      return null;
    } finally {
      if (!silent) setMaintenanceBusy("");
    }
  }

  async function refreshMaintenanceBackupProjections({ clampInvalid = false } = {}) {
    const currentOffset = maintenanceBackupDetailRef.current?.offset || 0;
    const tasks = [loadMaintenanceOverview()];
    if (maintenanceBackupDetailOpen || maintenanceBackupDetailRef.current) {
      tasks.push(loadMaintenanceBackupPage(currentOffset, {
        allowClamp: clampInvalid,
        silent: true,
      }));
    }
    await Promise.allSettled(tasks);
  }

  async function openMaintenanceBackupDetail() {
    if (maintenanceBusy) return;
    setMaintenanceBackupDetailOpen(true);
    await loadMaintenanceBackupPage(
      maintenanceBackupDetailRef.current?.offset || 0,
      { allowClamp: true },
    );
  }

  function closeMaintenanceBackupDetail() {
    if (maintenanceBusy || currentRestorePending) return;
    setMaintenanceBackupDetailOpen(false);
  }

  async function loadUpdateApplySurface({ silent = false } = {}) {
    if (!canManageMaintenance || updatePollInFlightRef.current) return null;
    updatePollInFlightRef.current = true;
    if (!silent) setMaintenanceBusy("update-status");
    try {
      const pending = sanitizeUpdateApplyPending(updateApplyPendingRef.current, Date.now());
      const [statusResult, applyResult] = await Promise.allSettled([
        apiFetch("/system/update/status"),
        apiFetch("/system/update/apply/status"),
      ]);
      const observedAtMs = monotonicWallNow();
      setUpdateApplyClockMs(observedAtMs);

      if (statusResult.status === "fulfilled") {
        setUpdateStatus(statusResult.value);
        setUpdateTransportErrors((current) => ({ ...current, update: null }));
      } else {
        setUpdateTransportErrors((current) => ({
          ...current,
          update: safeUpdateTransportError(statusResult.reason, t.updateApplyConnection),
        }));
      }

      if (applyResult.status === "fulfilled") {
        setUpdateApplyStatus(applyResult.value);
        setUpdateApplyReconnectSnapshot(updateApplyReconnectTiming(applyResult.value, observedAtMs));
        setUpdateTransportErrors((current) => ({ ...current, apply: null }));
        if (pending) reconcilePendingUpdateApply(applyResult.value, observedAtMs);
      } else {
        setUpdateTransportErrors((current) => ({
          ...current,
          apply: safeUpdateTransportError(applyResult.reason, t.updateApplyConnection),
        }));
      }
      return { statusResult, applyResult };
    } finally {
      updatePollInFlightRef.current = false;
      if (!silent) setMaintenanceBusy((current) => current === "update-status" ? "" : current);
    }
  }

  async function runMaintenanceDryRun(flowKey, bodyOverride = null) {
    const config = MAINTENANCE_DRY_RUN_ENDPOINTS[flowKey];
    if (!config || maintenanceBusy) return;
    setMaintenanceBusy(flowKey);
    setMaintenanceActionResult(null);
    try {
      const result = await apiFetch(config.path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(bodyOverride || config.body),
      });
      setMaintenanceActionResult({ flowKey, status: result?.status || "ok", reason: result?.reason || result?.blocked_reason || "" });
      if (flowKey === "restore") {
        setMaintenanceBackupResult({ kind: "check", status: result?.status || "ok", reason: result?.reason || result?.blocked_reason || "" });
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

  function requestDbAdoptionApply() {
    if (maintenanceBusy || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "db-adoption-apply",
      title: t.maintenanceDbAdoptionApply,
      text: t.maintenanceDbAdoptionApplyConfirm,
      confirmLabel: t.maintenanceDbAdoptionApply,
      danger: false,
      onConfirm: performDbAdoptionApply,
    });
  }

  async function performDbAdoptionApply() {
    if (maintenanceBusy || currentRestorePending) return;
    setMaintenanceBusy("db-adoption-apply");
    setMaintenanceConfirm(null);
    setMaintenanceActionResult(null);
    try {
      const result = await apiFetch("/system/db-adoption/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      setMaintenanceActionResult({
        flowKey: "db_adoption",
        status: result?.status || "completed",
        reason: result?.reason || "",
      });
      showToast({
        variant: "success",
        title: t.maintenanceDbAdoptionApply,
        text: t.maintenanceDbAdoptionApplied,
      });
      await loadMaintenanceOverview();
    } catch {
      setMaintenanceActionResult({
        flowKey: "db_adoption",
        status: "failed",
        reason: t.maintenanceDbAdoptionApplyFailed,
      });
      showToast({
        variant: "warning",
        title: t.maintenanceDbAdoptionApply,
        text: t.maintenanceDbAdoptionApplyFailed,
      });
    } finally {
      setMaintenanceBusy("");
    }
  }

  async function createMaintenanceBackup() {
    if (maintenanceBusy || maintenanceBackupPendingRef.current || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "backup-create",
      title: t.maintenanceBackupCreate,
      text: t.maintenanceBackupCreateConfirm,
      confirmLabel: t.maintenanceBackupCreateShort,
      danger: false,
      onConfirm: performMaintenanceBackupCreate,
    });
  }

  async function performMaintenanceBackupCreate() {
    await performMaintenanceBackupOperation("create", null);
  }

  function requestCheckMaintenanceBackup(artifact) {
    if (!artifact?.id || maintenanceBusy || maintenanceBackupPendingRef.current || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "backup-check",
      title: t.maintenanceBackupCheck,
      text: t.maintenanceBackupCheckConfirm.replace("{date}", artifact.createdAt || "-"),
      confirmLabel: t.maintenanceBackupCheck,
      danger: false,
      artifact,
      onConfirm: () => performMaintenanceBackupOperation("check", artifact),
    });
  }

  function requestDeleteMaintenanceBackup(artifact) {
    if (!artifact?.id || maintenanceBusy || maintenanceBackupPendingRef.current || currentRestorePending) return;
    setMaintenanceConfirm({
      kind: "backup-delete",
      title: t.maintenanceBackupDelete,
      text: t.maintenanceBackupDeleteConfirm.replace("{date}", artifact.createdAt || "-"),
      confirmLabel: t.maintenanceBackupDelete,
      danger: true,
      artifact,
      onConfirm: () => performMaintenanceBackupOperation("delete", artifact),
    });
  }

  async function performMaintenanceBackupOperation(kind, artifact) {
    if (
      maintenanceBusy
      || maintenanceBackupPendingRef.current
      || currentRestorePending
      || !["create", "check", "delete"].includes(kind)
      || (kind !== "create" && !artifact?.id)
    ) return;
    const submissionId = createUpdateApplySubmissionId();
    const pending = createBackupOperationPending(
      kind,
      artifact?.id || "",
      submissionId,
      Date.now(),
    );
    if (!pending || !commitBackupOperationPending(pending)) {
      showToast({
        variant: "warning",
        title: t.maintenanceBackupOperationLabels?.[kind] || t.maintenanceBackupsTitle,
        text: backupOperationFallback(kind),
      });
      return;
    }
    maintenanceBackupRecoveryRef.current = false;
    setMaintenanceBusy(`backup-${kind}`);
    setMaintenanceConfirm(null);
    setMaintenanceActionResult(null);
    setMaintenanceBackupResult(null);
    let endpoint = "/system/backup/create";
    let body = { confirm: true, submission_id: pending.submissionId };
    if (kind === "check") {
      endpoint = "/system/restore/apply";
      body = { ...body, artifact_id: artifact.id };
    } else if (kind === "delete") {
      endpoint = `/system/restore/artifacts/${encodeURIComponent(artifact.id)}/delete`;
    }
    maintenanceBackupAdmissionRef.current = pending.submissionId;
    try {
      const receipt = await apiFetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (
        maintenanceBackupAdmissionRef.current
        === pending.submissionId
      ) {
        maintenanceBackupAdmissionRef.current = null;
      }
      await acceptBackupOperationReceipt(receipt, pending);
    } catch (error) {
      if (
        maintenanceBackupAdmissionRef.current
        === pending.submissionId
      ) {
        maintenanceBackupAdmissionRef.current = null;
      }
      if (!updateApplyRequestIsAmbiguous(error)) {
        commitBackupOperationPending(null);
        setMaintenanceBackupResult({
          kind,
          status: "failed",
          state: "failed",
          reason: "request_rejected",
        });
        showToast({
          variant: "warning",
          title: t.maintenanceBackupOperationLabels?.[kind]
            || t.maintenanceBackupsTitle,
          text: backupOperationFallback(kind),
        });
        return;
      }
      setMaintenanceBackupResult({
        kind,
        status: "running",
        state: "running",
        recovering: true,
      });
      maintenanceBackupRecoveryRef.current = true;
      await reconcilePendingBackupOperation();
    } finally {
      if (
        maintenanceBackupAdmissionRef.current
        === pending.submissionId
      ) {
        maintenanceBackupAdmissionRef.current = null;
      }
      if (!maintenanceBackupPendingRef.current) {
        setMaintenanceBusy((current) => current === `backup-${kind}` ? "" : current);
      }
    }
  }

  async function runUpdateCheck() {
    if (maintenanceBusy) return;
    setMaintenanceBusy("update");
    setMaintenanceActionResult(null);
    try {
      const result = await apiFetch("/system/update/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      setUpdateStatus(result);
      setUpdateTransportErrors((current) => ({ ...current, update: null }));
      await loadUpdateApplySurface({ silent: true });
      const checkStatus = String(result?.status || "").toLowerCase();
      const checkFailed = ["check_failed", "blocked", "not_configured", "failed"].includes(checkStatus);
      const checkResultKey = checkStatus === "current"
        ? "current"
        : ["update_available", "available"].includes(checkStatus)
          ? "available"
          : checkFailed
            ? "blocked"
            : "unknown";
      const checkStatusText = maintenanceStatusText(result?.status, t);
      showToast({
        variant: checkFailed ? "warning" : "success",
        title: t.updateApplyHeadlines?.[checkResultKey] || checkStatusText,
        text: t.updateApplySummaries?.[checkResultKey] || checkStatusText,
      });
    } catch (err) {
      const transportError = safeUpdateTransportError(err, t.updateApplyUnavailable);
      const message = transportError.message;
      setUpdateTransportErrors((current) => ({ ...current, update: transportError }));
      setMaintenanceActionResult({ flowKey: "update", status: "blocked", reason: message });
      showToast({ variant: "warning", title: t.updateApplyCheck, text: message });
    } finally {
      setMaintenanceBusy("");
    }
  }

  function startUpdateApply() {
    if (maintenanceBusy || updateApplyPendingRef.current) return;
    const candidate = updateApplyCandidateSnapshot(updateStatus);
    if (!candidate.version || !candidate.commit) {
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyUnavailable });
      return;
    }
    setToast(null);
    setUpdateApplyDialog({ phase: "confirm", candidate, error: "", deadlineAtMs: null });
  }

  async function confirmUpdateApply() {
    const dialog = updateApplyDialogRef.current;
    if (!dialog || dialog.phase !== "confirm" || maintenanceBusy || updateApplyPendingRef.current) return;
    const submittedAtMs = Date.now();
    const submissionId = createUpdateApplySubmissionId();
    const pending = createUpdateApplyPending(
      submissionId,
      dialog.candidate,
      submittedAtMs,
    );
    if (!pending) {
      showToast({ variant: "warning", title: t.updateApplyTitle, text: t.updateApplyUnavailable });
      return;
    }
    setUpdateApplyDialog(null);
    setMaintenanceBusy("update-apply");
    setMaintenanceActionResult(null);
    commitUpdateApplyPending(pending);
    try {
      const result = await apiFetch("/system/update/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirm: true,
          submission_id: pending.submissionId,
          expected_manifest_version: dialog.candidate.version,
          expected_manifest_commit: dialog.candidate.commit,
        }),
      });
      setUpdateApplyStatus(result?.apply_status || result);
      setUpdateApplyReconnectSnapshot(updateApplyReconnectTiming(result?.apply_status || result, monotonicWallNow()));
      setUpdateTransportErrors((current) => ({ ...current, apply: null }));
      reconcilePendingUpdateApply(result?.apply_status || result, monotonicWallNow());
      void loadUpdateApplySurface({ silent: true });
    } catch (err) {
      if (updateApplyPendingRef.current && updateApplyRequestIsAmbiguous(err)) {
        void loadUpdateApplySurface({ silent: true });
      } else {
        commitUpdateApplyPending(null);
        const message = safeUpdateLaunchError(err);
        setMaintenanceActionResult({
          flowKey: "update",
          status: "blocked",
          reason: String(err?.code || err?.category || "update_launch_rejected"),
          displayReason: message,
        });
        showToast({ variant: "warning", title: t.updateApplyTitle, text: message });
      }
    } finally {
      setMaintenanceBusy((current) => current === "update-apply" ? "" : current);
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
          try {
            window.sessionStorage.setItem(CREDENTIALS_CHANGED_NOTICE_KEY, "credentials_changed");
          } catch (_) {}
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

  return (
    <Layout>
      <div className="settingsPage">
        <OperationToast toast={toast} onClose={() => setToast(null)} />

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
              <span
                className={`settingsDirtyNote${dirty ? " is-visible" : ""}`}
                aria-hidden={dirty ? undefined : "true"}
              >
                {t.dirty}
              </span>
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
                <div className="settingsRow settingsRowCompactSelect">
                  <div className="settingsRowIcon"><img src={languageIcon} alt="" /></div>
                  <div className="settingsRowText">
                    <label htmlFor="settings-language">{t.language}</label>
                    <span>{t.languageHelp}</span>
                  </div>
                  <div className="settingsRowControl">
                    <LanguageSelect id="settings-language" className="select settingsSelect" value={draft.language} onChange={(nextLanguage) => handleSettingsLanguageChange({ target: { value: nextLanguage } })} disabled={saving} aria-label={t.language} />
                  </div>
                </div>

                <div className="settingsRow settingsRowCompactSelect">
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

                <div className="settingsRow settingsRowCompactSelect">
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

                <div className="settingsRow settingsRowHardware settingsRowCompactSelect">
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
                    <button
                      className="button secondary small appIllustratedAction settingsUsersAddButton"
                      onClick={() => setSecurityModalOpen(true)}
                      title={`${t.open}: ${t.security}`}
                      aria-label={`${t.open}: ${t.security}`}
                    >
                      <img src="/assets/icons/ui/open.png" alt="" aria-hidden="true" />
                    </button>
                  </div>
                </div>

                <div className="settingsRow">
                  <div className="settingsRowIcon"><img src="/assets/icons/ui/settings.png" alt="" /></div>
                  <div className="settingsRowText">
                    <label>{t.maintenance}</label>
                    <span>{t.maintenanceText}</span>
                  </div>
                  <div className="settingsRowControl settingsRowControlMeta">
                    <button
                      ref={maintenanceTriggerRef}
                      className="button secondary small appIllustratedAction settingsUsersAddButton"
                      onClick={openMaintenanceModal}
                      disabled={!canManageMaintenance}
                      title={`${t.open}: ${t.maintenance}`}
                      aria-label={`${t.open}: ${t.maintenance}`}
                    >
                      <img src="/assets/icons/ui/open.png" alt="" aria-hidden="true" />
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
                    <button
                      className="button secondary small appIllustratedAction settingsUsersAddButton"
                      onClick={openUsersModal}
                      disabled={!canManageUsers || usersLoading || userBusy}
                      title={`${t.open}: ${t.users}`}
                      aria-label={`${t.open}: ${t.users}`}
                    >
                      <img src="/assets/icons/ui/open.png" alt="" aria-hidden="true" />
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
                            <button className="settingsUserIconButton" onClick={() => openEditUser(user)} disabled={userBusy || !(userCanBeManaged(currentUser, user) || user.id === currentUser?.id)} title={t.edit} aria-label={t.edit}>
                              <EditIcon />
                            </button>
                            <button className="settingsUserIconButton" onClick={() => toggleUserActive(user)} disabled={userBusy || !userCanBeManaged(currentUser, user) || user.id === currentUser?.id || user.role === "owner"} title={user.is_active ? t.deactivate : t.activate} aria-label={user.is_active ? t.deactivate : t.activate}>
                              {user.is_active ? <PowerIcon /> : <CheckIcon />}
                            </button>
                            <button className="settingsUserIconButton danger" onClick={() => requestDeleteUser(user)} disabled={userBusy || !userCanBeDeleted(currentUser, user, users)} title={t.delete} aria-label={t.delete}>
                              <TrashIcon />
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

        <OperationDialog
          dialog={userDeleteTarget ? {
            id: `user-delete-${userDeleteTarget.id}`,
            presentation: "compact-confirmation",
            title: lang === "en" ? "Delete user" : "Удалить пользователя",
            message: `${userDeleteTarget.username}?`,
            busy: userBusy,
            dismissible: !userBusy,
            cancelLabel: t.cancel,
            closeLabel: t.close,
            confirmLabel: userBusy
              ? (lang === "en" ? "Deleting..." : "Удаляем...")
              : (lang === "en" ? "Delete" : "Удалить"),
            confirmTone: "danger",
            tone: "error",
            onConfirm: () => deleteUser(userDeleteTarget),
          } : null}
          onClose={() => setUserDeleteTarget(null)}
        />

        <OperationDialog
          dialog={maintenanceConfirm ? {
            id: "maintenance-confirm",
            presentation: "compact-confirmation",
            title: maintenanceConfirm.title,
            message: maintenanceConfirm.text,
            busy: Boolean(maintenanceBusy),
            dismissible: !maintenanceBusy,
            cancelLabel: t.cancel,
            closeLabel: t.close,
            confirmLabel: maintenanceBusy === "backup-delete"
              ? t.maintenanceBackupDeleting
              : maintenanceBusy === "backup-check"
                ? t.maintenanceBackupChecking
                : maintenanceBusy === "backup-create"
                  ? t.maintenanceBackupCreating
                  : maintenanceBusy === "db-adoption-apply"
                    ? t.saving
                    : maintenanceConfirm.confirmLabel,
            confirmTone: maintenanceConfirm.danger ? "danger" : undefined,
            tone: maintenanceConfirm.danger ? "error" : "warning",
            onConfirm: maintenanceConfirm.onConfirm,
          } : null}
          onClose={() => setMaintenanceConfirm(null)}
        />

        <OperationDialog
          dialog={currentRestoreDialog ? {
            id: `current-db-restore-${currentRestoreDialog.artifact?.id || "current"}`,
            title: t.maintenanceCurrentRestoreTitle,
            message: t.maintenanceCurrentRestoreIntro.replace(
              "{date}",
              currentRestoreDialog.artifact?.createdAt || "-",
            ),
            overlayClassName: "settingsCurrentRestoreDialogOverlay",
            className: "settingsCurrentRestoreDialog",
            tone: currentRestoreStatus?.terminal_result === "completed"
              ? "success"
              : currentRestoreStatus?.terminal_result === "failed_recovery_required"
                ? "error"
                : "warning",
            items: [
              t.maintenanceCurrentRestoreChanges,
              t.maintenanceCurrentRestoreVideoSafe,
              t.maintenanceCurrentRestoreBackupFirst,
              t.maintenanceCurrentRestoreInterruption,
              t.maintenanceCurrentRestoreActor,
            ],
            content: currentRestoreDialogContent(),
            busy: Boolean(currentRestoreDialog.accepted && !currentRestoreTerminal()),
            dismissible: !currentRestoreDialog.accepted || currentRestoreTerminal(),
            closeLabel: t.maintenanceCurrentRestoreClose,
            ...(!currentRestoreDialog.accepted ? {
              cancelLabel: t.maintenanceCurrentRestoreCancel,
              confirmLabel: t.maintenanceCurrentRestoreConfirm,
              confirmTone: "danger",
              confirmDisabled: Boolean(
                currentRestoreDialog.preflightBusy
                || currentRestoreDialog.preflight?.can_restore !== true
                || currentRestoreDialog.phrase !== CURRENT_RESTORE_CONFIRMATION_PHRASE
              ),
              onConfirm: confirmCurrentDatabaseRestore,
            } : {}),
          } : null}
          onClose={closeCurrentRestoreDialog}
        />

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
            <div
              ref={maintenanceDialogRef}
              className="settingsMaintenanceModal"
              role="dialog"
              tabIndex={-1}
              aria-modal="true"
              aria-label={t.maintenanceOverview}
              aria-hidden={maintenanceChildDialogOpen ? "true" : undefined}
              inert={maintenanceChildDialogOpen ? true : undefined}
            >
              <div className="settingsMaintenanceModalHeader">
                <h2>{t.maintenanceOverview}</h2>
                <div className="settingsMaintenanceModalActions">
                  <button
                    type="button"
                    className="settingsMaintenanceIconButton"
                    onClick={refreshMaintenanceSurface}
                    disabled={maintenanceLoading || Boolean(maintenanceBusy)}
                    title={t.maintenanceRefresh}
                    aria-label={t.maintenanceRefresh}
                  >
                    ↻
                  </button>
                  <button type="button" className="settingsMaintenanceIconButton" onClick={closeMaintenanceModal} disabled={maintenanceChildDialogOpen} aria-label={t.close}>×</button>
                </div>
              </div>

              {maintenanceError ? <div className="settingsJournalEmpty error">{maintenanceError}</div> : null}
              {maintenanceLoading && !maintenanceOverview && !updateStatus && !updateApplyStatus ? <div className="settingsJournalEmpty">{t.checking}</div> : null}

              {maintenanceOverview || updateStatus || updateApplyStatus ? (
                <div className="settingsMaintenanceContent">
                  {!maintenanceBackupDetailOpen ? (
                    <>
                  <section className={`settingsMaintenanceOverall is-${maintenanceOverall.tone}`}>
                    <span className="settingsMaintenanceOverallIcon" aria-hidden="true">{maintenanceOverall.icon}</span>
                    <div>
                      <strong>{maintenanceOverall.title}</strong>
                      <p>{maintenanceOverall.summary}</p>
                    </div>
                  </section>

                  <section className="settingsUpdateApplyPanel">
                    <div className="settingsMaintenanceCardHeading settingsMaintenanceCardHeadingAligned">
                      <h3>{t.updateApplyTitle}</h3>
                      <span className={`settingsMaintenancePill is-${updateApplyOperator.severity}`}>
                        {updateApplyOperator.headline}
                      </span>
                    </div>
                    <div className="settingsUpdateApplyCompact">
                      <div className="settingsUpdateApplyRelease">
                        <dl>
                          <div>
                            <dt>{t.maintenanceLabels.current}</dt>
                            <dd>{updateApplyOperator.currentVersion}</dd>
                          </div>
                          <div>
                            <dt>{t.maintenanceLastUpdate}</dt>
                            <dd>{updateApplyOperator.installedAt}</dd>
                          </div>
                        </dl>
                        <p>{updateApplyOperator.releaseTitle}</p>
                      </div>
                      <div className="settingsUpdateApplyActions">
                        <button
                          type="button"
                          className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                          onClick={runUpdateCheck}
                          disabled={Boolean(maintenanceBusy)}
                          title={maintenanceBusy === "update" ? t.checking : t.updateApplyCheck}
                          aria-label={maintenanceBusy === "update" ? t.checking : t.updateApplyCheck}
                          aria-busy={maintenanceBusy === "update" ? "true" : undefined}
                        >
                          <img src="/assets/icons/ui/update-check.svg" alt="" aria-hidden="true" />
                        </button>
                        {updateApplyOperator.showApplyButton ? (
                          <button type="button" className="button primary small" onClick={startUpdateApply} disabled={!updateApplyAllowed}>
                            {maintenanceBusy === "update-apply" || updateApplyRunning || updateApplyPending || updateApplyOperator.stateUnknown
                              ? updateApplyPrimaryText
                              : t.updateApplyStart}
                          </button>
                        ) : null}
                      </div>
                    </div>

                    {updateApplyRunning || updateApplyOperator.stateUnknown || updateApplyOperator.reconnecting || updateApplyOperator.severity === "blocked" ? (
                      <>
                        <p className={`settingsUpdateApplyCompactMessage is-${updateApplyOperator.severity}`}>
                          {updateApplyOperator.summary}
                        </p>
                        <div className="settingsUpdateApplyTimeline" aria-label={t.updateApplyProgress}>
                          <ol>
                            {updateApplyOperator.timeline.map((step) => (
                              <li className={`is-${step.status}`} key={step.name}>
                                <span className="settingsUpdateApplyTimelineDot" aria-hidden="true">
                                  {step.icon === "alert" ? "!" : step.icon === "pulse" ? "•" : step.icon === "check" ? "✓" : ""}
                                </span>
                                <strong>{step.label}</strong>
                                {step.timeLabel ? <small>{step.timeLabel}</small> : null}
                              </li>
                            ))}
                          </ol>
                        </div>
                      </>
                    ) : null}

                    {(updateApplyOperator.canApply || updateApplyRunning || updateApplyOperator.severity === "blocked")
                      && (updateApplyOperator.releaseSummary || updateApplyOperator.releaseChangelog.length) ? (
                      <div className="settingsUpdateApplySummaryGrid">
                        <section>
                        <span>{t.updateApplyReleaseChanges}</span>
                        {updateApplyOperator.releaseSummary ? (
                          <p>{updateApplyOperator.releaseSummary}</p>
                        ) : null}
                        {updateApplyOperator.releaseChangelog.length ? (
                          <ul>
                            {updateApplyOperator.releaseChangelog.map((item, index) => (
                              <li key={`${index}-${item}`}>{item}</li>
                            ))}
                          </ul>
                        ) : null}
                        </section>
                      </div>
                    ) : null}

                    {updateApplyLaunchNotice ? <div className="settingsUpdateApplyNotice">{updateApplyLaunchNotice}</div> : null}
                    {updateTransportErrors.update && !updateTransportErrors.apply ? (
                      <div className="settingsUpdateApplyNotice">{t.updateApplyPeerCheckUnavailable}</div>
                    ) : null}
                    {updateApplyErrors.map((message) => <small className="settingsUpdateApplyError" key={message}>{message}</small>)}
                  </section>

                  <div className="settingsMaintenanceCoreGrid">
                    <section className={`settingsMaintenanceCoreCard is-${maintenanceDatabase.tone}`}>
                      <div className="settingsMaintenanceCardHeading">
                        <h3>{t.maintenanceDatabaseTitle}</h3>
                        <span className={`settingsMaintenancePill is-${maintenanceDatabase.tone}`}>
                          {maintenanceDatabase.statusLabel}
                        </span>
                      </div>
                      {maintenanceDatabase.facts.length ? (
                        <dl className="settingsMaintenanceCoreFacts">
                          {maintenanceDatabase.facts.map(([label, value]) => (
                            <div key={label}>
                              <dt>{label}</dt>
                              <dd>{String(value)}</dd>
                            </div>
                          ))}
                        </dl>
                      ) : null}
                      <p>{maintenanceDatabase.summary}</p>
                      {maintenanceDatabase.tone !== "ok" && maintenanceDatabase.action ? (
                        <small>{maintenanceDatabase.action}</small>
                      ) : null}
                      {maintenanceDatabase.actionableRow?.showCheck ? (
                        <button
                          type="button"
                          className="button secondary small settingsMaintenanceCoreAction"
                          onClick={() => runMaintenanceDryRun(maintenanceDatabase.actionableRow.key)}
                          disabled={Boolean(maintenanceBusy)}
                        >
                          {maintenanceBusy === maintenanceDatabase.actionableRow.key
                            ? t.checking
                            : maintenanceDatabase.actionableRow.checkLabel}
                        </button>
                      ) : null}
                      {maintenanceDatabase.actionableRow?.showApply ? (
                        <button
                          type="button"
                          className="button primary small settingsMaintenanceCoreAction"
                          onClick={requestDbAdoptionApply}
                          disabled={Boolean(maintenanceBusy) || Boolean(currentRestorePending)}
                        >
                          {maintenanceBusy === "db-adoption-apply"
                            ? t.saving
                            : maintenanceDatabase.actionableRow.applyLabel}
                        </button>
                      ) : null}
                    </section>

                  <section className={`settingsMaintenanceBackupManager settingsMaintenanceCoreCard is-${maintenanceBackupOverview.tone}`}>
                    <div className="settingsMaintenanceBackupHead">
                      <div className="settingsMaintenanceCardHeading settingsMaintenanceCardHeadingAligned">
                        <h3>{t.maintenanceBackupsTitle}</h3>
                        <span className={`settingsMaintenancePill is-${maintenanceBackupOverview.tone}`}>
                          {maintenanceBackupOverview.statusText}
                        </span>
                      </div>
                    </div>
                    <p className="settingsMaintenanceBackupTotals">
                      {maintenanceBackupOverview.countText} · {maintenanceBackupOverview.totalBytesText}
                    </p>
                    <div className="settingsMaintenanceBackupLatestRow">
                      <div className="settingsMaintenanceBackupLatest">
                        <span>{t.maintenanceBackupLatest}: {maintenanceBackupOverview.latestCreatedAt}</span>
                        {maintenanceBackupOverview.latestArtifact ? (
                          <span>
                            {maintenanceBackupOverview.latestArtifact.availabilityLabel}
                            {" · "}
                            {maintenanceBackupOverview.latestArtifact.integrityLabel}
                            {" · "}
                            {maintenanceBackupOverview.latestArtifact.compatibilityLabel}
                          </span>
                        ) : (
                          <span>{t.maintenanceBackupNoCopies}</span>
                        )}
                      </div>
                      <div className="settingsMaintenanceBackupActions">
                        <button
                          type="button"
                          className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                          onClick={createMaintenanceBackup}
                          disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending)}
                          title={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                          aria-label={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                          aria-busy={maintenanceBusy === "backup-create" ? "true" : undefined}
                        >
                          <img src="/assets/icons/ui/backup-create.svg" alt="" aria-hidden="true" />
                        </button>
                        <button
                          type="button"
                          className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                          onClick={openMaintenanceBackupDetail}
                          disabled={Boolean(maintenanceBusy)}
                          title={t.maintenanceBackupOpenList}
                          aria-label={t.maintenanceBackupOpenList}
                        >
                          <img src="/assets/icons/ui/open.png" alt="" aria-hidden="true" />
                        </button>
                      </div>
                    </div>
                    {maintenanceBackupPending || maintenanceBackupResult?.recovering ? (
                      <div className="settingsMaintenanceBackupPending" role="status">{maintenanceBackupProgressText}</div>
                    ) : null}
                  </section>
                  </div>

                  <section className="settingsMaintenanceSupport">
                    <div className="settingsMaintenanceSupportMain">
                      <div className="settingsMaintenanceSupportCopy">
                        <div className="settingsMaintenanceCardHeading settingsMaintenanceCardHeadingAligned">
                          <h3>{t.maintenanceSupportTitle}</h3>
                          <span className={`settingsMaintenancePill ${maintenanceWarnings.groups.actionable ? "is-warning" : "is-ok"}`}>
                            {maintenanceWarnings.groups.actionable
                              ? `${t.maintenanceWarningActionable}: ${maintenanceWarnings.groups.actionable}`
                              : t.maintenanceSupportStatusOk}
                          </span>
                        </div>
                        <p>{t.maintenanceSupportText}</p>
                      </div>
                    </div>
                    <div className="settingsMaintenanceSupportActions">
                      <button
                        type="button"
                        className="button secondary small appIllustratedAction settingsMaintenanceActionIcon settingsMaintenanceSupportActionButton"
                        onClick={() => setDiagnosticChoiceOpen(true)}
                        disabled={Boolean(maintenanceBusy) || securityBusy}
                        title={t.maintenanceReportDownload}
                        aria-label={t.maintenanceReportDownload}
                      >
                        <img src="/assets/icons/ui/download-report.svg" alt="" aria-hidden="true" />
                      </button>
                    </div>
                    {maintenanceWarnings.groups.actionable ? (
                      <div className="settingsMaintenanceWarningsList">
                        {maintenanceWarnings.items
                          .filter((item) => item.classification === "actionable")
                          .slice(0, 3)
                          .map((item) => (
                          <article className={`is-${item.classification}`} key={`${item.code}-${item.title}`}>
                            <strong>{item.title}</strong>
                            <p>{item.summary}</p>
                            <small>{item.action}</small>
                          </article>
                          ))}
                      </div>
                    ) : null}
                  </section>
                    </>
                  ) : (
                    <section className="settingsMaintenanceBackupDetail">
                      <div className="settingsMaintenanceBackupDetailHeader">
                        <button
                          type="button"
                          className="settingsMaintenanceBackButton"
                          onClick={closeMaintenanceBackupDetail}
                          disabled={Boolean(maintenanceBusy) || Boolean(currentRestorePending)}
                        >
                          <span aria-hidden="true">←</span>
                          {t.maintenanceBackupBackToOverview}
                        </button>
                        <div>
                          <h3>{t.maintenanceBackupsTitle}</h3>
                          <p>{maintenanceBackupManager.statusText} · {t.maintenanceBackupTotalSize}: {maintenanceBackupManager.totalBytesText}</p>
                        </div>
                        <button
                          type="button"
                          className="button secondary small appIllustratedAction settingsMaintenanceActionIcon"
                          onClick={createMaintenanceBackup}
                          disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending)}
                          title={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                          aria-label={maintenanceBusy === "backup-create" ? t.maintenanceBackupCreating : t.maintenanceBackupCreate}
                          aria-busy={maintenanceBusy === "backup-create" ? "true" : undefined}
                        >
                          <img src="/assets/icons/ui/backup-create.svg" alt="" aria-hidden="true" />
                        </button>
                      </div>
                      <p className="settingsMaintenanceBackupScope">{t.maintenanceBackupScope}</p>
                      {maintenanceBackupPending || maintenanceBackupResult?.recovering ? (
                        <div className="settingsMaintenanceBackupPending" role="status">{maintenanceBackupProgressText}</div>
                      ) : null}
                      {maintenanceBusy === "backup-page" && !maintenanceBackupDetail ? (
                        <div className="settingsJournalEmpty">{t.checking}</div>
                      ) : maintenanceBackupManager.artifacts.length ? (
                        <div className="settingsMaintenanceBackupList">
                          <div className="settingsMaintenanceBackupListHead">
                            <span>{t.maintenanceBackupList}</span>
                            <span>
                              {t.maintenanceBackupPage
                                .replace("{start}", String(maintenanceBackupManager.pageStart))
                                .replace("{end}", String(maintenanceBackupManager.pageEnd))
                                .replace("{total}", String(maintenanceBackupManager.totalCount))}
                            </span>
                          </div>
                          {maintenanceBackupManager.artifacts.map((artifact) => (
                            <article className={`settingsMaintenanceBackupItem ${artifact.hasProblem ? "is-problem" : ""}`} key={artifact.id}>
                              <div className="settingsMaintenanceBackupItemBody">
                                <div className="settingsMaintenanceBackupItemHead">
                                  <span className="settingsMaintenanceBackupCreatedAt">{artifact.createdAt}</span>
                                  <span className="settingsMaintenanceBackupMeta">{t.maintenanceBackupSize}: {artifact.size} · {t.maintenanceBackupSchema}: {artifact.schema} · {artifact.backend}</span>
                                </div>
                                <div className="settingsMaintenanceBackupDetailRow">
                                  <div className="settingsMaintenanceBackupStatusGrid">
                                    <div>
                                      <span>{t.maintenanceBackupAvailability}</span>
                                      <MaintenanceBackupDimensionStatus tone={artifact.availabilityTone} label={artifact.availabilityLabel} />
                                    </div>
                                    <div>
                                      <span>{t.maintenanceBackupIntegrity}</span>
                                      <MaintenanceBackupDimensionStatus tone={artifact.integrityTone} label={artifact.integrityLabel} />
                                    </div>
                                    <div>
                                      <span>{t.maintenanceBackupCompatibility}</span>
                                      <MaintenanceBackupDimensionStatus tone={artifact.compatibilityTone} label={artifact.compatibilityLabel} />
                                    </div>
                                    <div>
                                      <span>{t.maintenanceBackupValidation}</span>
                                      <MaintenanceBackupDimensionStatus tone={artifact.validationTone} label={artifact.validationLabel} />
                                    </div>
                                  </div>
                                  <div className="settingsMaintenanceBackupItemActions">
                                    <span
                                      className="settingsMaintenanceIconAction"
                                      title={artifact.canRestore
                                        ? t.maintenanceCurrentRestoreAction
                                        : currentRestoreReasonText(artifact.restoreIneligibleReason)}
                                    >
                                      <button
                                        type="button"
                                        className="settingsMaintenanceMiniButton"
                                        onClick={() => requestCurrentDatabaseRestore(artifact)}
                                        disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending) || !artifact.canRestore}
                                        aria-label={artifact.canRestore
                                          ? t.maintenanceCurrentRestoreAction
                                          : `${t.maintenanceCurrentRestoreAction}: ${currentRestoreReasonText(artifact.restoreIneligibleReason)}`}
                                      >
                                        <MaintenanceRestoreIcon />
                                      </button>
                                    </span>
                                    <span className="settingsMaintenanceIconAction" title={maintenanceBusy === "backup-check" ? t.maintenanceBackupChecking : t.maintenanceBackupCheck}>
                                      <button
                                        type="button"
                                        className="settingsMaintenanceMiniButton"
                                        onClick={() => requestCheckMaintenanceBackup(artifact)}
                                        disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending) || !artifact.canCheck}
                                        aria-label={maintenanceBusy === "backup-check" ? t.maintenanceBackupChecking : t.maintenanceBackupCheck}
                                        aria-busy={maintenanceBusy === "backup-check" ? "true" : undefined}
                                      >
                                        <MaintenanceCheckIcon />
                                      </button>
                                    </span>
                                    <span className="settingsMaintenanceIconAction" title={maintenanceBusy === "backup-delete" ? t.maintenanceBackupDeleting : t.maintenanceBackupDelete}>
                                      <button
                                        type="button"
                                        className="settingsMaintenanceMiniButton danger"
                                        onClick={() => requestDeleteMaintenanceBackup(artifact)}
                                        disabled={Boolean(maintenanceBusy) || Boolean(maintenanceBackupPending) || Boolean(currentRestorePending) || !artifact.deletable}
                                        aria-label={maintenanceBusy === "backup-delete" ? t.maintenanceBackupDeleting : t.maintenanceBackupDelete}
                                        aria-busy={maintenanceBusy === "backup-delete" ? "true" : undefined}
                                      >
                                        <MaintenanceTrashIcon />
                                      </button>
                                    </span>
                                  </div>
                                </div>
                                {artifact.checkedAt || artifact.validatedAt ? (
                                  <small className="settingsMaintenanceBackupEvidenceTime">
                                    {artifact.checkedAt ? t.maintenanceBackupCheckedAt.replace("{date}", artifact.checkedAt) : ""}
                                    {artifact.checkedAt && artifact.validatedAt ? " · " : ""}
                                    {artifact.validatedAt ? t.maintenanceBackupValidatedAt.replace("{date}", artifact.validatedAt) : ""}
                                  </small>
                                ) : null}
                              </div>
                            </article>
                          ))}
                          <div className="settingsMaintenanceBackupPagination">
                            <span className="settingsMaintenanceIconAction" title={t.maintenanceBackupPrevious}>
                              <button
                                type="button"
                                className="settingsMaintenanceMiniButton"
                                onClick={() => loadMaintenanceBackupPage(Math.max(0, maintenanceBackupManager.offset - maintenanceBackupManager.limit))}
                                disabled={Boolean(maintenanceBusy) || !maintenanceBackupManager.hasPrevious}
                                aria-label={t.maintenanceBackupPrevious}
                              >
                                <span aria-hidden="true">←</span>
                              </button>
                            </span>
                            <span className="settingsMaintenanceIconAction" title={t.maintenanceBackupNext}>
                              <button
                                type="button"
                                className="settingsMaintenanceMiniButton"
                                onClick={() => loadMaintenanceBackupPage(maintenanceBackupManager.offset + maintenanceBackupManager.limit)}
                                disabled={Boolean(maintenanceBusy) || !maintenanceBackupManager.hasMore}
                                aria-label={t.maintenanceBackupNext}
                              >
                                <span aria-hidden="true">→</span>
                              </button>
                            </span>
                          </div>
                        </div>
                      ) : <div className="settingsJournalEmpty">{t.maintenanceBackupStatusEmpty}</div>}
                      {maintenanceBackupResultModel ? (
                        <small className="settingsMaintenanceBackupResult">
                          {maintenanceBackupResultModel.label}: {maintenanceBackupResultModel.text}
                        </small>
                      ) : null}
                    </section>
                  )}

                  {maintenanceActionResult ? (
                    <div className={`settingsMaintenanceResult ${maintenanceStatusClass(maintenanceActionResult.status)}`}>
                      <strong>{t.maintenanceFlows?.[maintenanceActionResult.flowKey] || maintenanceActionResult.flowKey}: {maintenanceStatusText(maintenanceActionResult.status, t)}</strong>
                      {maintenanceActionResult.reason ? (
                        <span>{maintenanceActionResult.displayReason || formatMaintenanceMessage(maintenanceActionResult.reason, t, lang, "action")}</span>
                      ) : null}
                    </div>
                  ) : null}

                </div>
              ) : null}
            </div>
          </div>
        ) : null}

        <OperationDialog
          dialog={updateApplyDialog ? {
            id: "update-apply-confirm",
            presentation: "compact-confirmation",
            title: t.updateApplyModalTitle,
            message: t.updateApplyConfirm,
            overlayClassName: "settingsUpdateApplyDialogOverlay",
            tone: "warning",
            closeLabel: t.close,
            cancelLabel: t.cancel,
            confirmLabel: t.updateApplyModalConfirm,
            onConfirm: confirmUpdateApply,
          } : null}
          onClose={closeUpdateApplyDialog}
        />

        <OperationDialog
          dialog={diagnosticChoiceOpen ? {
            id: "diagnostic-archive-choice",
            presentation: "neutral-choice",
            tone: "neutral",
            title: t.diagnosticArchiveQuestion,
            message: t.diagnosticArchiveMessage,
            overlayClassName: "settingsDiagnosticDialogOverlay",
            descriptions: [
              { label: t.diagnosticArchiveNormal, value: t.diagnosticArchiveNormalDescription },
              { label: t.diagnosticArchiveExtended, value: t.diagnosticArchiveExtendedDescription },
            ],
            busy: securityBusy,
            dismissible: !securityBusy,
            closeLabel: t.close,
            showFooterClose: false,
            actions: [
              {
                id: "diagnostic-normal",
                label: t.diagnosticArchiveNormal,
                onClick: () => downloadLogArchive("normal"),
              },
              {
                id: "diagnostic-extended",
                label: t.diagnosticArchiveExtended,
                onClick: () => downloadLogArchive("extended"),
              },
            ],
          } : null}
          onClose={() => setDiagnosticChoiceOpen(false)}
        />

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
                  disabled
                  title={t.reportSendingPending}
                >
                  {t.sendBugReport}
                </button>
              </section>

            </div>
          </div>
        ) : null}
      </div>
    </Layout>
  );
}
