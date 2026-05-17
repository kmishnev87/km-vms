"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
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
import {
  shouldUseAdaptiveHighResolutionPlayback,
  normalizeVideoDimensions,
  selectCompactVideoRenderMode,
} from "../../lib/playbackResolution";
import { formatProductDateTime, productDateFilterParam, productDateTimeInputValue } from "../../lib/timezone";

const PAGE_SIZE = 30;
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
  exportManifest: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u043f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430",
  exportManifestHelp: "\u041f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430 \u2014 \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0444\u0430\u0439\u043b \u0434\u043b\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438, \u0441 \u043a\u0430\u043a\u043e\u0439 \u043a\u0430\u043c\u0435\u0440\u044b \u0438 \u0437\u0430 \u043a\u0430\u043a\u043e\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0441\u043e\u0437\u0434\u0430\u043d \u043a\u043b\u0438\u043f.",
  exportReady: "\u041a\u043b\u0438\u043f \u0433\u043e\u0442\u043e\u0432.",
  remove: "\u0423\u0434\u0430\u043b\u0438\u0442\u044c",
  noRecords: "\u041d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0435\u0439",
  pickAllPage: "\u0412\u044b\u0431\u0440\u0430\u0442\u044c \u0432\u0441\u0435 \u043d\u0430 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u0435",
  openEmbeddedViewer: "\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0432\u0441\u0442\u0440\u043e\u0435\u043d\u043d\u044b\u0439 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
  viewRecord: "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440 \u0437\u0430\u043f\u0438\u0441\u0438",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
  playbackError: "\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u043d\u0435 \u0441\u043c\u043e\u0433 \u0432\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0441\u0442\u0438 \u0437\u0430\u043f\u0438\u0441\u044c \u043e\u043d\u043b\u0430\u0439\u043d. \u0417\u0430\u043f\u0438\u0441\u044c \u043c\u043e\u0436\u043d\u043e \u0441\u043a\u0430\u0447\u0430\u0442\u044c.",
  missingFile: "\u0424\u0430\u0439\u043b \u043e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u0435\u0442 / \u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0430\u0440\u0445\u0438\u0432\u0430",
  unavailable: "\u041d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e",
  recordingActiveEmpty: "\u0418\u0434\u0451\u0442 \u0437\u0430\u043f\u0438\u0441\u044c. \u0417\u0430\u043f\u0438\u0441\u044c \u043f\u043e\u044f\u0432\u0438\u0442\u0441\u044f \u043f\u043e\u0441\u043b\u0435 \u0437\u0430\u043a\u0440\u044b\u0442\u0438\u044f \u0441\u0435\u0433\u043c\u0435\u043d\u0442\u0430.",
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
  return item?.available !== false && item?.playback_available !== false && item?.download_available !== false;
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
  const [selectedCamera, setSelectedCamera] = useState("__all__");
  const [selectedDate, setSelectedDate] = useState("");
  const [items, setItems] = useState([]);
  const [recordingsLoadState, setRecordingsLoadState] = useState("idle");
  const [productTimezone, setProductTimezone] = useState("UTC");
  const [selectedPaths, setSelectedPaths] = useState([]);
  const [sortBy, setSortBy] = useState(SORT_OPTIONS.created_at.key);
  const [sortDir, setSortDir] = useState("desc");
  const [currentPage, setCurrentPage] = useState(1);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
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
  const viewerFrameRef = useRef(null);
  const viewerVideoRef = useRef(null);
  const viewerRestoreStateRef = useRef(null);
  const viewerRefreshInFlightRef = useRef(false);
  const canDelete = canDeleteRecordings(currentUser);
  const canExport = canExportRecordings(currentUser);

  useEffect(() => {
    if (!canExport) return;
    getArchiveExportLimits().then(setExportLimits).catch(() => {});
  }, [canExport]);

  async function loadCameras() {
    const data = await apiFetch("/recordings/cameras");
    setCameras(data.items || []);
  }

  async function loadRecordings(camera = "__all__", dateValue = selectedDate) {
    const requestId = ++requestIdRef.current;
    setRecordingsLoadState((prev) => (prev === "loaded" || prev === "refreshing" ? "refreshing" : "loading"));
    const params = new URLSearchParams();
    if (camera && camera !== "__all__") params.set("camera", camera);
    if (dateValue) params.set("date", productDateFilterParam(dateValue));
    const query = params.toString() ? `?${params.toString()}` : "";

    try {
      const data = await apiFetch(`/recordings${query}`);
      if (requestId !== requestIdRef.current) return;

      setItems(data.items || []);
      setProductTimezone(data?.timezone?.id || "UTC");
      setSelectedPaths([]);
      setError("");
      setRecordingsLoadState("loaded");
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(normalizeRecordingError(err.message));
      setRecordingsLoadState((prev) => (prev === "loaded" || prev === "refreshing" ? "loaded" : "error"));
    }
  }

  async function loadRecorderStatus() {
    const status = await apiFetch("/system/recorder/status").catch(() => null);
    setRecorderStatus(status);
  }

  async function initialLoad() {
    try {
      setError("");
      setNotice("");
      await Promise.all([loadCameras(), loadRecorderStatus()]);
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    }
  }

  useEffect(() => {
    initialLoad();
  }, []);

  useEffect(() => {
    loadRecordings(selectedCamera, selectedDate);
  }, [selectedCamera, selectedDate]);

  useEffect(() => {
    setCurrentPage(1);
  }, [selectedCamera, selectedDate, sortBy, sortDir]);

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
      setNotice("");
      await Promise.all([loadCameras(), loadRecordings(selectedCamera, selectedDate), loadRecorderStatus()]);
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

  const filteredItems = items;
  const recordingsLoaded = recordingsLoadState === "loaded" || recordingsLoadState === "refreshing";
  const recordingsFirstLoading = recordingsLoadState === "idle" || recordingsLoadState === "loading";

  const sortedItems = useMemo(() => {
    return [...filteredItems].sort((left, right) =>
      compareValues(left, right, sortBy, sortDir)
    );
  }, [filteredItems, sortBy, sortDir]);

  const pageCount = Math.max(1, Math.ceil(sortedItems.length / PAGE_SIZE));

  useEffect(() => {
    setCurrentPage((prev) => Math.min(prev, pageCount));
  }, [pageCount]);

  const paginatedItems = useMemo(() => {
    const startIndex = (currentPage - 1) * PAGE_SIZE;
    return sortedItems.slice(startIndex, startIndex + PAGE_SIZE);
  }, [sortedItems, currentPage]);

  const visiblePaths = useMemo(
    () => paginatedItems.map((item) => item.path),
    [paginatedItems]
  );

  const allVisibleSelected = useMemo(() => {
    if (!visiblePaths.length) return false;
    return visiblePaths.every((path) => selectedPaths.includes(path));
  }, [visiblePaths, selectedPaths]);

  const visibleSummary = useMemo(() => {
    const sizeBytes = filteredItems.reduce(
      (total, item) => total + Number(item.size_bytes || 0),
      0
    );

    return {
      count: filteredItems.length,
      size_human: formatSizeBytes(sizeBytes),
    };
  }, [filteredItems]);

  const clipCameraOptions = useMemo(() => {
    const byId = new Map();
    items.forEach((item) => {
      if (!item?.camera_id) return;
      if (selectedCamera !== "__all__" && item.camera !== selectedCamera) return;
      byId.set(String(item.camera_id), item.camera || `${t.camera} ${item.camera_id}`);
    });
    return Array.from(byId.entries()).map(([id, name]) => ({ id, name }));
  }, [items, selectedCamera]);

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
      setNotice("");
      const mediaToken = await issueRecordingMediaToken(item.path, "download");
      const url = `/api/recordings/download?path=${encodeURIComponent(item.path)}&media_token=${encodeURIComponent(mediaToken)}`;
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
    const initialCameraId = selectedCamera === "__all__" ? "" : (clipCameraOptions[0]?.id || "");
    const { startTs, endTs } = defaultClipRange(selectedDate, exportLimits);
    setError("");
    setNotice("");
    setLastExportId("");
    setExportStatus("");
    setExportModal({
      cameraId: initialCameraId,
      title: selectedCamera === "__all__" ? t.createClip : `${t.createClip} ${selectedCamera}`,
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
      setNotice("");
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
      setNotice(t.exportReady);
      setExportStatus(t.exportReady);
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
      setNotice("");
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

  async function handleDeleteOne(item) {
    if (!canDelete) return;
    if (!window.confirm(`\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0437\u0430\u043f\u0438\u0441\u044c "${item.filename}"?`)) return;
    try {
      setError("");
      setNotice("");
      setBusy(true);
      const result = await apiFetch(`/recordings?path=${encodeURIComponent(item.path)}`, {
        method: "DELETE",
      });
      await refresh();
      setNotice(summarizeDeleteResult(result));
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteSelected() {
    if (!canDelete) return;
    if (!selectedPaths.length) return;
    if (!window.confirm(`\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0435 \u0437\u0430\u043f\u0438\u0441\u0438: ${selectedPaths.length} \u0448\u0442.?`)) return;

    try {
      setError("");
      setNotice("");
      setBusy(true);
      const result = await apiFetch("/recordings/bulk-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paths: selectedPaths }),
      });
      await refresh();
      setNotice(summarizeDeleteResult(result));
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteByCamera() {
    if (!canDelete) return;
    if (!selectedCamera || selectedCamera === "__all__") return;
    if (!window.confirm(`\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u043a\u0430\u043c\u0435\u0440\u044b "${selectedCamera}"?`)) return;

    try {
      setDangerMenuOpen(false);
      setError("");
      setNotice("");
      setBusy(true);
      const result = await apiFetch(`/recordings/by-camera?camera=${encodeURIComponent(selectedCamera)}`, {
        method: "DELETE",
      });
      await refresh();
      setNotice(summarizeDeleteResult(result));
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    } finally {
      setBusy(false);
    }
  }

  async function handleDeleteAll() {
    if (!canDelete) return;
    if (!window.confirm("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u043e\u043e\u0431\u0449\u0435 \u0432\u0441\u0435 \u0437\u0430\u043f\u0438\u0441\u0438 \u0432\u0441\u0435\u0445 \u043a\u0430\u043c\u0435\u0440?")) return;

    try {
      setDangerMenuOpen(false);
      setError("");
      setNotice("");
      setBusy(true);
      const result = await apiFetch("/recordings/all?confirm=true&confirmation_text=DELETE_ALL_RECORDINGS", { method: "DELETE" });
      await refresh();
      setNotice(summarizeDeleteResult(result));
    } catch (err) {
      setError(normalizeRecordingError(err.message));
    } finally {
      setBusy(false);
    }
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
      {notice ? (
        <div className="badge ok recordingsErrorBadge">
          {notice}
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
                className="button secondary small recordingsCreateClipButton"
                onClick={openExportModal}
                disabled={exportBusy}
                title={t.createClipTooltip}
                aria-label={t.createClip}
              >
                {ICONS.export}
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
              {ICONS.refresh}
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
                  {ICONS.trash}
                </button>

                <div className="recordingsDangerMenu recordingsToolbarMenu" ref={dangerMenuRef}>
                  <button
                    className="button secondary small recordingsDangerTrigger"
                    onClick={() => setDangerMenuOpen((prev) => !prev)}
                    aria-haspopup="menu"
                    aria-expanded={dangerMenuOpen}
                    title={t.dangerActions}
                  >
                    {ICONS.more}
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
                <tr key={item.path}>
                  <td>
                    {canDelete ? (
                      <input
                        type="checkbox"
                        checked={selectedPaths.includes(item.path)}
                        onChange={() => toggleSelected(item.path)}
                        aria-label={`\u0412\u044b\u0431\u0440\u0430\u0442\u044c ${item.filename}`}
                      />
                    ) : null}
                  </td>
                  <td className="recordingsCameraCell">{item.camera}</td>
                  <td className="recordingsSpacerCell" aria-hidden="true"></td>
                  <td className="recordingsFilenameCell">
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
                        <div className="recordingsMissingStatus">{t.missingFile}</div>
                      </div>
                    )}
                  </td>
                  <td className="recordingsDateCell">
                    {item.started_at_system ? formatProductDateTime(item.started_at_system, productTimezone) : (item.created_at || "-")}
                  </td>
                  <td className="recordingsSizeCell">{item.size_human}</td>
                  <td>
                    <div className="recordingsActions">
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleWatch(item)}
                        disabled={!isRecordingAvailable(item)}
                        title={`${ICONS.watch} ${t.watch}`}
                        aria-label={t.watch}
                      >
                        {ICONS.watch}
                      </button>
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleDownload(item)}
                        disabled={!isRecordingAvailable(item)}
                        title={t.downloadSource}
                        aria-label={t.downloadSource}
                      >
                        {ICONS.download}
                      </button>
                      {canDelete ? (
                        <button
                          className="recordingsIconButton danger"
                          onClick={() => handleDeleteOne(item)}
                          disabled={busy}
                          title={`${ICONS.remove} ${t.remove}`}
                          aria-label={t.remove}
                        >
                          {ICONS.remove}
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
              >
                <video
                  key={viewerUrl}
                  ref={viewerVideoRef}
                  src={viewerUrl}
                  controls
                  autoPlay
                  preload="metadata"
                  className={`recordingVideo ${viewerAdaptiveHighRes ? "recordingVideoAdaptiveHighRes" : ""}`}
                  onLoadedMetadata={restoreViewerPlaybackState}
                  onCanPlay={restoreViewerPlaybackState}
                  onError={refreshViewerUrlAfterMediaError}
                />
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
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>{t.exportTitle}</h2>
              <button className="iconCloseButton" onClick={closeExportModal} disabled={exportBusy} aria-label={t.close}>
                {ICONS.close}
              </button>
            </div>
            <div className="archiveExportForm">
              <div className="archiveExportSummary">
                <strong>{t.createClip}</strong>
                <span>{selectedDate || t.allCameras}</span>
              </div>
              <div className="archiveExportHelp">{t.exportHelp}</div>
              <div className="archiveExportLimits">
                <strong>{t.exportLimits}</strong>
                <span>{describeArchiveExportLimits(exportLimits)}</span>
              </div>
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
              {exportStatus ? <div className="archiveExportStatus">{exportStatus}</div> : null}
              <div className="actions">
                <button className="button primary" onClick={submitExport} disabled={exportBusy}>
                  {t.exportRun}
                </button>
                <button className="button secondary" onClick={downloadLastManifest} disabled={!lastExportId || exportBusy} title={t.exportManifestHelp}>
                  {t.exportManifest}
                </button>
                <button className="button secondary" onClick={closeExportModal} disabled={exportBusy}>
                  {t.close}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
      </div>
    </Layout>
  );
}
  async function buildRecordingStreamUrl(item) {
    const mediaToken = await issueRecordingMediaToken(item.path, "stream");
    return `/api/recordings/stream?path=${encodeURIComponent(item.path)}&media_token=${encodeURIComponent(mediaToken)}`;
  }
