"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch, canDeleteRecordings, issueRecordingMediaToken } from "../../lib/api";
import {
  buildArchiveExportPayload,
  canExportRecordings,
  downloadArchiveManifest,
  normalizeArchiveExportError,
  runArchiveExportWorkflow,
  saveBlobDownload,
} from "../../lib/archiveExports";
import { useCurrentUser } from "../../lib/currentUser";
import { shouldUseAdaptiveHighResolutionPlayback, normalizeVideoDimensions } from "../../lib/playbackResolution";

const PAGE_SIZE = 30;
const TEXT = {
  title: "\u0417\u0430\u043f\u0438\u0441\u0438",
  subtitle: "\u041f\u0440\u043e\u0441\u043c\u043e\u0442\u0440, \u0441\u043a\u0430\u0447\u0438\u0432\u0430\u043d\u0438\u0435 \u0438 \u0443\u0434\u0430\u043b\u0435\u043d\u0438\u0435 \u0430\u0440\u0445\u0438\u0432\u0430",
  allCameras: "\u0412\u0441\u0435 \u043a\u0430\u043c\u0435\u0440\u044b",
  date: "\u0414\u0430\u0442\u0430",
  refresh: "\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c",
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
  exportEvidence: "\u042d\u043a\u0441\u043f\u043e\u0440\u0442",
  exportTitle: "\u042d\u043a\u0441\u043f\u043e\u0440\u0442 evidence clip",
  exportReason: "\u041e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435",
  exportStart: "\u041d\u0430\u0447\u0430\u043b\u043e",
  exportEnd: "\u041a\u043e\u043d\u0435\u0446",
  exportRun: "\u0421\u043e\u0437\u0434\u0430\u0442\u044c export",
  exportManifest: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c manifest",
  exportReady: "\u042d\u043a\u0441\u043f\u043e\u0440\u0442 \u0433\u043e\u0442\u043e\u0432.",
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
  export: "\u21e9",
  remove: "\ud83d\uddd1",
  prev: "\u2190",
  next: "\u2192",
  up: "\u2191",
  down: "\u2193",
  sort: "\u2195",
  gap: "\u2026",
  close: "\u00d7",
};

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
    result = parseCreatedAt(left.created_at) - parseCreatedAt(right.created_at);
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
  const [cameras, setCameras] = useState([]);
  const [selectedCamera, setSelectedCamera] = useState("__all__");
  const [selectedDate, setSelectedDate] = useState("");
  const [items, setItems] = useState([]);
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

  const requestIdRef = useRef(0);
  const dangerMenuRef = useRef(null);
  const viewerFrameRef = useRef(null);
  const viewerVideoRef = useRef(null);
  const viewerRestoreStateRef = useRef(null);
  const viewerRefreshInFlightRef = useRef(false);
  const canDelete = canDeleteRecordings(currentUser);
  const canExport = canExportRecordings(currentUser);

  async function loadCameras() {
    const data = await apiFetch("/recordings/cameras");
    setCameras(data.items || []);
  }

  async function loadRecordings(camera = "__all__") {
    const requestId = ++requestIdRef.current;
    const query =
      camera && camera !== "__all__"
        ? `?camera=${encodeURIComponent(camera)}`
        : "";

    const data = await apiFetch(`/recordings${query}`);
    if (requestId !== requestIdRef.current) return;

    setItems(data.items || []);
    setSelectedPaths([]);
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
    loadRecordings(selectedCamera).catch((err) => setError(normalizeRecordingError(err.message)));
  }, [selectedCamera]);

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
      await Promise.all([loadCameras(), loadRecordings(selectedCamera), loadRecorderStatus()]);
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

  const filteredItems = useMemo(() => {
    return items.filter((item) => {
      if (selectedDate) {
        return formatDateInputFromCreatedAt(item.created_at) === selectedDate;
      }
      return true;
    });
  }, [items, selectedDate]);

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

  const paginationItems = useMemo(
    () => buildPageList(currentPage, pageCount),
    [currentPage, pageCount]
  );

  const activeRecordingEmpty = useMemo(
    () => !filteredItems.length && hasActiveRecordingJobs(recorderStatus, selectedCamera),
    [filteredItems.length, recorderStatus, selectedCamera]
  );

  const viewerAdaptiveHighRes = useMemo(
    () => shouldUseAdaptiveHighResolutionPlayback(viewerResolution, viewerRect, viewerFullscreen),
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
    if (!value) return "";
    const dt = new Date(value);
    if (!Number.isFinite(dt.getTime())) return "";
    const pad2 = (n) => String(n).padStart(2, "0");
    return `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}-${pad2(dt.getDate())}T${pad2(dt.getHours())}:${pad2(dt.getMinutes())}:${pad2(dt.getSeconds())}`;
  }

  function openExportModal(item) {
    if (!canExport || !isRecordingAvailable(item) || !item?.camera_id) return;
    const startTs = item.started_at || "";
    const endTs = item.ended_at || item.started_at || "";
    setError("");
    setNotice("");
    setLastExportId("");
    setExportStatus("");
    setExportModal({
      item,
      cameraId: item.camera_id,
      title: item.filename || "Evidence export",
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
        onStatus: (status, job) => {
          setExportStatus(status);
          if (job?.id) setLastExportId(job.id);
        },
      });
      if (result?.job?.id) setLastExportId(result.job.id);
      saveBlobDownload(result.clip.blob, result.clip.filename || "km-vms-evidence-export.mkv");
      setNotice(TEXT.exportReady);
      setExportStatus("done");
    } catch (err) {
      setError(normalizeArchiveExportError(err.message));
      setExportStatus("failed");
    } finally {
      setExportBusy(false);
    }
  }

  async function downloadLastManifest() {
    if (!lastExportId || exportBusy) return;
    try {
      setExportBusy(true);
      const manifest = await downloadArchiveManifest(lastExportId);
      saveBlobDownload(manifest.blob, manifest.filename || "km-vms-evidence-manifest.json");
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
          <h1 className="pageTitle">{TEXT.title}</h1>
          <div className="pageSubtitle">{TEXT.subtitle}</div>
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
              <option value="__all__">{TEXT.allCameras}</option>
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
              aria-label={TEXT.date}
            />
          </div>

          <div className="recordingsToolbar recordingsToolbarCompact">
            <button
              className="button secondary small recordingsActionButton recordingsToolbarIconButton"
              onClick={refresh}
              title={TEXT.refresh}
              aria-label={TEXT.refresh}
            >
              {ICONS.refresh}
            </button>

            {canDelete ? (
              <>
                <button
                  className="button secondary small recordingsActionButton recordingsToolbarIconButton"
                  onClick={handleDeleteSelected}
                  disabled={!selectedPaths.length || busy}
                  title={TEXT.deleteSelected}
                  aria-label={TEXT.deleteSelected}
                >
                  {ICONS.trash}
                </button>

                <div className="recordingsDangerMenu recordingsToolbarMenu" ref={dangerMenuRef}>
                  <button
                    className="button secondary small recordingsDangerTrigger"
                    onClick={() => setDangerMenuOpen((prev) => !prev)}
                    aria-haspopup="menu"
                    aria-expanded={dangerMenuOpen}
                    title={TEXT.dangerActions}
                  >
                    {ICONS.more}
                  </button>

                  {dangerMenuOpen ? (
                    <div className="recordingsDangerDropdown" role="menu">
                      <div className="recordingsDangerTitle">{TEXT.dangerActions}</div>
                      <button
                        className="recordingsDangerItem"
                        onClick={handleDeleteByCamera}
                        disabled={selectedCamera === "__all__" || busy}
                        role="menuitem"
                      >
                        {TEXT.deleteCamera}
                      </button>
                      <button
                        className="recordingsDangerItem recordingsDangerItemAlert"
                        onClick={handleDeleteAll}
                        disabled={busy}
                        role="menuitem"
                      >
                        {TEXT.deleteAll}
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
        <div className="badge">{TEXT.totalFiles}: {visibleSummary.count}</div>
        <div className="badge">{TEXT.totalSize}: {visibleSummary.size_human}</div>
        <div className="badge">{TEXT.page}: {currentPage} / {pageCount}</div>
        {selectedPaths.length ? (
          <div className="badge ok">{TEXT.selected}: {selectedPaths.length}</div>
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
                      aria-label={TEXT.pickAllPage}
                    />
                  ) : null}
                </th>
                <th>{TEXT.camera}</th>
                <th className="recordingsSpacerHeader" aria-hidden="true"></th>
                <th>{TEXT.file}</th>
                <th className="recordingsDateHeader">{TEXT.createdAt}</th>
                <th className="recordingsSizeHeader">
                  <button
                    className={`recordingsSortButton ${sortBy === SORT_OPTIONS.size_bytes.key ? "active" : ""}`}
                    onClick={() => handleSort(SORT_OPTIONS.size_bytes.key)}
                  >
                    <span className="recordingsSortLabel">{TEXT.size}</span>
                    <span className="recordingsSortIcon">{sortBy === SORT_OPTIONS.size_bytes.key ? (sortDir === "asc" ? ICONS.up : ICONS.down) : ICONS.sort}</span>
                  </button>
                </th>
                <th className="recordingsActionsHeader">{TEXT.actions}</th>
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
                        title={TEXT.openEmbeddedViewer}
                      >
                        {item.filename}
                      </button>
                    ) : (
                      <div>
                        <div>{item.filename}</div>
                        <div className="recordingsMissingStatus">{TEXT.missingFile}</div>
                      </div>
                    )}
                  </td>
                  <td className="recordingsDateCell">{item.created_at || "-"}</td>
                  <td className="recordingsSizeCell">{item.size_human}</td>
                  <td>
                    <div className="recordingsActions">
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleWatch(item)}
                        disabled={!isRecordingAvailable(item)}
                        title={`${ICONS.watch} ${TEXT.watch}`}
                        aria-label={TEXT.watch}
                      >
                        {ICONS.watch}
                      </button>
                      <button
                        className="recordingsIconButton"
                        onClick={() => handleDownload(item)}
                        disabled={!isRecordingAvailable(item)}
                        title={`${ICONS.download} ${TEXT.download}`}
                        aria-label={TEXT.download}
                      >
                        {ICONS.download}
                      </button>
                      {canExport ? (
                        <button
                          className="recordingsIconButton"
                          onClick={() => openExportModal(item)}
                          disabled={!isRecordingAvailable(item) || busy || exportBusy || !item.camera_id}
                          title={`${ICONS.export} ${TEXT.exportEvidence}`}
                          aria-label={TEXT.exportEvidence}
                        >
                          {ICONS.export}
                        </button>
                      ) : null}
                      {canDelete ? (
                        <button
                          className="recordingsIconButton danger"
                          onClick={() => handleDeleteOne(item)}
                          disabled={busy}
                          title={`${ICONS.remove} ${TEXT.remove}`}
                          aria-label={TEXT.remove}
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
                    {activeRecordingEmpty ? TEXT.recordingActiveEmpty : TEXT.noRecords}
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
              <h2 style={{ margin: 0 }}>{TEXT.viewRecord}</h2>
              <button
                className="iconCloseButton"
                onClick={closeViewer}
                aria-label={TEXT.close}
              >
                {ICONS.close}
              </button>
            </div>

            <div style={{ marginBottom: 14, color: "#475569" }}>{viewerTitle}</div>

            {viewerPlaybackError ? (
              <div className="recordingPlaybackNotice">
                {TEXT.playbackError}
              </div>
            ) : (
              <div
                ref={viewerFrameRef}
                className={`recordingVideoFrame ${viewerAdaptiveHighRes ? "adaptiveHighRes" : ""}`}
                data-highres-adaptive={viewerAdaptiveHighRes ? "true" : "false"}
                data-natural-resolution={`${viewerResolution.width}x${viewerResolution.height}`}
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
                  {TEXT.download}
                </button>
              ) : null}
              <button className="button secondary" onClick={closeViewer}>
                {TEXT.close}
              </button>
            </div>
          </div>
        </div>
      ) : null}
      {exportModal ? (
        <div className="modalBackdrop">
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>{TEXT.exportTitle}</h2>
              <button className="iconCloseButton" onClick={closeExportModal} disabled={exportBusy} aria-label={TEXT.close}>
                {ICONS.close}
              </button>
            </div>
            <div className="archiveExportForm">
              <div className="archiveExportSummary">
                <strong>{exportModal.item?.camera || TEXT.camera}</strong>
                <span>{exportModal.item?.filename}</span>
              </div>
              <label className="archiveExportField">
                <span>{TEXT.exportStart}</span>
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
                <span>{TEXT.exportEnd}</span>
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
                <span>{TEXT.exportReason}</span>
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
                  {TEXT.exportRun}
                </button>
                <button className="button secondary" onClick={downloadLastManifest} disabled={!lastExportId || exportBusy || exportStatus !== "done"}>
                  {TEXT.exportManifest}
                </button>
                <button className="button secondary" onClick={closeExportModal} disabled={exportBusy}>
                  {TEXT.close}
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
