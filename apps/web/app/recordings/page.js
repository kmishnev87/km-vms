"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { OperationDialog, OperationToast } from "../../components/OperationFeedback";
import VideoZoomPanSurface from "../../components/VideoZoomPanSurface";
import { apiFetch, canDeleteRecordings, issueRecordingMediaToken } from "../../lib/api";
import {
  buildArchiveExportPayload,
  canExportRecordings,
  describeArchiveExportLimits,
  downloadArchiveManifest,
  getArchiveExportLimits,
  normalizeArchiveExportError,
  runArchiveExportWorkflow,
  saveBlobDownload,
  validateArchiveExportSelection,
} from "../../lib/archiveExports";
import { useCurrentUser } from "../../lib/currentUser";
import { useI18n } from "../../lib/i18n";
import { resolveEffectiveRecordingCamera } from "../../lib/recordingFilters";
import {
  shouldUseAdaptiveHighResolutionPlayback,
  normalizeVideoDimensions,
  selectCompactVideoRenderMode,
} from "../../lib/playbackResolution";
import { formatProductDateTime, productDateFilterParam, productDateTimeInputValue } from "../../lib/timezone";

const DEFAULT_PAGE_SIZE = 30;
const PAGE_SIZE_OPTIONS = [15, 30, 50, 100];
const DOUBLE_TAP_MS = 330;
const DOUBLE_TAP_DISTANCE_PX = 28;
const TEXT = {
  title: "\u0417\u0430\u043f\u0438\u0441\u0438",
  subtitle: "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440, \u0441\u043a\u0430\u0447\u0438\u0432\u0430\u043d\u0438\u0435 \u0438 \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435 \u0430\u0440\u0445\u0438\u0432\u0430",
  allCameras: "\u0412\u0441\u0435 \u043a\u0430\u043c\u0435\u0440\u044b",
  date: "\u0414\u0430\u0442\u0430",
  refresh: "\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c",
  loading: "\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430...",
  deleteSelected: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435",
  dangerActions: "\u041e\u043f\u0430\u0441\u043d\u044b\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u044f",
  deleteCamera: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u043a\u0430\u043c\u0435\u0440\u044b",
  deleteAll: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438",
  totalFiles: "\u0412\u0441\u0435\u0433\u043e \u0444\u0430\u0439\u043b\u043e\u0432",
  totalSize: "\u041e\u0431\u0449\u0438\u0439 \u043e\u0431\u044a\u0451\u043c",
  page: "\u0421\u0442\u0440\u0430\u043d\u0438\u0446\u0430",
  pageSize: "\u041d\u0430 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0435",
  jumpToPage: "\u041f\u0435\u0440\u0435\u0439\u0442\u0438",
  selected: "\u0412\u044b\u0431\u0440\u0430\u043d\u043e",
  camera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  file: "\u0424\u0430\u0439\u043b",
  createdAt: "\u0414\u0430\u0442\u0430 \u0441\u043e\u0437\u0434\u0430\u043d\u0438\u044f",
  size: "\u0420\u0430\u0437\u043c\u0435\u0440",
  actions: "\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u044f",
  watch: "\u0421\u043c\u043e\u0442\u0440\u0435\u0442\u044c",
  download: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c",
  downloadSource: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0438\u0441\u0445\u043e\u0434\u043d\u0443\u044e \u0437\u0430\u043f\u0438\u0441\u044c",
  createClip: "\u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043a\u043b\u0438\u043f",
  createClipTooltip: "\u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043a\u043b\u0438\u043f \u0437\u0430 \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0438\u0437 \u0430\u0440\u0445\u0438\u0432\u0430",
  exportTitle: "\u0421\u043e\u0437\u0434\u0430\u043d\u0438\u0435 \u043a\u043b\u0438\u043f\u0430",
  exportHelp: "\u041a\u043b\u0438\u043f \u0441\u043e\u0437\u0434\u0430\u0435\u0442\u0441\u044f \u0437\u0430 \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0438\u0437 \u0430\u0440\u0445\u0438\u0432\u0430. \u041f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430 \u043f\u043e\u043c\u043e\u0433\u0430\u0435\u0442 \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043a\u0430\u043c\u0435\u0440\u0443, \u0432\u0440\u0435\u043c\u044f \u0438 \u0446\u0435\u043b\u043e\u0441\u0442\u043d\u043e\u0441\u0442\u044c \u0444\u0430\u0439\u043b\u0430.",
  exportLimits: "\u041b\u0438\u043c\u0438\u0442\u044b",
  exportCamera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  exportPickCamera: "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443",
  exportReason: "\u041e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435",
  exportStart: "\u041d\u0430\u0447\u0430\u043b\u043e",
  exportEnd: "\u041a\u043e\u043d\u0435\u0446",
  exportRun: "\u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043a\u043b\u0438\u043f",
  exportManifest: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u043f\u0430\u0441\u043f\u043e\u0440\u0442",
  exportManifestHelp: "\u041f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430 \u2014 \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0444\u0430\u0439\u043b \u0434\u043b\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438, \u0441 \u043a\u0430\u043a\u043e\u0439 \u043a\u0430\u043c\u0435\u0440\u044b \u0438 \u0437\u0430 \u043a\u0430\u043a\u043e\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0441\u043e\u0437\u0434\u0430\u043d \u043a\u043b\u0438\u043f.",
  exportReady: "\u041a\u043b\u0438\u043f \u0433\u043e\u0442\u043e\u0432.",
  remove: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c",
  noRecords: "\u041d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0435\u0439",
  pickAllPage: "\u0412\u044b\u0431\u0440\u0430\u0442\u044c \u0432\u0441\u0435 \u043d\u0430 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0435",
  openEmbeddedViewer: "\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0432\u0441\u0442\u0440\u043e\u0435\u043d\u043d\u044b\u0439 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
  viewRecord: "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u0437\u0430\u043f\u0438\u0441\u0438",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
  playbackError: "\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u043d\u0435 \u0441\u043c\u043e\u0433 \u0432\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0441\u0442\u0438 \u0437\u0430\u043f\u0438\u0441\u044c \u043e\u043d\u043b\u0430\u0439\u043d. \u0417\u0430\u043f\u0438\u0441\u044c \u043c\u043e\u0436\u043d\u043e \u0441\u043a\u0430\u0447\u0430\u0442\u044c.",
  missingFile: "\u0424\u0430\u0439\u043b \u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u0435\u0442",
  rootUnavailable: "\u041a\u043e\u0440\u0435\u043d\u044c \u0430\u0440\u0445\u0438\u0432\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d",
  rootUnresolved: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0434\u043d\u043e\u0437\u043d\u0430\u0447\u043d\u043e \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c \u0440\u0430\u0441\u043f\u043e\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u0437\u0430\u043f\u0438\u0441\u0438",
  verificationError: "\u041e\u0448\u0438\u0431\u043a\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438",
  unavailable: "\u041d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e",
  recordingActiveEmpty: "\u0418\u0434\u0451\u0442 \u0437\u0430\u043f\u0438\u0441\u044c. \u0417\u0430\u043f\u0438\u0441\u044c \u043f\u043e\u044f\u0432\u0438\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f \u0441\u0435\u0433\u043c\u0435\u043d\u0442\u0430.",
  cancel: "Отмена",
  deleteOneTitle: "Удалить запись?",
  deleteOneMessage: "Запись «{name}» будет удалена без возможности восстановления.",
  deleteSelectedTitle: "Удалить выбранные записи?",
  deleteSelectedMessage: "Будет удалено записей: {count}.",
  deleteCameraTitle: "Удалить записи камеры?",
  deleteCameraMessage: "Будут удалены все записи камеры «{camera}», вошедшие в подтверждённый план.",
  deleteAllTitle: "Удалить все записи?",
  deleteAllMessage: "Будут удалены все записи из подтверждённого сервером списка.",
  preparingDeletionTitle: "Подготовка удаления",
  preparingDeletionMessage: "Сервер формирует точный список записей и рассчитывает объём.",
  deletingTitle: "Удаление записей",
  deletingMessage: "Операция выполняется. Не закрывайте страницу до получения результата.",
  deletionCompletedTitle: "Записи удалены",
  deletionCompletedMessage: "Удалено: {count}; освобождено: {size}.",
  deletionPartialTitle: "Удаление завершено не полностью",
  deletionFailedTitle: "Записи не удалены",
  plannedCount: "Будет удалено",
  deletedCount: "Удалено",
  skippedCount: "Пропущено",
  failedCount: "Ошибки",
  freedSpace: "Освобождено",
  openStorage: "Открыть хранилище",
  refreshRecords: "Обновить записи",
  deletionRetryHint: "Причины показаны ниже. Устраните их и сформируйте новый план удаления.",
  reasonActiveJob: "Запись ещё выполняется",
  reasonActiveJobDetail: "Дождитесь завершения текущего файла записи и повторите операцию.",
  reasonFileMissing: "Файл уже отсутствует",
  reasonFileMissingDetail: "Откройте Хранилище и выполните проверку архива, чтобы сверить метаданные.",
  reasonStorageUnavailable: "Расположение архива недоступно",
  reasonStorageUnavailableDetail: "Проверьте доступность корня архива в разделе Хранилище.",
  reasonUnsafePath: "Расположение записи не подтверждено",
  reasonUnsafePathDetail: "Удаление безопасно заблокировано до проверки архива.",
  reasonForeign: "Запись не принадлежит KM VMS",
  reasonForeignDetail: "KM VMS не удаляет чужие или неподтверждённые данные.",
  reasonPermission: "Недостаточно прав",
  reasonPermissionDetail: "Войдите под пользователем с правом удаления записей.",
  reasonConflict: "Другая операция уже работает с этими записями",
  reasonConflictDetail: "Дождитесь её завершения, обновите список и повторите удаление.",
  reasonPlanExpired: "План удаления устарел",
  reasonPlanExpiredDetail: "Сформируйте новый план и подтвердите его ещё раз.",
  reasonInternal: "Удаление не завершено",
  reasonInternalDetail: "Повторите операцию. Если ошибка сохраняется, проверьте состояние системы.",
};

const ICONS = {
  more: "\u22ef",
  refresh: "\u21bb",
  trash: "\ud83d\uddd1",
  watch: "\u25b6",
  download: "\u2b07",
  export: "\u2702",
  remove: "\ud83d\uddd1",
  prev: "\u2190",
  next: "\u2192",
  up: "\u2191",
  down: "\u2193",
  sort: "\u2195",
  gap: "\u2026",
  close: "\u00d7",
};

function toDateTimeInputParts(dateValue) {
  const dt = dateValue instanceof Date ? dateValue : new Date(dateValue);
  if (!Number.isFinite(dt.getTime())) return "";
  const pad2 = (n) => String(n).padStart(2, "0");
  return `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}-${pad2(dt.getDate())}T${pad2(dt.getHours())}:${pad2(dt.getMinutes())}:${pad2(dt.getSeconds())}`;
}

function pointerDistance(a, b) {
  return Math.hypot(Number(a.x || 0) - Number(b.x || 0), Number(a.y || 0) - Number(b.y || 0));
}

function defaultClipRange(selectedDate, exportLimits) {
  const limitMs = Number(exportLimits?.max_duration_seconds || 3 * 60 * 60) * 1000;
  let start;
  if (selectedDate) {
    start = new Date(`${selectedDate}T00:00:00`);
  } else {
    start = new Date();
    start.setSeconds(0, 0);
    start = new Date(start.getTime() - 60_000);
  }
  if (!Number.isFinite(start.getTime())) {
    start = new Date();
    start.setSeconds(0, 0);
  }
  const end = new Date(start.getTime() + Math.min(60_000, limitMs));
  return {
    startTs: toDateTimeInputParts(start),
    endTs: toDateTimeInputParts(end),
  };
}

function formatRecordingsTableDateTime(value, timezone) {
  if (!value) return "";
  const date = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const options = {
    timeZone: timezone || "UTC",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  };
  try {
    const parts = new Intl.DateTimeFormat("ru-RU", options)
      .formatToParts(date)
      .reduce((acc, part) => {
        if (part.type !== "literal") acc[part.type] = part.value;
        return acc;
      }, {});
    return {
      time: `${parts.hour}:${parts.minute}`,
      date: `${parts.day}.${parts.month}.${parts.year}`,
    };
  } catch {
    const fallback = formatProductDateTime(value, timezone).replace(/:\d{2}(?=,|\s|$)/, "");
    const match = fallback.match(/(\d{2}:\d{2}).*?(\d{2}\.\d{2}\.\d{4})/);
    return match ? { time: match[1], date: match[2] } : { time: fallback, date: "" };
  }
}

function renderRecordingsTableDateTime(value, timezone) {
  const formatted = formatRecordingsTableDateTime(value, timezone);
  if (!formatted) return "-";
  return (
    <span className="recordingsDateTime">
      <span>{formatted.time}</span>
      {formatted.date ? (
        <>
          <span className="recordingsDateDivider" aria-hidden="true"></span>
          <span>{formatted.date}</span>
        </>
      ) : null}
    </span>
  );
}

function ScissorsIcon() {
  return (
    <svg className="recordingsUiIcon recordingsToolbarSvgIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="5.5" cy="6.5" r="3"></circle>
      <circle cx="5.5" cy="17.5" r="3"></circle>
      <path d="M8.3 8.3 20.5 19.2"></path>
      <path d="M8.3 15.7 20.5 4.8"></path>
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg className="recordingsUiIcon recordingsToolbarSvgIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M18.6 8.2A7.2 7.2 0 1 0 19 15"></path>
      <path d="M18.8 4.8v4.1h-4.1"></path>
    </svg>
  );
}

function TrashIcon({ compact = false } = {}) {
  return (
    <svg className={`recordingsUiIcon recordingsTrashIcon ${compact ? "recordingsRowSvgIcon" : "recordingsToolbarSvgIcon"}`} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M4.2 6.8h15.6"></path>
      <path d="M8.9 6.8V4.5h6.2v2.3"></path>
      <path d="M6.7 7.2 7.6 19c.1 1.05 1 1.9 2.05 1.9h4.7c1.05 0 1.95-.85 2.05-1.9l.9-11.8"></path>
      <path d="M10.1 10.7v6.6"></path>
      <path d="M13.9 10.7v6.6"></path>
    </svg>
  );
}

function PlayIcon() {
  return (
    <svg className="recordingsUiIcon recordingsRowSvgIcon recordingsPlayIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M8 5.8v12.4L18.2 12 8 5.8Z"></path>
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg className="recordingsUiIcon recordingsRowSvgIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 4.6v10.2"></path>
      <path d="m7.8 10.8 4.2 4.2 4.2-4.2"></path>
      <path d="M5.8 19.4h12.4"></path>
    </svg>
  );
}

function MoreIcon() {
  return (
    <svg className="recordingsUiIcon recordingsToolbarSvgIcon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="4.8" cy="12" r="1.9"></circle>
      <circle cx="12" cy="12" r="1.7"></circle>
      <circle cx="19.2" cy="12" r="1.9"></circle>
    </svg>
  );
}

const SORT_OPTIONS = {
  created_at: { key: "created_at", label: TEXT.createdAt },
  size_bytes: { key: "size_bytes", label: TEXT.size },
  camera: { key: "camera", label: TEXT.camera },
};

function formatSizeBytes(sizeBytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(sizeBytes || 0);
  let unit = units[0];

  for (const currentUnit of units) {
    unit = currentUnit;
    if (value < 1024 || currentUnit === units[units.length - 1]) break;
    value /= 1024;
  }

  if (sizeBytes <= 0) return "0 B";
  if (unit === "GB" || unit === "TB") return `${value.toFixed(2)} ${unit}`;
  if (unit === "MB") return `${value < 100 ? value.toFixed(1) : value.toFixed(0)} ${unit}`;
  if (unit === "KB") return `${value.toFixed(0)} ${unit}`;
  return `${Math.round(value)} ${unit}`;
}

function parseCreatedAt(value) {
  if (!value) return 0;

  const match = String(value).match(
    /^(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}):(\d{2}):(\d{2})$/
  );

  if (!match) return 0;

  const [, day, month, year, hours, minutes, seconds] = match;
  return new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hours),
    Number(minutes),
    Number(seconds)
  ).getTime();
}

function formatDateInputFromCreatedAt(value) {
  const ts = parseCreatedAt(value);
  if (!ts) return "";

  const dt = new Date(ts);
  const year = dt.getFullYear();
  const month = String(dt.getMonth() + 1).padStart(2, "0");
  const day = String(dt.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function compareValues(left, right, sortBy, sortDir) {
  let result = 0;

  if (sortBy === SORT_OPTIONS.created_at.key) {
    result = Date.parse(left.started_at_system || "") - Date.parse(right.started_at_system || "");
    if (!Number.isFinite(result)) {
      result = parseCreatedAt(left.created_at) - parseCreatedAt(right.created_at);
    }
  } else if (sortBy === SORT_OPTIONS.size_bytes.key) {
    result = Number(left.size_bytes || 0) - Number(right.size_bytes || 0);
  } else if (sortBy === SORT_OPTIONS.camera.key) {
    result = String(left.camera || "").localeCompare(String(right.camera || ""), "ru", {
      sensitivity: "base",
      numeric: true,
    });
  }

  if (result === 0) {
    result = String(left.filename || "").localeCompare(String(right.filename || ""), "ru", {
      sensitivity: "base",
      numeric: true,
    });
  }

  return sortDir === "asc" ? result : -result;
}

function buildPageList(currentPage, pageCount) {
  if (pageCount <= 1) return [1];
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, index) => index + 1);
  }

  const pages = new Set([1, pageCount, currentPage - 1, currentPage, currentPage + 1]);
  if (currentPage <= 3) {
    pages.add(2);
    pages.add(3);
    pages.add(4);
  }
  if (currentPage >= pageCount - 2) {
    pages.add(pageCount - 1);
    pages.add(pageCount - 2);
    pages.add(pageCount - 3);
  }

  const ordered = Array.from(pages)
    .filter((page) => page >= 1 && page <= pageCount)
    .sort((left, right) => left - right);

  const result = [];
  ordered.forEach((page, index) => {
    if (index > 0 && page - ordered[index - 1] > 1) {
      result.push("gap");
    }
    result.push(page);
  });

  return result;
}

function summarizeDeleteResult(result) {
  if (!result || typeof result !== "object") return "\u041e\u043f\u0435\u0440\u0430\u0446\u0438\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430.";
  const deleted = Number(result.deleted_count || 0);
  const skipped = Number(result.skipped_count || 0);
  const failed = Number(result.failed_count || 0);
  const notFound = Number(result.not_found_count || 0);
  return `\u0423\u0434\u0430\u043b\u0435\u043d\u043e: ${deleted}; \u043f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u043e: ${skipped + notFound}; \u043e\u0448\u0438\u0431\u043e\u043a: ${failed}.`;
}

function newClientOperationId(prefix = "recording-delete") {
  const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

function deletionResultFromError(error) {
  const detail = error?.detail || error?.data?.detail;
  return detail && typeof detail === "object" ? detail : null;
}

function deletionReasonRows(result, t) {
  const skipped = result?.skipped_reason_counts || {};
  const failed = result?.failed_reason_counts || {};
  const counts = {};
  for (const source of [skipped, failed]) {
    for (const [reason, count] of Object.entries(source)) {
      counts[reason] = Number(counts[reason] || 0) + Number(count || 0);
    }
  }
  const mapping = {
    active_job: [t.reasonActiveJob, t.reasonActiveJobDetail],
    writing: [t.reasonActiveJob, t.reasonActiveJobDetail],
    file_missing: [t.reasonFileMissing, t.reasonFileMissingDetail],
    metadata_not_found: [t.reasonFileMissing, t.reasonFileMissingDetail],
    storage_unavailable: [t.reasonStorageUnavailable, t.reasonStorageUnavailableDetail],
    root_unavailable: [t.reasonStorageUnavailable, t.reasonStorageUnavailableDetail],
    root_unresolved: [t.reasonUnsafePath, t.reasonUnsafePathDetail],
    outside_kmvms_namespace: [t.reasonUnsafePath, t.reasonUnsafePathDetail],
    path_outside_storage: [t.reasonUnsafePath, t.reasonUnsafePathDetail],
    invalid_path: [t.reasonUnsafePath, t.reasonUnsafePathDetail],
    unowned: [t.reasonForeign, t.reasonForeignDetail],
    foreign_source: [t.reasonForeign, t.reasonForeignDetail],
    permission_denied: [t.reasonPermission, t.reasonPermissionDetail],
    destructive_scope_conflict: [t.reasonConflict, t.reasonConflictDetail],
    destructive_operation_already_running: [t.reasonConflict, t.reasonConflictDetail],
    deletion_plan_expired: [t.reasonPlanExpired, t.reasonPlanExpiredDetail],
    deletion_plan_scope_changed: [t.reasonPlanExpired, t.reasonPlanExpiredDetail],
    recording_deletion_internal_failure: [t.reasonInternal, t.reasonInternalDetail],
    operation_lease_lost: [t.reasonInternal, t.reasonInternalDetail],
    destructive_scope_lease_lost: [t.reasonInternal, t.reasonInternalDetail],
    delete_failed: [t.reasonInternal, t.reasonInternalDetail],
    metadata_update_failed: [t.reasonInternal, t.reasonInternalDetail],
    metadata_update_failed_recovered: [t.reasonInternal, t.reasonInternalDetail],
    limit_exceeded: [t.reasonInternal, t.reasonInternalDetail],
  };
  return Object.entries(counts)
    .filter(([, count]) => Number(count || 0) > 0)
    .map(([code, count]) => {
      const [label, detail] = mapping[code] || [t.reasonInternal, t.reasonInternalDetail];
      return { code, count: Number(count), label, detail };
    });
}

function normalizeRecordingError(message) {
  const text = String(message || "").trim();
  if (!text) return "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044e. \u041f\u043e\u0432\u0442\u043e\u0440\u0438\u0442\u0435 \u043f\u043e\u043f\u044b\u0442\u043a\u0443.";
  if (text.startsWith("{") || text.startsWith("[")) {
    try {
      const parsed = JSON.parse(text);
      return summarizeDeleteResult(parsed?.detail || parsed);
    } catch (_) {
      return "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044e. \u041e\u0442\u0432\u0435\u0442 \u0441\u0435\u0440\u0432\u0435\u0440\u0430 \u043d\u0435 \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u043d.";
    }
  }
  if (text.includes("Recording file not found")) return TEXT.missingFile;
  if (text.includes("metadata")) return "\u041c\u0435\u0442\u0430\u0434\u0430\u043d\u043d\u044b\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b.";
  if (text.includes("Invalid path")) return "\u041f\u0443\u0442\u044c \u0437\u0430\u043f\u0438\u0441\u0438 \u043d\u0435\u0434\u043e\u043f\u0443\u0441\u0442\u0438\u043c.";
  if (text.length > 180) return "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0432\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u044c \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044e. \u041e\u0442\u0432\u0435\u0442 \u0441\u0435\u0440\u0432\u0435\u0440\u0430 \u0441\u043a\u0440\u044b\u0442.";
  return text;
}

function isRecordingAvailable(item) {
  return item?.availability_status === "available"
    && item?.available === true
    && item?.playback_available === true
    && item?.download_available === true;
}

function recordingIdentityKey(item) {
  if (!item) return "";
  return item.recording_ref || (item.segment_id ? `segment:${item.segment_id}:root:${item.archive_root_id || "default"}` : item.path || "");
}

function recordingIdentityPayload(item) {
  if (!item) return {};
  return {
    segment_id: item.segment_id,
    archive_root_id: item.archive_root_id,
    recording_ref: item.recording_ref,
    path: item.path,
  };
}

function recordingIdentityQuery(item) {
  const payload = recordingIdentityPayload(item);
  const params = new URLSearchParams();
  if (payload.segment_id) params.set("segment_id", String(payload.segment_id));
  if (payload.archive_root_id) params.set("archive_root_id", payload.archive_root_id);
  if (payload.recording_ref) params.set("recording_ref", payload.recording_ref);
  if (!params.has("segment_id") && !params.has("recording_ref") && payload.path) params.set("path", payload.path);
  return params.toString();
}

function recordingAvailabilityLabel(item, t) {
  if (item?.availability_status === "root_unavailable") return t.rootUnavailable;
  if (item?.availability_status === "root_unresolved") return t.rootUnresolved;
  if (item?.availability_status === "error") return t.verificationError;
  return t.missingFile;
}

function hasActiveRecordingJobs(recorderStatus, selectedCamera) {
  const states = Array.isArray(recorderStatus?.camera_recording_states)
    ? recorderStatus.camera_recording_states
    : [];
  return states.some((item) => {
    const state = String(item?.job_state || item?.status || item?.state || "").toLowerCase();
    const cameraName = String(item?.camera_name || item?.name || item?.camera || "");
    const selectedMatches = !selectedCamera || selectedCamera === "__all__" || cameraName === selectedCamera;
    return selectedMatches && (state === "recording" || state === "active" || state === "running");
  });
}

export default function RecordingsPage() {
  const { text } = useI18n();
  const t = useMemo(
    () => Object.fromEntries(Object.entries(TEXT).map(([key, value]) => [key, text(value)])),
    [text]
  );
  const [cameras, setCameras] = useState([]);
  const [exportCameraOptions, setExportCameraOptions] = useState([]);
  const [selectedCamera, setSelectedCamera] = useState("__all__");
  const [selectedDate, setSelectedDate] = useState("");
  const [items, setItems] = useState([]);
  const [recordingsLoadState, setRecordingsLoadState] = useState("idle");
  const [recordingsSummary, setRecordingsSummary] = useState(null);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [pageJumpValue, setPageJumpValue] = useState("1");
  const [recordingsPagination, setRecordingsPagination] = useState({ limit: DEFAULT_PAGE_SIZE, offset: 0, total_count: 0, has_more: false });
  const [productTimezone, setProductTimezone] = useState("UTC");
  const [selectedPaths, setSelectedPaths] = useState([]);
  const [sortBy, setSortBy] = useState(SORT_OPTIONS.created_at.key);
  const [sortDir, setSortDir] = useState("desc");
  const [currentPage, setCurrentPage] = useState(1);
  const [error, setError] = useState("");
  const [deleteDialog, setDeleteDialog] = useState(null);
  const [operationToast, setOperationToast] = useState(null);
  const [busy, setBusy] = useState(false);
  const [dangerMenuOpen, setDangerMenuOpen] = useState(false);
  const [recorderStatus, setRecorderStatus] = useState(null);
  const { currentUser } = useCurrentUser();

  const [viewerOpen, setViewerOpen] = useState(false);
  const [viewerTitle, setViewerTitle] = useState("");
  const [viewerUrl, setViewerUrl] = useState("");
  const [viewerItem, setViewerItem] = useState(null);
  const [viewerPlaybackError, setViewerPlaybackError] = useState(false);
  const [viewerRefreshAttempted, setViewerRefreshAttempted] = useState(false);
  const [viewerResolution, setViewerResolution] = useState({ width: 0, height: 0 });
  const [viewerRect, setViewerRect] = useState({ width: 0, height: 0 });
  const [viewerFullscreen, setViewerFullscreen] = useState(false);
  const [exportModal, setExportModal] = useState(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportStatus, setExportStatus] = useState("");
  const [lastExportId, setLastExportId] = useState("");
  const [exportLimits, setExportLimits] = useState(null);

  const requestIdRef = useRef(0);
  const dangerMenuRef = useRef(null);
  const dangerTriggerRef = useRef(null);
  const viewerFrameRef = useRef(null);
  const viewerVideoRef = useRef(null);
  const viewerRestoreStateRef = useRef(null);
  const viewerRefreshInFlightRef = useRef(false);
  const viewerLastTapRef = useRef(null);
  const canDelete = canDeleteRecordings(currentUser);
  const canExport = canExportRecordings(currentUser);

  useEffect(() => {
    if (!canExport) return;
    getArchiveExportLimits().then(setExportLimits).catch(() => {});
  }, [canExport]);

  async function loadCameras() {
    const data = await apiFetch("/recordings/cameras");
    const cameraOptions = data.items || [];
    setCameras(cameraOptions);
    setExportCameraOptions(data.export_items || []);
    return cameraOptions;
  }

  async function loadRecordings(camera = "__all__", dateValue = selectedDate, page = currentPage) {
    const requestId = ++requestIdRef.current;
    setRecordingsLoadState("loading");
    setItems([]);
    setSelectedPaths([]);
    const params = new URLSearchParams();
    if (camera && camera !== "__all__") params.set("camera", camera);
    if (dateValue) params.set("date", productDateFilterParam(dateValue));
    params.set("limit", String(pageSize));
    params.set("offset", String(Math.max(0, (page - 1) * pageSize)));
    params.set("sort_by", sortBy);
    params.set("sort_dir", sortDir);
    const query = params.toString() ? `?${params.toString()}` : "";

    try {
      const data = await apiFetch(`/recordings${query}`);
      if (requestId !== requestIdRef.current) return;

      setItems(data.items || []);
      setRecordingsSummary(data.summary || null);
      setRecordingsPagination(data.pagination || { limit: pageSize, offset: 0, total_count: data.items?.length || 0, has_more: false });
      setProductTimezone(data?.timezone?.id || "UTC");
      setError("");
      setRecordingsLoadState("loaded");
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(normalizeRecordingError(err.message));
      setRecordingsLoadState("error");
    }
  }

  async function loadRecorderStatus() {
    const status = await apiFetch("/system/recorder/summary").catch(() => null);
    setRecorderStatus(status);
  }

  async function initialLoad() {
    try {
      setError("");
      await Promise.all([loadCameras(), loadRecorderStatus()]);
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    }
  }

  useEffect(() => {
    initialLoad();
  }, []);

  useEffect(() => {
    loadRecordings(selectedCamera, selectedDate, currentPage);
  }, [selectedCamera, selectedDate, sortBy, sortDir, currentPage, pageSize]);

  useEffect(() => {
    setCurrentPage(1);
  }, [selectedCamera, selectedDate, sortBy, sortDir]);

  useEffect(() => {
    setPageJumpValue(String(currentPage));
  }, [currentPage]);

  useEffect(() => {
    function handlePointerDown(event) {
      if (!dangerMenuRef.current?.contains(event.target)) {
        setDangerMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, []);

  async function refresh() {
    try {
      setError("");
      const [cameraOptions] = await Promise.all([loadCameras(), loadRecorderStatus()]);
      const effectiveCamera = resolveEffectiveRecordingCamera(selectedCamera, cameraOptions);
      const effectivePage = effectiveCamera === selectedCamera ? currentPage : 1;
      if (effectiveCamera !== selectedCamera) {
        setSelectedCamera(effectiveCamera);
        setCurrentPage(1);
      }
      await loadRecordings(effectiveCamera, selectedDate, effectivePage);
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    }
  }

  function toggleSelected(path) {
    setSelectedPaths((prev) =>
      prev.includes(path) ? prev.filter((x) => x !== path) : [...prev, path]
    );
  }

  function handleSort(nextSortBy) {
    if (sortBy === nextSortBy) {
      setSortDir((prev) => (prev === "asc" ? "desc" : "asc"));
      return;
    }

    setSortBy(nextSortBy);
    setSortDir(nextSortBy === SORT_OPTIONS.created_at.key ? "desc" : "asc");
  }

  function handlePageSizeChange(event) {
    const nextPageSize = Number(event.target.value);
    if (!PAGE_SIZE_OPTIONS.includes(nextPageSize)) return;
    setPageSize(nextPageSize);
    setCurrentPage(1);
  }

  function handlePageJump(event) {
    event.preventDefault();
    const requested = Number.parseInt(pageJumpValue, 10);
    if (!Number.isFinite(requested)) {
      setPageJumpValue(String(currentPage));
      return;
    }
    const nextPage = Math.min(Math.max(requested, 1), pageCount);
    setPageJumpValue(String(nextPage));
    setCurrentPage(nextPage);
  }

  const recordingsLoaded = recordingsLoadState === "loaded";
  const recordingsFirstLoading = recordingsLoadState === "idle" || recordingsLoadState === "loading";
  const filteredItems = recordingsLoaded ? items : [];

  const pageCount = Math.max(1, Math.ceil(Number(recordingsPagination?.total_count || recordingsSummary?.count || 0) / pageSize));

  useEffect(() => {
    setCurrentPage((prev) => Math.min(prev, pageCount));
  }, [pageCount]);

  const paginatedItems = filteredItems;

  const visiblePaths = useMemo(
    () => paginatedItems.map((item) => recordingIdentityKey(item)),
    [paginatedItems]
  );

  const allVisibleSelected = useMemo(() => {
    if (!visiblePaths.length) return false;
    return visiblePaths.every((path) => selectedPaths.includes(path));
  }, [visiblePaths, selectedPaths]);

  const visibleSummary = useMemo(() => {
    return {
      count: recordingsSummary?.count ?? 0,
      size_human: recordingsSummary?.size_human || formatSizeBytes(recordingsSummary?.size_bytes || 0),
    };
  }, [recordingsSummary]);

  const clipCameraOptions = useMemo(() => {
    return exportCameraOptions;
  }, [exportCameraOptions]);

  const paginationItems = useMemo(
    () => buildPageList(currentPage, pageCount),
    [currentPage, pageCount]
  );

  const activeRecordingEmpty = useMemo(
    () => recordingsLoaded && !filteredItems.length && hasActiveRecordingJobs(recorderStatus, selectedCamera),
    [recordingsLoaded, filteredItems.length, recorderStatus, selectedCamera]
  );
  const recordingsEmptyMessage = recordingsFirstLoading
    ? t.loading
    : recordingsLoadState === "error"
      ? t.unavailable
      : activeRecordingEmpty
        ? t.recordingActiveEmpty
        : t.noRecords;

  const viewerAdaptiveHighRes = useMemo(
    () => shouldUseAdaptiveHighResolutionPlayback(viewerResolution, viewerRect, viewerFullscreen),
    [viewerResolution, viewerRect, viewerFullscreen]
  );
  const viewerRenderState = useMemo(
    () =>
      selectCompactVideoRenderMode({
        dimensions: viewerResolution,
        rect: viewerRect,
        isFullscreen: viewerFullscreen,
        sourceHighResolution: true,
      }),
    [viewerResolution, viewerRect, viewerFullscreen]
  );

  useEffect(() => {
    if (!viewerOpen) return;
    const el = viewerFrameRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    const updateRect = () => {
      const rect = el.getBoundingClientRect();
      setViewerRect({ width: Math.round(rect.width || 0), height: Math.round(rect.height || 0) });
    };
    updateRect();
    const observer = new ResizeObserver(updateRect);
    observer.observe(el);
    return () => observer.disconnect();
  }, [viewerOpen, viewerUrl]);

  useEffect(() => {
    if (!viewerOpen) return;
    function handleFullscreenChange() {
      const fullscreenElement = document.fullscreenElement;
      const video = viewerVideoRef.current;
      const frame = viewerFrameRef.current;
      setViewerFullscreen(Boolean(fullscreenElement && (fullscreenElement === video || fullscreenElement === frame || frame?.contains(fullscreenElement))));
    }
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    handleFullscreenChange();
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, [viewerOpen]);

  function toggleSelectAll() {
    if (allVisibleSelected) {
      setSelectedPaths((prev) => prev.filter((path) => !visiblePaths.includes(path)));
      return;
    }

    setSelectedPaths((prev) => Array.from(new Set([...prev, ...visiblePaths])));
  }

  async function handleDownload(item) {
    if (!isRecordingAvailable(item)) return;
    try {
      setError("");
      const mediaToken = await issueRecordingMediaToken(recordingIdentityPayload(item), "download");
      const url = `/api/recordings/download?${recordingIdentityQuery(item)}&media_token=${encodeURIComponent(mediaToken)}`;
      const a = document.createElement("a");
      a.href = url;
      a.download = item.filename || "recording.mp4";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    }
  }

  function toDateTimeLocal(value) {
    return productDateTimeInputValue(value) || toDateTimeInputParts(value);
  }

  function openExportModal() {
    if (!canExport) return;
    const { startTs, endTs } = defaultClipRange(selectedDate, exportLimits);
    setError("");
    setLastExportId("");
    setExportStatus("");
    setExportModal({
      cameraId: "",
      title: t.createClip,
      reason: "",
      startTs,
      endTs,
    });
  }

  function closeExportModal() {
    if (exportBusy) return;
    setExportModal(null);
    setExportStatus("");
    setLastExportId("");
  }

  async function submitExport() {
    if (!exportModal || exportBusy) return;
    const validation = validateArchiveExportSelection(
      {
        startTs: exportModal.startTs,
        endTs: exportModal.endTs,
        estimatedSourceBytes: 0,
      },
      exportLimits
    );
    if (!exportModal.cameraId) {
      setError(t.exportPickCamera);
      setExportStatus(t.exportPickCamera);
      return;
    }
    if (validation) {
      setError(validation);
      setExportStatus(validation);
      return;
    }
    try {
      setError("");
      setExportBusy(true);
      setLastExportId("");
      const payload = buildArchiveExportPayload({
        cameraId: exportModal.cameraId,
        startTs: exportModal.startTs,
        endTs: exportModal.endTs,
        title: exportModal.title,
        reason: exportModal.reason,
      });
      const result = await runArchiveExportWorkflow(payload, {
        onStatus: (message, job) => {
          setExportStatus(message);
          if (job?.id) setLastExportId(job.id);
        },
      });
      if (result?.job?.id) setLastExportId(result.job.id);
      saveBlobDownload(result.clip.blob, result.clip.filename || "km-vms-clip.mkv");
      setExportStatus(t.exportReady);
      setOperationToast({
        id: `recordings-clip-${result?.job?.id || Date.now()}`,
        title: t.exportReady,
        tone: "success",
      });
    } catch (err) {
      const message = normalizeArchiveExportError(err.message);
      setError(message);
      setExportStatus(message);
    } finally {
      setExportBusy(false);
    }
  }

  async function downloadLastManifest() {
    if (!lastExportId || exportBusy) return;
    try {
      setExportBusy(true);
      const manifest = await downloadArchiveManifest(lastExportId);
      saveBlobDownload(manifest.blob, manifest.filename || "km-vms-clip-passport.json");
    } catch (err) {
      setError(normalizeArchiveExportError(err.message));
    } finally {
      setExportBusy(false);
    }
  }

  async function handleWatch(item) {
    if (!isRecordingAvailable(item)) return;
    try {
      setError("");
      const url = await buildRecordingStreamUrl(item);
      setViewerTitle(item.filename);
      setViewerUrl(url);
      setViewerItem(item);
      setViewerPlaybackError(false);
      setViewerRefreshAttempted(false);
      setViewerResolution({ width: 0, height: 0 });
      setViewerRect({ width: 0, height: 0 });
      setViewerFullscreen(false);
      viewerRestoreStateRef.current = null;
      viewerRefreshInFlightRef.current = false;
      setViewerOpen(true);
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    }
  }

  function closeViewer() {
    const frame = viewerFrameRef.current;
    if (document.fullscreenElement && frame && (document.fullscreenElement === frame || frame.contains(document.fullscreenElement))) {
      document.exitFullscreen?.().catch(() => {});
    }
    setViewerTitle("");
    setViewerUrl("");
    setViewerItem(null);
    setViewerPlaybackError(false);
    setViewerRefreshAttempted(false);
    setViewerResolution({ width: 0, height: 0 });
    setViewerRect({ width: 0, height: 0 });
    setViewerFullscreen(false);
    viewerRestoreStateRef.current = null;
    viewerRefreshInFlightRef.current = false;
    setViewerOpen(false);
  }

  async function toggleViewerFrameFullscreen(event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    const frame = viewerFrameRef.current;
    if (!frame) return;
    if (document.fullscreenElement && (document.fullscreenElement === frame || frame.contains(document.fullscreenElement))) {
      await document.exitFullscreen?.().catch(() => {});
      return;
    }
    await frame.requestFullscreen?.().catch(() => {});
  }

  function handleViewerSurfacePointerUp(event) {
    if (event.pointerType !== "touch") return;
    if (event.target?.closest?.("button, select, input, textarea, a")) return;
    const scale = Number(event.currentTarget?.querySelector?.("[data-video-zoom-surface]")?.getAttribute("data-video-zoom-scale") || 1);
    if (scale > 1.001) return;

    const now = Date.now();
    const point = { x: Number(event.clientX || 0), y: Number(event.clientY || 0) };
    const previous = viewerLastTapRef.current;
    viewerLastTapRef.current = { time: now, point };

    if (
      previous &&
      now - previous.time <= DOUBLE_TAP_MS &&
      pointerDistance(previous.point, point) <= DOUBLE_TAP_DISTANCE_PX
    ) {
      viewerLastTapRef.current = null;
      toggleViewerFrameFullscreen(event);
    }
  }

  async function refreshViewerUrlAfterMediaError() {
    if (
      !viewerItem ||
      viewerRefreshAttempted ||
      viewerRefreshInFlightRef.current
    ) {
      if (viewerRefreshInFlightRef.current) return;
      setViewerPlaybackError(true);
      return;
    }

    const video = viewerVideoRef.current;
    viewerRestoreStateRef.current = {
      currentTime: Number(video?.currentTime || 0),
      playbackRate: Number(video?.playbackRate || 1),
      playIntent: Boolean(video && !video.paused && !video.ended),
    };

    try {
      viewerRefreshInFlightRef.current = true;
      setViewerRefreshAttempted(true);
      const url = await buildRecordingStreamUrl(viewerItem);
      setViewerUrl(url);
      setViewerPlaybackError(false);
    } catch (_) {
      viewerRestoreStateRef.current = null;
      setViewerResolution({ width: 0, height: 0 });
      setViewerPlaybackError(true);
    } finally {
      viewerRefreshInFlightRef.current = false;
    }
  }

  function restoreViewerPlaybackState() {
    const video = viewerVideoRef.current;
    if (video?.videoWidth || video?.videoHeight) {
      setViewerResolution(normalizeVideoDimensions(video.videoWidth, video.videoHeight));
    }
    const restoreState = viewerRestoreStateRef.current;
    if (!video || !restoreState) return;

    viewerRestoreStateRef.current = null;

    const targetTime = Math.max(0, Number(restoreState.currentTime || 0));
    const duration = Number(video.duration || 0);
    const safeTime = Number.isFinite(duration) && duration > 0
      ? Math.min(targetTime, Math.max(0, duration - 0.25))
      : targetTime;

    try {
      video.currentTime = safeTime;
    } catch (_) {}

    try {
      video.playbackRate = Number(restoreState.playbackRate || 1);
    } catch (_) {}

    if (restoreState.playIntent) {
      video.play().catch(() => {});
    } else {
      try {
        video.pause();
      } catch (_) {}
    }
  }

  function closeDeleteDialog() {
    if (deleteDialog?.busy) return;
    const readyPlanId = deleteDialog?.readyPlanId;
    setDeleteDialog(null);
    if (readyPlanId) {
      apiFetch(`/recordings/deletion-plans/${encodeURIComponent(readyPlanId)}`, { method: "DELETE" }).catch(() => {});
    }
  }

  function showDeletionResult(result) {
    const completed = result?.ok === true && result?.status === "completed";
    if (completed) {
      setDeleteDialog(null);
      setOperationToast({
        id: `recording-delete-${result.operation_id || Date.now()}`,
        title: t.deletionCompletedTitle,
        message: t.deletionCompletedMessage
          .replace("{count}", String(result.deleted_count || 0))
          .replace("{size}", formatSizeBytes(result.bytes_freed || 0)),
        closeLabel: t.close,
        tone: "success",
      });
      return;
    }
    const reasons = deletionReasonRows(result, t);
    const reasonCodes = new Set(reasons.map((reason) => reason.code));
    const storageActionNeeded = [...reasonCodes].some((code) => [
      "file_missing",
      "metadata_not_found",
      "storage_unavailable",
      "root_unavailable",
      "root_unresolved",
      "outside_kmvms_namespace",
      "path_outside_storage",
      "invalid_path",
    ].includes(code));
    setDeleteDialog({
      id: `recording-delete-result-${result?.operation_id || Date.now()}`,
      title: Number(result?.deleted_count || 0) > 0 ? t.deletionPartialTitle : t.deletionFailedTitle,
      message: t.deletionRetryHint,
      summary: [
        { label: t.deletedCount, value: String(result?.deleted_count || 0) },
        { label: t.skippedCount, value: String(result?.skipped_count || 0) },
        { label: t.failedCount, value: String(result?.failed_count || 0) },
        { label: t.freedSpace, value: formatSizeBytes(result?.bytes_freed || 0) },
      ],
      reasons: reasons.length ? reasons : [{ code: "unknown", label: t.reasonInternal, detail: t.reasonInternalDetail }],
      actions: [
        ...(storageActionNeeded ? [{ id: "open-storage", label: t.openStorage, onClick: () => window.location.assign("/storage") }] : []),
        { id: "refresh-recordings", label: t.refreshRecords, onClick: async () => { setDeleteDialog(null); await refresh(); } },
      ],
      closeLabel: t.close,
      tone: "error",
    });
  }

  function showDeletionError(error) {
    const result = deletionResultFromError(error);
    if (result) {
      showDeletionResult(result);
      return;
    }
    setDeleteDialog({
      id: `recording-delete-error-${Date.now()}`,
      title: t.deletionFailedTitle,
      message: normalizeRecordingError(error?.message),
      reasons: [{ code: "request_failed", label: t.reasonInternal, detail: t.reasonInternalDetail }],
      closeLabel: t.close,
      tone: "error",
    });
  }

  async function executeExactDelete({ url, options, operationId }) {
    setBusy(true);
    setError("");
    setDeleteDialog({
      id: `recording-delete-running-${operationId}`,
      title: t.deletingTitle,
      message: t.deletingMessage,
      busy: true,
      dismissible: false,
      tone: "warning",
    });
    try {
      const result = await apiFetch(url, options);
      await refresh();
      showDeletionResult(result);
    } catch (error) {
      showDeletionError(error);
    } finally {
      setBusy(false);
    }
  }

  function handleDeleteOne(item) {
    if (!canDelete) return;
    const operationId = newClientOperationId("recording-single");
    setDeleteDialog({
      id: `recording-single-confirm-${operationId}`,
      title: t.deleteOneTitle,
      presentation: "compact-confirmation",
      message: t.deleteOneMessage.replace("{name}", item.filename || t.file),
      summary: [{ label: t.size, value: formatSizeBytes(item.size_bytes || item.size || 0) }],
      confirmLabel: t.remove,
      cancelLabel: t.cancel,
      closeLabel: t.cancel,
      confirmTone: "danger",
      tone: "error",
      onConfirm: () => executeExactDelete({
        operationId,
        url: `/recordings?${recordingIdentityQuery(item)}&operation_id=${encodeURIComponent(operationId)}`,
        options: { method: "DELETE" },
      }),
    });
  }

  function handleDeleteSelected() {
    if (!canDelete || !selectedPaths.length) return;
    const selectedItems = paginatedItems
      .filter((item) => selectedPaths.includes(recordingIdentityKey(item)))
      .map((item) => recordingIdentityPayload(item));
    const operationId = newClientOperationId("recording-selected");
    setDeleteDialog({
      id: `recording-selected-confirm-${operationId}`,
      title: t.deleteSelectedTitle,
      presentation: "compact-confirmation",
      message: t.deleteSelectedMessage.replace("{count}", String(selectedItems.length)),
      summary: [{ label: t.selected, value: String(selectedItems.length) }],
      confirmLabel: t.remove,
      cancelLabel: t.cancel,
      closeLabel: t.cancel,
      confirmTone: "danger",
      tone: "error",
      onConfirm: () => executeExactDelete({
        operationId,
        url: "/recordings/bulk-delete",
        options: {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ operation_id: operationId, items: selectedItems }),
        },
      }),
    });
  }

  async function prepareDynamicDeletion(scope, camera = null) {
    dangerTriggerRef.current?.focus({ preventScroll: true });
    setDangerMenuOpen(false);
    setBusy(true);
    setError("");
    setDeleteDialog({
      id: `recording-plan-${scope}`,
      title: t.preparingDeletionTitle,
      message: t.preparingDeletionMessage,
      busy: true,
      dismissible: false,
      tone: "warning",
    });
    try {
      const plan = await apiFetch("/recordings/deletion-plans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scope, camera }),
      });
      const title = scope === "all" ? t.deleteAllTitle : t.deleteCameraTitle;
      const message = scope === "all"
        ? t.deleteAllMessage
        : t.deleteCameraMessage.replace("{camera}", camera || "");
      setDeleteDialog({
        id: `recording-plan-confirm-${plan.plan_id}`,
        readyPlanId: plan.plan_id,
        title,
        presentation: "compact-confirmation",
        message,
        summary: [
          { label: t.plannedCount, value: String(plan.planned_count || 0) },
          { label: t.totalSize, value: formatSizeBytes(plan.planned_bytes || 0) },
        ],
        confirmLabel: t.remove,
        cancelLabel: t.cancel,
        closeLabel: t.cancel,
        confirmTone: "danger",
        tone: "error",
        onConfirm: () => executeDeletionPlan(plan),
      });
    } catch (error) {
      showDeletionError(error);
    } finally {
      setBusy(false);
    }
  }

  async function executeDeletionPlan(plan) {
    setBusy(true);
    setDeleteDialog({
      id: `recording-plan-running-${plan.plan_id}`,
      title: t.deletingTitle,
      message: t.deletingMessage,
      busy: true,
      dismissible: false,
      tone: "warning",
    });
    try {
      const result = await apiFetch(`/recordings/deletion-plans/${encodeURIComponent(plan.plan_id)}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      await refresh();
      showDeletionResult(result);
    } catch (error) {
      showDeletionError(error);
    } finally {
      setBusy(false);
    }
  }

  function handleDeleteByCamera() {
    if (!canDelete || !selectedCamera || selectedCamera === "__all__") return;
    prepareDynamicDeletion("camera", selectedCamera);
  }

  function handleDeleteAll() {
    if (!canDelete) return;
    prepareDynamicDeletion("all");
  }

  return (
    <Layout>
      <div className="standardPage">
      <div className="pageHeader recordingsHeader">
        <div>
          <h1 className="pageTitle">{t.title}</h1>
          <div className="pageSubtitle">{t.subtitle}</div>
        </div>
      </div>

      {error ? (
        <div className="badge err recordingsErrorBadge">
          {error}
        </div>
      ) : null}
      <div className="card recordingsFilterCard">
        <div className="recordingsFilterBar">
          <div className="recordingsFilterGroup">
            <select
              className="select recordingsFilterSelect"
              value={selectedCamera}
              onChange={(e) => setSelectedCamera(e.target.value)}
            >
              <option value="__all__">{t.allCameras}</option>
              {cameras.map((camera) => (
                <option key={camera} value={camera}>
                  {camera}
                </option>
              ))}
            </select>

            <input
              type="date"
              className="input recordingsFilterDate"
              value={selectedDate}
              onChange={(e) => setSelectedDate(e.target.value)}
              aria-label={t.date}
            />

            {canExport ? (
              <button
                type="button"
                className="button secondary small recordingsActionButton recordingsToolbarIconButton recordingsCreateClipButton"
                onClick={openExportModal}
                disabled={exportBusy}
                title={t.createClipTooltip}
                aria-label={t.createClip}
              >
                <ScissorsIcon />
              </button>
            ) : null}
          </div>

          <div className="recordingsToolbar recordingsToolbarCompact">
            <button
              className="button secondary small recordingsActionButton recordingsToolbarIconButton"
              onClick={refresh}
              title={t.refresh}
              aria-label={t.refresh}
            >
              <RefreshIcon />
            </button>

            {canDelete ? (
              <>
                <button
                  className="button secondary small recordingsActionButton recordingsToolbarIconButton"
                  onClick={handleDeleteSelected}
                  disabled={!selectedPaths.length || busy}
                  title={t.deleteSelected}
                  aria-label={t.deleteSelected}
                >
                  <TrashIcon />
                </button>

                <div className="recordingsDangerMenu recordingsToolbarMenu" ref={dangerMenuRef}>
                  <button
                    ref={dangerTriggerRef}
                    className="button secondary small recordingsDangerTrigger"
                    onClick={() => setDangerMenuOpen((prev) => !prev)}
                    aria-haspopup="menu"
                    aria-expanded={dangerMenuOpen}
                    title={t.dangerActions}
                  >
                    <MoreIcon />
                  </button>

                  {dangerMenuOpen ? (
                    <div className="recordingsDangerDropdown" role="menu">
                      <div className="recordingsDangerTitle">{t.dangerActions}</div>
                      <button
                        className="recordingsDangerItem"
                        onClick={handleDeleteByCamera}
                        disabled={selectedCamera === "__all__" || busy}
                        role="menuitem"
                      >
                        {t.deleteCamera}
                      </button>
                      <button
                        className="recordingsDangerItem recordingsDangerItemAlert"
                        onClick={handleDeleteAll}
                        disabled={busy}
                        role="menuitem"
                      >
                        {t.deleteAll}
                      </button>
                    </div>
                  ) : null}
                </div>
              </>
            ) : null}
          </div>
        </div>
      </div>

      <div className="recordingsStatsRow">
        <div className="badge">{t.totalFiles}: {recordingsLoaded ? visibleSummary.count : t.loading}</div>
        <div className="badge">{t.totalSize}: {recordingsLoaded ? visibleSummary.size_human : t.loading}</div>
        <div className="badge">{t.page}: {recordingsLoaded ? `${currentPage} / ${pageCount}` : t.loading}</div>
        {selectedPaths.length ? (
          <div className="badge ok">{t.selected}: {selectedPaths.length}</div>
        ) : null}
      </div>

      <div className="card recordingsTableCard">
        <div className="recordingsMobileControls">
          {canDelete ? (
            <label className="recordingsMobileSelectAll">
              <input
                type="checkbox"
                checked={allVisibleSelected}
                onChange={toggleSelectAll}
                aria-label={t.pickAllPage}
              />
              <span>{t.pickAllPage}</span>
            </label>
          ) : <span />}
          <button
            className={`recordingsMobileSortButton ${sortBy === SORT_OPTIONS.size_bytes.key ? "active" : ""}`}
            onClick={() => handleSort(SORT_OPTIONS.size_bytes.key)}
            type="button"
          >
            <span>{t.size}</span>
            <span aria-hidden="true">{sortBy === SORT_OPTIONS.size_bytes.key ? (sortDir === "asc" ? ICONS.up : ICONS.down) : ICONS.sort}</span>
          </button>
        </div>
        <div className="recordingsTableWrap">
          <table className="table recordingsTable">
            <colgroup>
              <col className="recordingsSelectCol" />
              <col className="recordingsCameraCol" />
              <col className="recordingsSpacerCol" />
              <col className="recordingsFileCol" />
              <col className="recordingsDateCol" />
              <col className="recordingsSizeCol" />
              <col className="recordingsActionsCol" />
            </colgroup>
            <thead>
              <tr>
                <th style={{ width: 44 }}>
                  {canDelete ? (
                    <input
                      type="checkbox"
                      checked={allVisibleSelected}
                      onChange={toggleSelectAll}
                      aria-label={t.pickAllPage}
                    />
                  ) : null}
                </th>
                <th>{t.camera}</th>
                <th className="recordingsSpacerHeader" aria-hidden="true"></th>
                <th>{t.file}</th>
                <th className="recordingsDateHeader">{t.createdAt}</th>
                <th className="recordingsSizeHeader">
                  <button
                    className={`recordingsSortButton ${sortBy === SORT_OPTIONS.size_bytes.key ? "active" : ""}`}
                    onClick={() => handleSort(SORT_OPTIONS.size_bytes.key)}
                  >
                    <span className="recordingsSortLabel">{t.size}</span>
                    <span className="recordingsSortIcon">{sortBy === SORT_OPTIONS.size_bytes.key ? (sortDir === "asc" ? ICONS.up : ICONS.down) : ICONS.sort}</span>
                  </button>
                </th>
                <th className="recordingsActionsHeader">{t.actions}</th>
              </tr>
            </thead>
            <tbody>
              {paginatedItems.map((item) => (
                <tr key={recordingIdentityKey(item)}>
                  <td>
                    {canDelete ? (
                      <input
                        type="checkbox"
                        checked={selectedPaths.includes(recordingIdentityKey(item))}
                        onChange={() => toggleSelected(recordingIdentityKey(item))}
                        aria-label={`\u0412\u044b\u0431\u0440\u0430\u0442\u044c ${item.filename}`}
                      />
                    ) : null}
                  </td>
                  <td className="recordingsCameraCell" data-label={t.camera}>{item.camera}</td>
                  <td className="recordingsSpacerCell" aria-hidden="true"></td>
                  <td className="recordingsFilenameCell" data-label={t.file}>
                    {isRecordingAvailable(item) ? (
                      <button
                        className="linkButton recordingsFileLink"
                        onClick={() => handleWatch(item)}
                        title={t.openEmbeddedViewer}
                      >
                        {item.filename}
                      </button>
                    ) : (
                      <div>
                        <div>{item.filename}</div>
                        <div className="recordingsMissingStatus">{recordingAvailabilityLabel(item, t)}</div>
                      </div>
                    )}
                  </td>
                  <td className="recordingsDateCell" data-label={t.createdAt}>
                    {item.started_at_system ? renderRecordingsTableDateTime(item.started_at_system, productTimezone) : (item.created_at || "-")}
                  </td>
                  <td className="recordingsSizeCell" data-label={t.size}>{item.size_human}</td>
                  <td className="recordingsActionsCell" data-label={t.actions}>
                    <div className="recordingsActions">
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleWatch(item)}
                        disabled={!isRecordingAvailable(item)}
                        title={t.watch}
                        aria-label={t.watch}
                      >
                        <PlayIcon />
                      </button>
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleDownload(item)}
                        disabled={!isRecordingAvailable(item)}
                        title={t.downloadSource}
                        aria-label={t.downloadSource}
                      >
                        <DownloadIcon />
                      </button>
                      {canDelete ? (
                        <button
                          className="recordingsIconButton danger"
                          onClick={() => handleDeleteOne(item)}
                          disabled={busy}
                          title={t.remove}
                          aria-label={t.remove}
                        >
                          <TrashIcon compact />
                        </button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ))}

              {!paginatedItems.length ? (
                <tr>
                  <td colSpan="7" className="recordingsEmptyCell">
                    {recordingsEmptyMessage}
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        <div className="recordingsPagination">
          <label className="recordingsPageSizeControl">
            <span>{t.pageSize}</span>
            <select value={pageSize} onChange={handlePageSizeChange}>
              {PAGE_SIZE_OPTIONS.map((size) => (
                <option key={size} value={size}>{size}</option>
              ))}
            </select>
          </label>

          <button
            className="recordingsPageButton"
            onClick={() => setCurrentPage((prev) => Math.max(prev - 1, 1))}
            disabled={currentPage === 1}
          >
            {ICONS.prev}
          </button>

          {paginationItems.map((item, index) =>
            item === "gap" ? (
              <span key={`gap-${index}`} className="recordingsPageGap">
                {ICONS.gap}
              </span>
            ) : (
              <button
                key={item}
                className={`recordingsPageButton ${currentPage === item ? "active" : ""}`}
                onClick={() => setCurrentPage(item)}
              >
                {item}
              </button>
            )
          )}

          <button
            className="recordingsPageButton"
            onClick={() => setCurrentPage((prev) => Math.min(prev + 1, pageCount))}
            disabled={currentPage === pageCount}
          >
            {ICONS.next}
          </button>

          <form className="recordingsPageJump" onSubmit={handlePageJump}>
            <input
              type="number"
              min="1"
              max={pageCount}
              value={pageJumpValue}
              onChange={(event) => setPageJumpValue(event.target.value)}
              aria-label={t.page}
            />
            <button className="recordingsPageButton" type="submit" title={t.jumpToPage}>
              {t.jumpToPage}
            </button>
          </form>
        </div>
      </div>

      {viewerOpen ? (
        <div className="modalBackdrop">
          <div className="modal modalWide" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>{t.viewRecord}</h2>
              <button
                className="iconCloseButton"
                onClick={closeViewer}
                aria-label={t.close}
              >
                {ICONS.close}
              </button>
            </div>

            <div style={{ marginBottom: 14, color: "#475569" }}>{viewerTitle}</div>

            {viewerPlaybackError ? (
              <div className="recordingPlaybackNotice">
                {t.playbackError}
              </div>
            ) : (
              <div
                ref={viewerFrameRef}
                className={`recordingVideoFrame ${viewerAdaptiveHighRes ? "adaptiveHighRes" : ""}`}
                data-highres-adaptive={viewerAdaptiveHighRes ? "true" : "false"}
                data-natural-resolution={`${viewerResolution.width}x${viewerResolution.height}`}
                data-render-context="records"
                data-renderer="native"
                data-render-mode={viewerRenderState.mode}
                data-quality-tier={viewerRenderState.qualityTier}
                data-downscale-ratio={viewerRenderState.ratio == null ? "" : viewerRenderState.ratio.toFixed(4)}
                data-rendered-rect={`${viewerRect.width}x${viewerRect.height}`}
                data-decoded-resolution={`${viewerResolution.width}x${viewerResolution.height}`}
                data-source-resolution={`${viewerResolution.width}x${viewerResolution.height}`}
                data-ready-state={viewerVideoRef.current?.readyState || 0}
                data-dimension-source={viewerResolution.width && viewerResolution.height ? "video-metadata" : "missing"}
                data-canvas-ready="false"
                data-first-frame-drawn="false"
                data-canvas-draw-error=""
                data-fullscreen={viewerFullscreen ? "true" : "false"}
                onPointerUpCapture={handleViewerSurfacePointerUp}
                onDoubleClickCapture={toggleViewerFrameFullscreen}
              >
                <VideoZoomPanSurface
                  className="recordingVideoZoomSurface"
                  context="records"
                  sourceKey={viewerUrl}
                >
                  <video
                    key={viewerUrl}
                    ref={viewerVideoRef}
                    src={viewerUrl}
                    controls
                    controlsList="nofullscreen"
                    playsInline
                    autoPlay
                    preload="metadata"
                    className={`recordingVideo ${viewerAdaptiveHighRes ? "recordingVideoAdaptiveHighRes" : ""}`}
                    onLoadedMetadata={restoreViewerPlaybackState}
                    onCanPlay={restoreViewerPlaybackState}
                    onError={refreshViewerUrlAfterMediaError}
                  />
                </VideoZoomPanSurface>
              </div>
            )}

            <div className="actions">
              {viewerItem ? (
                <button className="button secondary" onClick={() => handleDownload(viewerItem)}>
                  {t.download}
                </button>
              ) : null}
              <button className="button secondary" onClick={closeViewer}>
                {t.close}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {exportModal ? (
        <div className="modalBackdrop">
          <div className="modal archiveExportModal" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h2 className="archiveExportTitle">{t.exportTitle}</h2>
              <button className="iconCloseButton" onClick={closeExportModal} disabled={exportBusy} aria-label={t.close}>
                {ICONS.close}
              </button>
            </div>
            <div className="archiveExportForm">
              <div className="archiveExportInfo">
                {t.exportHelp} {describeArchiveExportLimits(exportLimits)}
              </div>
              <div className="archiveExportFields">
                <label className="archiveExportField">
                  <span>{t.exportCamera}</span>
                  <select
                    className="select"
                    value={exportModal.cameraId}
                    onChange={(event) => setExportModal((prev) => ({ ...prev, cameraId: event.target.value }))}
                    disabled={exportBusy}
                  >
                    <option value="">{t.exportPickCamera}</option>
                    {clipCameraOptions.map((camera) => (
                      <option key={camera.id} value={camera.id}>
                        {camera.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="archiveExportField">
                  <span>{t.exportReason}</span>
                  <input
                    className="input"
                    value={exportModal.reason}
                    onChange={(event) => setExportModal((prev) => ({ ...prev, reason: event.target.value }))}
                    disabled={exportBusy}
                    maxLength={500}
                  />
                </label>
                <label className="archiveExportField">
                  <span>{t.exportStart}</span>
                  <input
                    className="input"
                    type="datetime-local"
                    step="1"
                    value={toDateTimeLocal(exportModal.startTs)}
                    onChange={(event) => setExportModal((prev) => ({ ...prev, startTs: event.target.value }))}
                    disabled={exportBusy}
                  />
                </label>
                <label className="archiveExportField">
                  <span>{t.exportEnd}</span>
                  <input
                    className="input"
                    type="datetime-local"
                    step="1"
                    value={toDateTimeLocal(exportModal.endTs)}
                    onChange={(event) => setExportModal((prev) => ({ ...prev, endTs: event.target.value }))}
                    disabled={exportBusy}
                  />
                </label>
              </div>
              {exportStatus ? <div className="archiveExportStatus">{exportStatus}</div> : null}
              <div className="actions archiveExportActions">
                <button className="button primary" onClick={submitExport} disabled={exportBusy}>
                  {t.exportRun}
                </button>
                <button className="button secondary" onClick={downloadLastManifest} disabled={!lastExportId || exportBusy} title={t.exportManifestHelp}>
                  {t.exportManifest}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
      <OperationDialog dialog={deleteDialog} onClose={closeDeleteDialog} />
      <OperationToast toast={operationToast} onClose={() => setOperationToast(null)} />
      </div>
    </Layout>
  );
}
  async function buildRecordingStreamUrl(item) {
    const mediaToken = await issueRecordingMediaToken(recordingIdentityPayload(item), "stream");
    return `/api/recordings/stream?${recordingIdentityQuery(item)}&media_token=${encodeURIComponent(mediaToken)}`;
  }
