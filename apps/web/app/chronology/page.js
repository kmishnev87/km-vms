"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import ArchiveTilePlayer from "../../components/ArchiveTilePlayer";
import ChronologyTimeline from "../../components/ChronologyTimeline";
import { apiFetch } from "../../lib/api";
import {
  buildArchiveExportPayload,
  canExportRecordings,
  describeArchiveExportLimits,
  downloadArchiveManifest,
  getArchiveExportLimits,
  normalizeChronologyDownloadError,
  normalizeArchiveExportError,
  runArchiveExportWorkflow,
  saveBlobDownload,
  startChronologyCurrentRecordingDownload,
  validateArchiveExportSelection,
} from "../../lib/archiveExports";
import { productLocalInputToApi } from "../../lib/timezone";
import { resizeWorkspaceTile, visibleWorkspaceTiles, workspaceCameraIds } from "../../lib/workspaceLayoutCore";

const STORAGE_KEY = "vms_chronology_workspace_v1";
const LEGACY_STORAGE_KEY = "vms_chronology" + "2_workspace_v1";
const WORKSPACE_KEY = "chronology";
const MIGRATION_MARKER_PREFIX = `${STORAGE_KEY}_backend_migrated`;
const LEGACY_CANVAS_W = 1600;
const LEGACY_CANVAS_H = 900;
const MIN_TILE_W = 220;
const MIN_TILE_H = 150;
const DEFAULT_W_PCT = 0.32;
const DEFAULT_H_PCT = 0.34;
const ZOOM_KEYS = ["24h", "3d", "7d"];
const ZOOM_HOURS = {
  "24h": 24,
  "3d": 72,
  "7d": 168,
};

const SPEED_OPTIONS = [
  { value: 0.25, label: "0.25x" },
  { value: 0.5, label: "0.5x" },
  { value: 1, label: "1x" },
  { value: 2, label: "2x" },
  { value: 4, label: "4x" },
];

const TEXT = {
  cameras: "\u041a\u0430\u043c\u0435\u0440\u044b",
  align: "\u0412\u044b\u0440\u043e\u0432\u043d\u044f\u0442\u044c",
  addAll: "\u0412\u0441\u0435",
  loadError: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043a\u0430\u043c\u0435\u0440\u044b",
  empty: "\u041f\u0435\u0440\u0435\u0442\u0430\u0449\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443 \u043d\u0430 workspace",
  camera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
  enterFullscreen: "\u0412\u043e \u0432\u0435\u0441\u044c \u044d\u043a\u0440\u0430\u043d",
  exitFullscreen: "\u0412\u044b\u0439\u0442\u0438 \u0438\u0437 fullscreen",
  fullscreenControls: "\u0423\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435",
  hideFullscreenControls: "\u0421\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c",
  showFullscreenControls: "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c",
  noArchiveAtTime: "\u041d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0438 \u043d\u0430 \u044d\u0442\u043e \u0432\u0440\u0435\u043c\u044f",
  archiveReady: "\u0410\u0440\u0445\u0438\u0432 \u043d\u0430\u0439\u0434\u0435\u043d",
  timeContext: "\u0412\u0440\u0435\u043c\u044f",
  rangeContext: "\u0414\u0438\u0430\u043f\u0430\u0437\u043e\u043d",
  play: "\u041f\u0443\u0441\u043a",
  pause: "\u041f\u0430\u0443\u0437\u0430",
  back10: "\u041d\u0430 10 \u0441\u0435\u043a\u0443\u043d\u0434 \u043d\u0430\u0437\u0430\u0434",
  forward10: "\u041d\u0430 10 \u0441\u0435\u043a\u0443\u043d\u0434 \u0432\u043f\u0435\u0440\u0435\u0434",
  collapseSidebar: "\u0421\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c \u043a\u0430\u043c\u0435\u0440",
  expandSidebar: "\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c \u043a\u0430\u043c\u0435\u0440",
  missing: "\u041a\u0430\u043c\u0435\u0440\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430",
  resize: "\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0440\u0430\u0437\u043c\u0435\u0440",
  find: "\u041d\u0430\u0439\u0442\u0438",
  currentTime: "\u0412\u044b\u0431\u0440\u0430\u043d\u043e\u0435 \u0432\u0440\u0435\u043c\u044f",
  previewTime: "\u041f\u0440\u0435\u0434\u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
  duplicate: "\u041a\u0430\u043c\u0435\u0440\u0430 \u0443\u0436\u0435 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0430",
  quickDownload: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u0442\u0435\u043a\u0443\u0449\u0443\u044e \u0437\u0430\u043f\u0438\u0441\u044c",
  quickDownloadHelp: "\u0421\u043a\u0430\u0447\u0438\u0432\u0430\u0435\u0442 \u0438\u0441\u0445\u043e\u0434\u043d\u044b\u0439 \u0430\u0440\u0445\u0438\u0432\u043d\u044b\u0439 \u0444\u0430\u0439\u043b \u0434\u043b\u044f \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u043e\u0439 \u043a\u0430\u043c\u0435\u0440\u044b \u0438 \u0442\u0435\u043a\u0443\u0449\u0435\u0433\u043e \u0432\u0440\u0435\u043c\u0435\u043d\u0438.",
  quickDownloadChooseHelp: "\u041e\u0442\u043a\u0440\u043e\u0435\u0442 \u0432\u044b\u0431\u043e\u0440 \u043a\u0430\u043c\u0435\u0440\u044b \u0434\u043b\u044f \u0441\u043a\u0430\u0447\u0438\u0432\u0430\u043d\u0438\u044f \u0438\u0441\u0445\u043e\u0434\u043d\u043e\u0439 \u0437\u0430\u043f\u0438\u0441\u0438 \u0432 \u0442\u0435\u043a\u0443\u0449\u0435\u0435 \u0432\u0440\u0435\u043c\u044f.",
  quickDownloadReady: "\u0421\u043a\u0430\u0447\u0438\u0432\u0430\u043d\u0438\u0435 \u0442\u0435\u043a\u0443\u0449\u0435\u0439 \u0437\u0430\u043f\u0438\u0441\u0438 \u043d\u0430\u0447\u0430\u0442\u043e.",
  quickDownloadChoose: "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443",
  allCameras: "\u0412\u0441\u0435 \u043a\u0430\u043c\u0435\u0440\u044b",
  noRecordingShort: "\u043d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0438",
  downloadStartedShort: "\u0441\u0442\u0430\u0440\u0442",
  downloadFailedShort: "\u043e\u0448\u0438\u0431\u043a\u0430",
  exportEvidence: "\u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043a\u043b\u0438\u043f",
  exportEvidenceHelpShort: "\u0421\u043e\u0437\u0434\u0430\u0435\u0442 \u0432\u0438\u0434\u0435\u043e\u043a\u043b\u0438\u043f \u0437\u0430 \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0438\u0437 \u0430\u0440\u0445\u0438\u0432\u0430.",
  exportTitle: "\u0421\u043e\u0437\u0434\u0430\u043d\u0438\u0435 \u043a\u043b\u0438\u043f\u0430",
  exportHelp: "\u041a\u043b\u0438\u043f \u0441\u043e\u0437\u0434\u0430\u0435\u0442\u0441\u044f \u0437\u0430 \u0432\u044b\u0431\u0440\u0430\u043d\u043d\u044b\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0438\u0437 \u0430\u0440\u0445\u0438\u0432\u0430. \u041f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430 \u043f\u043e\u043c\u043e\u0433\u0430\u0435\u0442 \u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c \u043a\u0430\u043c\u0435\u0440\u0443, \u0432\u0440\u0435\u043c\u044f \u0438 \u0446\u0435\u043b\u043e\u0441\u0442\u043d\u043e\u0441\u0442\u044c \u0444\u0430\u0439\u043b\u0430.",
  exportLimits: "\u041b\u0438\u043c\u0438\u0442\u044b",
  exportCamera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  exportPickCamera: "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443",
  exportStart: "\u041d\u0430\u0447\u0430\u043b\u043e",
  exportEnd: "\u041a\u043e\u043d\u0435\u0446",
  exportReason: "\u041e\u0441\u043d\u043e\u0432\u0430\u043d\u0438\u0435",
  exportRun: "\u0421\u043e\u0437\u0434\u0430\u0442\u044c \u043a\u043b\u0438\u043f",
  exportManifest: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u043f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430",
  exportManifestHelp: "\u041f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430 \u2014 \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0444\u0430\u0439\u043b \u0434\u043b\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438, \u0441 \u043a\u0430\u043a\u043e\u0439 \u043a\u0430\u043c\u0435\u0440\u044b \u0438 \u0437\u0430 \u043a\u0430\u043a\u043e\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0441\u043e\u0437\u0434\u0430\u043d \u043a\u043b\u0438\u043f.",
  exportReady: "\u041a\u043b\u0438\u043f \u0433\u043e\u0442\u043e\u0432.",
};

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function getNow() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours(), now.getMinutes(), now.getSeconds());
}

function formatPlaybackDateTime(dt) {
  if (!dt) return "--";
  return `${pad(dt.getDate())}.${pad(dt.getMonth() + 1)}.${dt.getFullYear()} ${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`;
}

function formatLocalNaiveTs(dt) {
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`;
}

function formatProductTimestampParam(dt) {
  return productLocalInputToApi(formatLocalNaiveTs(dt));
}

function dateTimeFromDate(dt) {
  return {
    date: `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`,
    time: `${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}`,
  };
}

function normalizeTile(tile) {
  const hasPercent =
    Number.isFinite(tile?.xPct) &&
    Number.isFinite(tile?.yPct) &&
    Number.isFinite(tile?.wPct) &&
    Number.isFinite(tile?.hPct);

  if (hasPercent) {
    const wPct = clamp(Number(tile.wPct), 0.05, 1);
    const hPct = clamp(Number(tile.hPct), 0.05, 1);
    return {
      id: String(tile.id || `${tile.cameraId || "camera"}-${Date.now()}`),
      cameraId: String(tile.cameraId || ""),
      xPct: clamp(Number(tile.xPct), 0, 1 - wPct),
      yPct: clamp(Number(tile.yPct), 0, 1 - hPct),
      wPct,
      hPct,
      z: Number(tile.z || 2),
    };
  }

  const wPct = clamp(Number(tile?.w || 520) / LEGACY_CANVAS_W, 0.05, 1);
  const hPct = clamp(Number(tile?.h || 300) / LEGACY_CANVAS_H, 0.05, 1);
  return {
    id: String(tile?.id || `${tile?.cameraId || "camera"}-${Date.now()}`),
    cameraId: String(tile?.cameraId || ""),
    xPct: clamp(Number(tile?.x || 0) / LEGACY_CANVAS_W, 0, 1 - wPct),
    yPct: clamp(Number(tile?.y || 0) / LEGACY_CANVAS_H, 0, 1 - hPct),
    wPct,
    hPct,
    z: Number(tile?.z || 2),
  };
}

function readSavedTiles() {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY) || localStorage.getItem(LEGACY_STORAGE_KEY) || "[]";
    const parsed = JSON.parse(raw);
    const tiles = Array.isArray(parsed) ? dedupeTiles(parsed.map(normalizeTile)) : [];
    if (!localStorage.getItem(STORAGE_KEY) && localStorage.getItem(LEGACY_STORAGE_KEY)) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(tiles));
    }
    return tiles;
  } catch {
    return [];
  }
}

function migrationMarkerKey(userId) {
  return `${MIGRATION_MARKER_PREFIX}_${String(userId || "anonymous")}`;
}

function saveTiles(tiles) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(dedupeTiles(tiles.map(normalizeTile))));
  } catch {}
}

function dedupeTiles(tiles) {
  const seen = new Set();
  return tiles.filter((tile) => {
    const cameraId = String(tile?.cameraId || "");
    if (!cameraId) return true;
    if (seen.has(cameraId)) return false;
    seen.add(cameraId);
    return true;
  });
}

function nextZIndex(tiles) {
  return tiles.reduce((max, tile) => Math.max(max, Number(tile.z || 2)), 2) + 1;
}

function chooseAutoGrid(count, workspaceW, workspaceH) {
  if (count <= 1) return { cols: 1, rows: 1 };

  let best = { cols: count, rows: 1, score: -Infinity };
  const workspaceRatio = workspaceW / Math.max(workspaceH, 1);

  for (let cols = 1; cols <= count; cols += 1) {
    const rows = Math.ceil(count / cols);
    const cellRatio = (workspaceRatio * rows) / cols;
    const areaScore = 1 / (cols * rows);
    const shapePenalty = Math.abs(Math.log(Math.max(cellRatio, 0.01) / (16 / 9)));
    const emptyPenalty = (cols * rows - count) * 0.08;
    const score = areaScore - shapePenalty * 0.03 - emptyPenalty;

    if (score > best.score) {
      best = { cols, rows, score };
    }
  }

  return best;
}

function layoutTiles(source, workspaceBounds) {
  const tiles = dedupeTiles(source);
  if (!tiles.length) return tiles;

  const workspaceW = workspaceBounds?.width || 16;
  const workspaceH = workspaceBounds?.height || 9;
  const { cols, rows } = chooseAutoGrid(tiles.length, workspaceW, workspaceH);
  const wPct = 1 / cols;
  const hPct = 1 / rows;

  return tiles.map((tile, idx) =>
    normalizeTile({
      ...tile,
      xPct: (idx % cols) * wPct,
      yPct: Math.floor(idx / cols) * hPct,
      wPct,
      hPct,
      z: idx + 2,
    })
  );
}

function backendPayload(tiles) {
  return {
    layout_version: 1,
    tiles: dedupeTiles(tiles.map(normalizeTile)),
  };
}

export default function ChronologyPage() {
  const initialTs = getNow();
  const initialForm = dateTimeFromDate(initialTs);
  const workspaceRef = useRef(null);
  const hydratedRef = useRef(false);
  const backendReadyRef = useRef(false);
  const saveTimerRef = useRef(null);
  const lastBackendLayoutPayloadRef = useRef("");
  const currentTsRef = useRef(initialTs);
  const tilesRef = useRef([]);
  const playbackMapRef = useRef({});
  const isScrubbingRef = useRef(false);
  const playbackRequestIdRef = useRef(0);
  const rangesRequestIdRef = useRef(0);
  const loadedRangesWindowRef = useRef(null);
  const seekActionIdRef = useRef(0);
  const activeSeekActionRef = useRef(null);
  const playWasActiveRef = useRef(false);
  const shellRef = useRef(null);
  const tileRefs = useRef(new Map());
  const tileVideoRefs = useRef(new Map());
  const fullscreenControlsTimerRef = useRef(null);
  const downloadChooserRef = useRef(null);

  const [cameras, setCameras] = useState([]);
  const [camerasLoaded, setCamerasLoaded] = useState(false);
  const [tiles, setTiles] = useState([]);
  const [error, setError] = useState("");
  const [dragState, setDragState] = useState(null);
  const [resizeState, setResizeState] = useState(null);
  const [currentTs, setCurrentTs] = useState(initialTs);
  const [previewTs, setPreviewTs] = useState(initialTs);
  const [date, setDate] = useState(initialForm.date);
  const [time, setTime] = useState(initialForm.time);
  const [speed, setSpeed] = useState(1);
  const [isPlaying, setIsPlaying] = useState(false);
  const [zoomKey, setZoomKey] = useState("24h");
  const [playbackMap, setPlaybackMap] = useState({});
  const [rangesData, setRangesData] = useState({});
  const [rangesLoading, setRangesLoading] = useState(false);
  const [rangesError, setRangesError] = useState(false);
  const [isTimelinePreviewing, setIsTimelinePreviewing] = useState(false);
  const [fullscreenTileId, setFullscreenTileId] = useState(null);
  const [fullscreenControlsVisible, setFullscreenControlsVisible] = useState(true);
  const [isSystemFullscreen, setIsSystemFullscreen] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(true);
  const [currentUser, setCurrentUser] = useState(null);
  const [exportModal, setExportModal] = useState(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportStatus, setExportStatus] = useState("");
  const [lastExportId, setLastExportId] = useState("");
  const [exportLimits, setExportLimits] = useState(null);
  const [quickDownloadBusy, setQuickDownloadBusy] = useState(false);
  const [downloadChooserOpen, setDownloadChooserOpen] = useState(false);
  const [downloadResults, setDownloadResults] = useState([]);

  async function loadCameras() {
    try {
      setError("");
      const data = await apiFetch("/viewer/cameras");
      setCameras(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(err.message || TEXT.loadError);
    } finally {
      setCamerasLoaded(true);
    }
  }

  async function loadWorkspaceLayout() {
    try {
      const [user, layout] = await Promise.all([
        apiFetch("/users/me"),
        apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`),
      ]);
      setCurrentUser(user);
      backendReadyRef.current = true;
      const backendTiles = Array.isArray(layout?.tiles) ? dedupeTiles(layout.tiles.map(normalizeTile)) : [];
      const markerKey = migrationMarkerKey(user?.id);
      lastBackendLayoutPayloadRef.current = JSON.stringify(backendPayload(backendTiles));

      if (backendTiles.length) {
        setTiles(backendTiles);
        saveTiles(backendTiles);
        localStorage.setItem(markerKey, "1");
        return;
      }

      const localTiles = readSavedTiles();
      const shouldMigrate = localTiles.length && !localStorage.getItem(markerKey);
      if (shouldMigrate) {
        setTiles(localTiles);
        saveTiles(localTiles);
        const payload = backendPayload(localTiles);
        await apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        lastBackendLayoutPayloadRef.current = JSON.stringify(payload);
      } else {
        setTiles([]);
      }
      localStorage.setItem(markerKey, "1");
    } catch (err) {
      backendReadyRef.current = false;
      setTiles(readSavedTiles());
      setError((prev) => prev || err.message || TEXT.loadError);
    } finally {
      hydratedRef.current = true;
    }
  }

  useEffect(() => {
    loadWorkspaceLayout();
    loadCameras();
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (!hydratedRef.current) return;
    saveTiles(tiles);
    if (!backendReadyRef.current) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(async () => {
      try {
        const payload = backendPayload(tiles);
        const payloadText = JSON.stringify(payload);
        if (payloadText === lastBackendLayoutPayloadRef.current) return;
        await apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: payloadText,
        });
        lastBackendLayoutPayloadRef.current = payloadText;
      } catch (err) {
        setError((prev) => prev || err.message || TEXT.loadError);
      }
    }, 500);
  }, [tiles]);

  useEffect(() => {
    currentTsRef.current = currentTs;
  }, [currentTs]);

  useEffect(() => {
    playbackMapRef.current = playbackMap;
  }, [playbackMap]);

  useEffect(() => {
    if (!isPlaying || !currentTsRef.current) return undefined;

    const timer = setInterval(() => {
      setCurrentTs((prev) => (prev ? new Date(prev.getTime() + 1000 * Number(speed || 1)) : prev));
    }, 1000);

    return () => clearInterval(timer);
  }, [isPlaying, speed]);

  useEffect(() => {
    if (!isScrubbingRef.current) {
      setPreviewTs(currentTs);
    }
  }, [currentTs]);

  const cameraMap = useMemo(() => {
    const map = new Map();
    cameras.forEach((camera) => map.set(String(camera.id), camera));
    return map;
  }, [cameras]);

  const visibleTiles = useMemo(() => {
    if (!camerasLoaded) return tiles;
    return visibleWorkspaceTiles(tiles, cameras);
  }, [tiles, cameras, camerasLoaded]);

  function activeLayoutTiles(source) {
    return camerasLoaded ? visibleWorkspaceTiles(source, cameras) : dedupeTiles(source);
  }

  const selectedCameraIds = useMemo(() => (
    visibleTiles
      .map((tile) => String(tile.cameraId || ""))
      .filter(Boolean)
      .filter((value, index, arr) => arr.indexOf(value) === index)
  ), [visibleTiles]);

  const selectedCameraNames = useMemo(() => {
    const result = {};
    selectedCameraIds.forEach((id) => {
      const camera = cameraMap.get(String(id));
      result[String(id)] = camera?.name || `${TEXT.camera} ${id}`;
    });
    return result;
  }, [selectedCameraIds, cameraMap]);

  const selectedCameraKey = selectedCameraIds.join(",");
  const tileSourceKey = visibleTiles.map((tile) => `${tile.id}:${tile.cameraId}`).join("|");
  const timelineTs = previewTs || currentTs;
  const canExport = canExportRecordings(currentUser);

  useEffect(() => {
    if (!canExport) return;
    getArchiveExportLimits().then(setExportLimits).catch(() => {});
  }, [canExport]);

  useEffect(() => {
    if (!downloadChooserOpen) return undefined;
    function handlePointerDown(event) {
      if (!downloadChooserRef.current?.contains(event.target)) {
        setDownloadChooserOpen(false);
      }
    }
    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setDownloadChooserOpen(false);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [downloadChooserOpen]);

  useEffect(() => {
    setDownloadChooserOpen(false);
  }, [selectedCameraKey, tileSourceKey, date, time]);

  useEffect(() => {
    setDownloadChooserOpen(false);
  }, [timelineTs?.getTime?.()]);

  useEffect(() => {
    tilesRef.current = visibleTiles;
  }, [visibleTiles]);

  function workspaceBounds() {
    return workspaceRef.current?.getBoundingClientRect() || null;
  }

  function normalizeTargetTs() {
    const normalizedDate = date || dateTimeFromDate(getNow()).date;
    const normalizedTime = time && time.trim() ? time.trim() : "00:00:00";
    const finalTime = normalizedTime.length === 5 ? `${normalizedTime}:00` : normalizedTime;
    return `${normalizedDate}T${finalTime}`;
  }

  function syncFormDateTime(nextDate) {
    const next = dateTimeFromDate(nextDate);
    setDate(next.date);
    setTime(next.time);
  }

  function commitCurrentTimestamp(nextDate) {
    setCurrentTs(nextDate);
    setPreviewTs(nextDate);
    syncFormDateTime(nextDate);
  }

  function invalidateLoadedRangesWindow() {
    loadedRangesWindowRef.current = null;
  }

  function startSeekAction() {
    seekActionIdRef.current += 1;
    const action = {
      id: seekActionIdRef.current,
      shouldResume: isPlaying,
    };
    activeSeekActionRef.current = action;
    return action;
  }

  function invalidateSeekActions() {
    seekActionIdRef.current += 1;
    activeSeekActionRef.current = null;
  }

  function addTile(cameraId, clientX, clientY) {
    const bounds = workspaceBounds();
    if (!bounds) return;
    const normalizedCameraId = String(cameraId || "");

    const minWPct = MIN_TILE_W / Math.max(bounds.width, 1);
    const minHPct = MIN_TILE_H / Math.max(bounds.height, 1);
    const wPct = clamp(DEFAULT_W_PCT, minWPct, 0.95);
    const hPct = clamp(DEFAULT_H_PCT, minHPct, 0.95);
    const xPct = clamp((clientX - bounds.left) / bounds.width - wPct / 2, 0, 1 - wPct);
    const yPct = clamp((clientY - bounds.top) / bounds.height - 0.03, 0, 1 - hPct);

    setTiles((prev) => {
      const active = activeLayoutTiles(prev);
      if (active.some((tile) => String(tile.cameraId || "") === normalizedCameraId)) {
        setError(TEXT.duplicate);
        return prev;
      }
      setError("");
      return [
        ...prev,
        {
          id: `${cameraId}-${Date.now()}`,
          cameraId: normalizedCameraId,
          xPct,
          yPct,
          wPct,
          hPct,
          z: nextZIndex(prev),
        },
      ];
    });
  }

  function updateTile(tileId, patch) {
    setTiles((prev) =>
      dedupeTiles(prev.map((tile) => (tile.id === tileId ? normalizeTile({ ...tile, ...patch }) : tile)))
    );
  }

  function removeTile(tileId) {
    setTiles((prev) => prev.filter((tile) => tile.id !== tileId));
    setFullscreenTileId((prev) => (prev === tileId ? null : prev));
    setPlaybackMap((prev) => {
      const next = { ...prev };
      delete next[tileId];
      return next;
    });
  }

  function bringToFront(tileId) {
    setTiles((prev) => {
      const current = prev.find((tile) => tile.id === tileId);
      if (!current) return prev;
      const z = nextZIndex(prev);
      if (Number(current.z || 2) === z - 1) return prev;
      return prev.map((tile) => (tile.id === tileId ? { ...tile, z } : tile));
    });
  }

  function autoLayoutTiles() {
    setTiles((prev) => layoutTiles(activeLayoutTiles(prev), workspaceBounds()));
  }

  function addAllCameras() {
    setTiles((prev) => {
      const active = activeLayoutTiles(prev);
      const existing = workspaceCameraIds(active);
      const additions = cameras
        .filter((camera) => !existing.has(String(camera.id)))
        .map((camera, idx) =>
          normalizeTile({
            id: `${camera.id}-${Date.now()}-${idx}`,
            cameraId: String(camera.id),
            xPct: 0,
            yPct: 0,
            wPct: DEFAULT_W_PCT,
            hPct: DEFAULT_H_PCT,
            z: nextZIndex(active) + idx,
          })
        );
      if (!additions.length) return prev;
      setError("");
      return layoutTiles([...active, ...additions], workspaceBounds());
    });
  }

  function handleDrop(event) {
    event.preventDefault();
    const cameraId = event.dataTransfer.getData("application/x-chronology-camera-id");
    if (cameraId) addTile(cameraId, event.clientX, event.clientY);
  }

  function startMove(event, tile) {
    if (fullscreenTileId) return;
    if (event.button !== 0) return;
    const bounds = workspaceBounds();
    if (!bounds) return;

    event.preventDefault();
    bringToFront(tile.id);
    setDragState({
      id: tile.id,
      startX: event.clientX,
      startY: event.clientY,
      tileX: tile.xPct,
      tileY: tile.yPct,
      tileW: tile.wPct,
      tileH: tile.hPct,
      workspaceW: bounds.width,
      workspaceH: bounds.height,
    });
  }

  function startResize(event, tile, corner = "bottom-right") {
    if (fullscreenTileId) return;
    if (event.button !== 0) return;
    const bounds = workspaceBounds();
    if (!bounds) return;

    event.preventDefault();
    event.stopPropagation();
    bringToFront(tile.id);
    setResizeState({
      id: tile.id,
      startX: event.clientX,
      startY: event.clientY,
      tileX: tile.xPct,
      tileY: tile.yPct,
      tileW: tile.wPct,
      tileH: tile.hPct,
      workspaceW: bounds.width,
      workspaceH: bounds.height,
      minWPct: MIN_TILE_W / Math.max(bounds.width, 1),
      minHPct: MIN_TILE_H / Math.max(bounds.height, 1),
      corner,
    });
  }

  async function enterTileFullscreen(tileId) {
    setIsSystemFullscreen(false);
    setFullscreenTileId(tileId);
    setFullscreenControlsVisible(true);
    const fullscreenEl = tileVideoRefs.current.get(tileId) || tileRefs.current.get(tileId);
    if (!fullscreenEl || document.fullscreenElement === fullscreenEl) return;

    try {
      await fullscreenEl.requestFullscreen?.();
    } catch (_) {}
  }

  async function exitTileFullscreen() {
    setFullscreenTileId(null);
    setFullscreenControlsVisible(true);
    if (document.fullscreenElement && document.fullscreenElement !== shellRef.current) {
      try {
        await document.exitFullscreen?.();
      } catch (_) {}
    }
  }

  useEffect(() => {
    if (!dragState) return undefined;

    function onMove(event) {
      const nextX = clamp(
        dragState.tileX + (event.clientX - dragState.startX) / dragState.workspaceW,
        0,
        1 - dragState.tileW
      );
      const nextY = clamp(
        dragState.tileY + (event.clientY - dragState.startY) / dragState.workspaceH,
        0,
        1 - dragState.tileH
      );
      updateTile(dragState.id, { xPct: nextX, yPct: nextY });
    }

    function onUp() {
      setDragState(null);
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragState]);

  useEffect(() => {
    if (!resizeState) return undefined;

    function onMove(event) {
      updateTile(resizeState.id, resizeWorkspaceTile(null, resizeState, event));
    }

    function onUp() {
      setResizeState(null);
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [resizeState]);

  async function fetchPlaybackForTile(tile, ts, forceReload = false) {
    const prev = playbackMapRef.current[tile.id];

    if (!tile.cameraId) {
      return {
        hasVideo: false,
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey: forceReload || !prev ? `${tile.id}-empty-${Date.now()}` : prev.playbackKey,
      };
    }

    try {
      const response = await apiFetch(
        `/chronology/playback?camera_id=${encodeURIComponent(tile.cameraId)}&ts=${encodeURIComponent(ts)}`
      );

      const hasVideo = Boolean(response?.has_video);
      const relPath = hasVideo ? (response?.rel_path || null) : null;
      const offsetSec = hasVideo ? Number(response?.offset_sec || 0) : 0;
      const cameraId = hasVideo ? String(tile.cameraId) : null;
      const containerFormat = hasVideo ? String(response?.container_format || "").toLowerCase() : "";
      const fileExtension = hasVideo ? String(response?.file_extension || "").toLowerCase() : "";
      const mimeType = hasVideo ? String(response?.mime_type || "").toLowerCase() : "";

      const sameSource =
        !forceReload &&
        prev &&
        Boolean(prev.hasVideo) === hasVideo &&
        String(prev.cameraId || "") === String(cameraId || "") &&
        String(prev.relPath || "") === String(relPath || "");

      return {
        hasVideo,
        cameraId,
        relPath,
        offsetSec,
        containerFormat,
        fileExtension,
        mimeType,
        playbackKey: sameSource
          ? prev.playbackKey
          : `${tile.id}-${cameraId || "empty"}-${relPath || "empty"}-${offsetSec}-${containerFormat || fileExtension || "unknown"}-${Date.now()}`,
      };
    } catch (_) {
      return {
        hasVideo: false,
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey: `${tile.id}-error-${Date.now()}`,
      };
    }
  }

  async function resolvePlaybackForTimestamp(ts, forceReload = false) {
    const requestId = ++playbackRequestIdRef.current;
    const sourceTiles = tilesRef.current;
    const results = await Promise.all(
      sourceTiles.map(async (tile) => [tile.id, await fetchPlaybackForTile(tile, ts, forceReload)])
    );

    if (requestId !== playbackRequestIdRef.current) {
      return { applied: false };
    }

    const nextMap = {};
    results.forEach(([tileId, value]) => {
      nextMap[tileId] = value;
    });
    setPlaybackMap(nextMap);
    return { applied: true };
  }

  async function handleFind() {
    invalidateSeekActions();
    const targetDate = new Date(normalizeTargetTs());

    setIsPlaying(false);
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(targetDate);
    await resolvePlaybackForTimestamp(formatProductTimestampParam(targetDate), true);
  }

  function handlePlay() {
    invalidateSeekActions();
    if (!currentTsRef.current) {
      commitCurrentTimestamp(new Date(normalizeTargetTs()));
    }
    setIsPlaying(true);
  }

  function handlePause() {
    invalidateSeekActions();
    setIsPlaying(false);
  }

  function formatDateTimeLocalInput(value) {
    const dt = value instanceof Date ? value : new Date(value);
    if (!Number.isFinite(dt.getTime())) return "";
    return formatLocalNaiveTs(dt);
  }

  function openExportModal() {
    if (!canExport || !selectedCameraIds.length || exportBusy) return;
    const center = timelineTs || currentTs || new Date(normalizeTargetTs());
    const start = new Date(center.getTime() - 15_000);
    const end = new Date(center.getTime() + 15_000);
    const [onlyCameraId] = selectedCameraIds;
    const cameraId = selectedCameraIds.length === 1 ? onlyCameraId : "";
    setError("");
    setLastExportId("");
    setExportStatus("");
    setExportModal({
      cameraId,
      title: `${TEXT.exportEvidence} ${formatPlaybackDateTime(center)}`,
      reason: "",
      startTs: formatProductTimestampParam(start),
      endTs: formatProductTimestampParam(end),
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
    if (!exportModal.cameraId) {
      setError(TEXT.exportPickCamera);
      setExportStatus(TEXT.exportPickCamera);
      return;
    }
    const validation = validateArchiveExportSelection(exportModal, exportLimits);
    if (validation) {
      setError(validation);
      setExportStatus(validation);
      return;
    }
    try {
      setError("");
      setExportBusy(true);
      setLastExportId("");
      const payload = buildArchiveExportPayload(exportModal);
      const result = await runArchiveExportWorkflow(payload, {
        onStatus: (message, job) => {
          setExportStatus(message);
          if (job?.id) setLastExportId(job.id);
        },
      });
      if (result?.job?.id) setLastExportId(result.job.id);
      saveBlobDownload(result.clip.blob, result.clip.filename || "km-vms-clip.mkv");
      setExportStatus(TEXT.exportReady);
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

  async function handleQuickDownload() {
    if (!selectedCameraIds.length || quickDownloadBusy) return;
    if (selectedCameraIds.length > 1) {
      setDownloadChooserOpen((prev) => !prev);
      return;
    }
    const [onlyCameraId] = selectedCameraIds;
    await startQuickDownloadForCamera(onlyCameraId);
  }

  function quickDownloadTimestamp() {
    return formatProductTimestampParam(timelineTs || currentTs || new Date(normalizeTargetTs()));
  }

  async function startQuickDownloadForCamera(cameraId) {
    setDownloadChooserOpen(false);
    try {
      setError("");
      setQuickDownloadBusy(true);
      const timestamp = quickDownloadTimestamp();
      await startChronologyCurrentRecordingDownload(cameraId, timestamp);
      const name = selectedCameraNames[cameraId] || `${TEXT.camera} ${cameraId}`;
      setDownloadResults((prev) => [...prev, { cameraId, name, status: TEXT.downloadStartedShort }].slice(-12));
      setExportStatus(`${TEXT.quickDownloadReady} ${name}`);
    } catch (err) {
      const message = normalizeChronologyDownloadError(err.message);
      const name = selectedCameraNames[cameraId] || `${TEXT.camera} ${cameraId}`;
      setDownloadResults((prev) => [...prev, { cameraId, name, status: message }].slice(-12));
      setError(message);
    } finally {
      setQuickDownloadBusy(false);
    }
  }

  async function startQuickDownloadForAllCameras() {
    if (quickDownloadBusy) return;
    setDownloadChooserOpen(false);
    setDownloadResults([]);
    for (const cameraId of selectedCameraIds) {
      await startQuickDownloadForCamera(cameraId);
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
  }

  function revealFullscreenControls(autoHide = true) {
    setFullscreenControlsVisible(true);
    if (fullscreenControlsTimerRef.current) {
      clearTimeout(fullscreenControlsTimerRef.current);
      fullscreenControlsTimerRef.current = null;
    }
    if (autoHide) {
      fullscreenControlsTimerRef.current = setTimeout(() => {
        setFullscreenControlsVisible(false);
        fullscreenControlsTimerRef.current = null;
      }, 5000);
    }
  }

  function hideFullscreenControls() {
    if (fullscreenControlsTimerRef.current) {
      clearTimeout(fullscreenControlsTimerRef.current);
      fullscreenControlsTimerRef.current = null;
    }
    setFullscreenControlsVisible(false);
  }

  async function seekBySeconds(seconds) {
    const nextDate = currentTsRef.current
      ? new Date(currentTsRef.current.getTime() + seconds * 1000)
      : new Date(normalizeTargetTs());
    const action = startSeekAction();

    setIsPlaying(false);
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(formatProductTimestampParam(nextDate), true);
    if (result.applied && action.shouldResume && seekActionIdRef.current === action.id) {
      setIsPlaying(true);
    }
  }

  async function handleTimelineSelect(nextDate) {
    const action = startSeekAction();

    setIsPlaying(false);
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(formatProductTimestampParam(nextDate), true);
    if (result.applied && action.shouldResume && seekActionIdRef.current === action.id) {
      setIsPlaying(true);
    }
  }

  function handleZoomOut() {
    const currentIndex = ZOOM_KEYS.indexOf(zoomKey);
    setZoomKey(ZOOM_KEYS[Math.max(currentIndex - 1, 0)]);
  }

  function handleZoomIn() {
    const currentIndex = ZOOM_KEYS.indexOf(zoomKey);
    setZoomKey(ZOOM_KEYS[Math.min(currentIndex + 1, ZOOM_KEYS.length - 1)]);
  }

  function handleTimelinePreview(nextDate) {
    setIsTimelinePreviewing(true);
    setPreviewTs(nextDate);
    syncFormDateTime(nextDate);
  }

  function handleTimelineDragStart() {
    const action = startSeekAction();
    isScrubbingRef.current = true;
    setIsTimelinePreviewing(true);
    playWasActiveRef.current = action.shouldResume;
    setPreviewTs(currentTsRef.current || new Date(normalizeTargetTs()));
    if (action.shouldResume) {
      setIsPlaying(false);
    }
  }

  async function handleTimelineDragEnd(nextDate) {
    const action = activeSeekActionRef.current;

    isScrubbingRef.current = false;
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(formatProductTimestampParam(nextDate), true);
    if (action && result.applied && action.shouldResume && seekActionIdRef.current === action.id) {
      setIsPlaying(true);
    }
  }

  useEffect(() => {
    if (!currentTs || !tileSourceKey) {
      setPlaybackMap({});
      return;
    }

    resolvePlaybackForTimestamp(formatProductTimestampParam(currentTs), true);
  }, [tileSourceKey]);

  useEffect(() => {
    if (!isPlaying || !currentTs || isScrubbingRef.current) return;
    resolvePlaybackForTimestamp(formatProductTimestampParam(currentTs), false);
  }, [currentTs, isPlaying]);

  useEffect(() => {
    const requestId = ++rangesRequestIdRef.current;

    async function loadRanges() {
      if (!currentTs || !selectedCameraIds.length || isScrubbingRef.current) {
        if (!selectedCameraIds.length) {
          setRangesData({});
          setRangesLoading(false);
          setRangesError(false);
          loadedRangesWindowRef.current = null;
        }
        return;
      }

      const hours = ZOOM_HOURS[zoomKey] || 24;
      const spanMs = hours * 3600 * 1000;
      const halfMs = spanMs / 2;
      const centerMs = currentTs.getTime();
      const fromMs = centerMs - halfMs;
      const toMs = centerMs + halfMs;
      const loaded = loadedRangesWindowRef.current;
      const needsReload =
        !loaded ||
        loaded.zoomKey !== zoomKey ||
        loaded.cameraKey !== selectedCameraKey ||
        fromMs < loaded.fromMs ||
        toMs > loaded.toMs ||
        Math.abs(centerMs - loaded.centerMs) > spanMs * 0.25;

      if (!needsReload) return;

      try {
        setRangesLoading(true);
        setRangesError(false);
        const response = await apiFetch(
          `/chronology/ranges?camera_ids=${selectedCameraKey}&from=${encodeURIComponent(formatProductTimestampParam(new Date(fromMs)))}&to=${encodeURIComponent(formatProductTimestampParam(new Date(toMs)))}`
        );

        if (requestId !== rangesRequestIdRef.current) return;

        setRangesData(response?.items || {});
        setRangesError(false);
        loadedRangesWindowRef.current = {
          fromMs,
          toMs,
          centerMs,
          zoomKey,
          cameraKey: selectedCameraKey,
        };
      } catch (_) {
        if (requestId !== rangesRequestIdRef.current) return;
        setRangesData({});
        setRangesError(true);
      } finally {
        if (requestId === rangesRequestIdRef.current) {
          setRangesLoading(false);
        }
      }
    }

    loadRanges();
  }, [currentTs, zoomKey, selectedCameraKey]);

  useEffect(() => {
    function handleKeyDown(event) {
      if (event.key === "Escape") {
        setFullscreenTileId(null);
        setIsSystemFullscreen(false);
        setFullscreenControlsVisible(true);
        if (document.fullscreenElement) {
          document.exitFullscreen?.().catch(() => {});
        }
      }
      const tagName = String(event.target?.tagName || "").toLowerCase();
      const isEditableTarget =
        ["button", "input", "select", "textarea", "a"].includes(tagName) ||
        Boolean(event.target?.isContentEditable);
      if (fullscreenTileId && event.key === " " && !isEditableTarget) {
        event.preventDefault();
        const fullscreenPlayback = playbackMapRef.current[fullscreenTileId];
        if (!fullscreenPlayback?.hasVideo) {
          revealFullscreenControls(false);
          return;
        }
        if (isPlaying) {
          handlePause();
        } else {
          handlePlay();
        }
        revealFullscreenControls(false);
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [fullscreenTileId, isPlaying]);

  useEffect(() => {
    if (!fullscreenTileId) {
      if (fullscreenControlsTimerRef.current) {
        clearTimeout(fullscreenControlsTimerRef.current);
        fullscreenControlsTimerRef.current = null;
      }
      setFullscreenControlsVisible(true);
      return undefined;
    }

    revealFullscreenControls(true);
    return () => {
      if (fullscreenControlsTimerRef.current) {
        clearTimeout(fullscreenControlsTimerRef.current);
        fullscreenControlsTimerRef.current = null;
      }
    };
  }, [fullscreenTileId]);

  useEffect(() => {
    document.body.classList.toggle("chronologySystemFullscreenBody", isSystemFullscreen);
    return () => document.body.classList.remove("chronologySystemFullscreenBody");
  }, [isSystemFullscreen]);

  useEffect(() => {
    function handleFullscreenChange() {
      const fullscreenElement = document.fullscreenElement;
      if (!fullscreenElement) {
        setIsSystemFullscreen(false);
        setFullscreenTileId(null);
        setFullscreenControlsVisible(true);
        return;
      }
      if (fullscreenElement === shellRef.current) {
        setIsSystemFullscreen(true);
        setFullscreenTileId(null);
        return;
      }

      const tileId =
        fullscreenElement.getAttribute("data-chronology-tile-video-id") ||
        fullscreenElement.getAttribute("data-chronology-tile-id");
      if (tileId) {
        setIsSystemFullscreen(false);
        setFullscreenTileId(tileId);
        setFullscreenControlsVisible(true);
      } else {
        setIsSystemFullscreen(false);
      }
    }

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  async function enterSystemFullscreen() {
    setFullscreenTileId(null);
    setIsSystemFullscreen(true);
    setIsSidebarCollapsed(true);
    try {
      await shellRef.current?.requestFullscreen?.();
    } catch (_) {}
  }

  async function exitSystemFullscreen() {
    setIsSystemFullscreen(false);
    if (document.fullscreenElement) {
      try {
        await document.exitFullscreen?.();
      } catch (_) {}
    }
  }

  return (
    <Layout>
      <div
        ref={shellRef}
        className={`chronologyShell ${isSystemFullscreen ? "systemFullscreen" : ""} ${isSidebarCollapsed ? "sidebarCollapsed" : "sidebarOpen"}`}
      >
        {isSystemFullscreen ? (
          <button
            type="button"
            className="chronologySidebarTab"
            title={isSidebarCollapsed ? TEXT.expandSidebar : TEXT.collapseSidebar}
            aria-label={isSidebarCollapsed ? TEXT.expandSidebar : TEXT.collapseSidebar}
            onClick={() => setIsSidebarCollapsed((value) => !value)}
          >
            {isSidebarCollapsed ? "\u203a" : "\u2039"}
          </button>
        ) : null}

        <aside className="chronologyCameraPanel" aria-hidden={isSystemFullscreen && isSidebarCollapsed}>
          <div className="chronologyPanelHeader">
            <div className="chronologyPanelTitle">{TEXT.cameras}</div>
            <div className="chronologyPanelActions">
              <button
                type="button"
                className="chronologyAlignButton"
                onClick={addAllCameras}
                disabled={!cameras.length}
              >
                {TEXT.addAll}
              </button>
              <button
                type="button"
                className="chronologyAlignButton"
                onClick={autoLayoutTiles}
                disabled={!tiles.length}
              >
                {TEXT.align}
              </button>
            </div>
          </div>

          {error ? <div className="chronologyError">{error}</div> : null}

          <div className="chronologyCameraList">
            {cameras.map((camera) => (
              <div
                key={camera.id}
                className="chronologyCameraItem"
                draggable
                onDragStart={(event) => {
                  event.dataTransfer.setData("application/x-chronology-camera-id", String(camera.id));
                  event.dataTransfer.effectAllowed = "copy";
                }}
              >
                <div className="chronologyCameraName">{camera.name}</div>
                <div className="chronologyCameraMeta">{camera.host}:{camera.port}</div>
              </div>
            ))}
          </div>
        </aside>

        <section className="chronologyMain">
          <div className={`chronologyToolbar ${isTimelinePreviewing ? "isPreviewing" : ""}`}>
            <input
              type="date"
              className="chronologyDateInput"
              value={date}
              title={TEXT.currentTime}
              aria-label={TEXT.currentTime}
              onChange={(event) => setDate(event.target.value)}
            />
            <input
              type="time"
              step="1"
              className="chronologyTimeInput"
              value={time}
              title={isTimelinePreviewing ? TEXT.previewTime : TEXT.currentTime}
              aria-label={isTimelinePreviewing ? TEXT.previewTime : TEXT.currentTime}
              onChange={(event) => setTime(event.target.value)}
            />
            <button type="button" className="chronologyPrimaryButton" onClick={handleFind}>
              {TEXT.find}
            </button>
            <div className="chronologyActionWithHelp" ref={downloadChooserRef}>
              <button
                type="button"
                className="chronologyIconButton chronologyDownloadButton"
                onClick={handleQuickDownload}
                disabled={!selectedCameraIds.length || quickDownloadBusy}
                title={TEXT.quickDownload}
                aria-label={TEXT.quickDownload}
              >
                {"\u2b07"}
              </button>
              <span className="chronologyHelpTooltip" tabIndex={0} aria-label={selectedCameraIds.length > 1 ? TEXT.quickDownloadChooseHelp : TEXT.quickDownloadHelp}>
                ?
                <span role="tooltip">{selectedCameraIds.length > 1 ? TEXT.quickDownloadChooseHelp : TEXT.quickDownloadHelp}</span>
              </span>
              {downloadChooserOpen ? (
                <div className="chronologyDownloadChooser" role="menu">
                  <div className="chronologyDownloadChooserTitle">{TEXT.quickDownloadChoose}</div>
                  {selectedCameraIds.map((cameraId) => (
                    <button
                      key={cameraId}
                      type="button"
                      className="chronologyDownloadChoice"
                      onClick={() => startQuickDownloadForCamera(cameraId)}
                      disabled={quickDownloadBusy}
                      role="menuitem"
                    >
                      {selectedCameraNames[cameraId] || `${TEXT.camera} ${cameraId}`}
                    </button>
                  ))}
                  <button
                    type="button"
                    className="chronologyDownloadChoice strong"
                    onClick={startQuickDownloadForAllCameras}
                    disabled={quickDownloadBusy}
                    role="menuitem"
                  >
                    {TEXT.allCameras}
                  </button>
                  {downloadResults.length ? (
                    <div className="chronologyDownloadResults">
                      {downloadResults.map((item, index) => (
                        <div key={`${item.cameraId}-${index}`}>
                          <span>{item.name}</span>
                          <strong>{item.status}</strong>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
            {canExport ? (
              <div className="chronologyActionWithHelp">
                <button
                  type="button"
                  className="chronologyIconButton chronologyEvidenceButton"
                  onClick={openExportModal}
                  disabled={!selectedCameraIds.length || exportBusy}
                  title={TEXT.exportEvidence}
                  aria-label={TEXT.exportEvidence}
                >
                  {"\u2696"}
                </button>
                <span className="chronologyHelpTooltip" tabIndex={0} aria-label={TEXT.exportEvidenceHelpShort}>
                  ?
                  <span role="tooltip">{TEXT.exportEvidenceHelpShort}</span>
                </span>
              </div>
            ) : null}
            <button type="button" className="chronologyIconButton" onClick={() => seekBySeconds(-10)} title={TEXT.back10} aria-label={TEXT.back10}>-10</button>
            <button type="button" className="chronologyIconButton" onClick={handlePlay} title={TEXT.play} aria-label={TEXT.play}>{"\u25b6"}</button>
            <button type="button" className="chronologyIconButton" onClick={handlePause} title={TEXT.pause} aria-label={TEXT.pause}>{"\u275a\u275a"}</button>
            <button type="button" className="chronologyIconButton" onClick={() => seekBySeconds(10)} title={TEXT.forward10} aria-label={TEXT.forward10}>+10</button>

            <div className="chronologySpeedGroup">
              {SPEED_OPTIONS.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  className={`chronologySpeedButton ${speed === item.value ? "active" : ""}`}
                  onClick={() => setSpeed(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>

            <div className={`chronologyTimeBox ${isTimelinePreviewing ? "preview" : ""}`}>
              <span>{isTimelinePreviewing ? TEXT.previewTime : TEXT.currentTime}</span>
              <strong>{formatPlaybackDateTime(timelineTs)}</strong>
            </div>
            <button
              type="button"
              className="chronologyIconButton chronologyFullscreenButton"
              onClick={isSystemFullscreen ? exitSystemFullscreen : enterSystemFullscreen}
              title={isSystemFullscreen ? TEXT.exitFullscreen : TEXT.enterFullscreen}
              aria-label={isSystemFullscreen ? TEXT.exitFullscreen : TEXT.enterFullscreen}
            >
              {isSystemFullscreen ? "\u2715" : "\u26f6"}
            </button>
          </div>

          <div className="chronologyTimelineWrap">
            <ChronologyTimeline
              currentTs={timelineTs}
              committedTs={currentTs}
              zoomKey={zoomKey}
              onZoomOut={handleZoomOut}
              onZoomIn={handleZoomIn}
              onPreviewTime={handleTimelinePreview}
              onDragStart={handleTimelineDragStart}
              onDragEnd={handleTimelineDragEnd}
              onSelectTime={handleTimelineSelect}
              rangesByCamera={rangesData}
              selectedCameraIds={selectedCameraIds}
              cameraNames={selectedCameraNames}
              currentTimeLabel={formatPlaybackDateTime(timelineTs)}
              committedTimeLabel={formatPlaybackDateTime(currentTs)}
              isPreviewing={isTimelinePreviewing}
              rangesLoading={rangesLoading}
              rangesError={rangesError}
              compact
            />
          </div>

          <div
            ref={workspaceRef}
            className={`chronologyWorkspace ${dragState || resizeState ? "isEditing" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = "copy";
            }}
            onDrop={handleDrop}
          >
            {!visibleTiles.length ? (
              <div className="chronologyEmpty">
                <div className="chronologyEmptyTitle">{TEXT.empty}</div>
              </div>
            ) : null}

            {visibleTiles.map((tile) => {
              const camera = cameraMap.get(String(tile.cameraId));
              const playback = playbackMap[tile.id] || {
                hasVideo: false,
                cameraId: null,
                relPath: null,
                offsetSec: 0,
                playbackKey: `${tile.id}-initial`,
              };

              return (
                <div
                  key={tile.id}
                  ref={(node) => {
                    if (node) tileRefs.current.set(tile.id, node);
                    else tileRefs.current.delete(tile.id);
                  }}
                  data-chronology-tile-id={tile.id}
                  className={`chronologyTile ${fullscreenTileId === tile.id ? "fullscreen" : ""}`}
                  style={{
                    left: fullscreenTileId === tile.id ? undefined : `${tile.xPct * 100}%`,
                    top: fullscreenTileId === tile.id ? undefined : `${tile.yPct * 100}%`,
                    width: fullscreenTileId === tile.id ? undefined : `${tile.wPct * 100}%`,
                    height: fullscreenTileId === tile.id ? undefined : `${tile.hPct * 100}%`,
                    zIndex: fullscreenTileId === tile.id ? 4000 : tile.z || 2,
                  }}
                  onPointerDown={(event) => startMove(event, tile)}
                >
                  <div className="chronologyTileBar">
                    <div className="chronologyTileTitle">{camera?.name || TEXT.camera}</div>
                    <button
                      type="button"
                      className="chronologyTileButton"
                      title={TEXT.close}
                      onPointerDown={(event) => event.stopPropagation()}
                      onClick={() => removeTile(tile.id)}
                    >
                      {"\u00d7"}
                    </button>
                  </div>

                  <div
                    ref={(node) => {
                      if (node) tileVideoRefs.current.set(tile.id, node);
                      else tileVideoRefs.current.delete(tile.id);
                    }}
                    data-chronology-tile-video-id={tile.id}
                    className={`chronologyTileVideo ${fullscreenTileId === tile.id ? "tileFullscreenVideo" : ""} ${fullscreenControlsVisible ? "controlsVisible" : "controlsHidden"}`}
                    tabIndex={fullscreenTileId === tile.id ? 0 : undefined}
                    onMouseMove={() => {
                      if (fullscreenTileId === tile.id) revealFullscreenControls(true);
                    }}
                    onDoubleClick={async (event) => {
                      event.stopPropagation();
                      if (fullscreenTileId === tile.id) {
                        await exitTileFullscreen();
                      } else {
                        await enterTileFullscreen(tile.id);
                      }
                    }}
                  >
                    {camera ? (
                      <ArchiveTilePlayer
                        playback={playback}
                        speed={speed}
                        isPlaying={isPlaying}
                        allowFullscreen={false}
                      />
                    ) : (
                      <div className="chronologyMissing">{TEXT.missing}</div>
                    )}

                    {fullscreenTileId === tile.id ? (
                      <div
                        className="chronologyFullscreenControlsLayer"
                        onPointerDown={(event) => event.stopPropagation()}
                        onClick={(event) => event.stopPropagation()}
                        onDoubleClick={(event) => event.stopPropagation()}
                      >
                        {fullscreenControlsVisible ? (
                          <div className="chronologyFullscreenPanel" role="group" aria-label={TEXT.fullscreenControls}>
                            <div className="chronologyFullscreenContext">
                              <div className="chronologyFullscreenCameraName" title={camera?.name || TEXT.camera}>{camera?.name || TEXT.camera}</div>
                              <div className="chronologyFullscreenMeta">
                                <span>{TEXT.timeContext}: {formatPlaybackDateTime(timelineTs)}</span>
                                <span>{TEXT.rangeContext}: {zoomKey}</span>
                                <span className={playback.hasVideo ? "isReady" : "isEmpty"}>
                                  {playback.hasVideo ? TEXT.archiveReady : TEXT.noArchiveAtTime}
                                </span>
                              </div>
                            </div>

                            <div className="chronologyFullscreenTimelineMini" aria-hidden="true">
                              <span className={playback.hasVideo ? "hasArchive" : "noArchive"} />
                            </div>

                            <div className="chronologyFullscreenActions">
                              <button
                                type="button"
                                className="chronologyFullscreenControlButton"
                                onClick={() => seekBySeconds(-10)}
                                title={TEXT.back10}
                                aria-label={TEXT.back10}
                              >
                                -10
                              </button>
                              <button
                                type="button"
                                className="chronologyFullscreenControlButton primary"
                                onClick={isPlaying ? handlePause : handlePlay}
                                disabled={!playback.hasVideo}
                                title={isPlaying ? TEXT.pause : TEXT.play}
                                aria-label={isPlaying ? TEXT.pause : TEXT.play}
                              >
                                {isPlaying ? "\u275a\u275a" : "\u25b6"}
                              </button>
                              <button
                                type="button"
                                className="chronologyFullscreenControlButton"
                                onClick={() => seekBySeconds(10)}
                                title={TEXT.forward10}
                                aria-label={TEXT.forward10}
                              >
                                +10
                              </button>
                              <button
                                type="button"
                                className="chronologyFullscreenControlButton"
                                onClick={hideFullscreenControls}
                                title={TEXT.hideFullscreenControls}
                                aria-label={TEXT.hideFullscreenControls}
                              >
                                _
                              </button>
                              <button
                                type="button"
                                className="chronologyFullscreenControlButton danger"
                                onClick={exitTileFullscreen}
                                title={TEXT.exitFullscreen}
                                aria-label={TEXT.exitFullscreen}
                              >
                                {"\u2715"}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <button
                            type="button"
                            className="chronologyFullscreenRevealTab"
                            onClick={() => revealFullscreenControls(false)}
                            title={TEXT.showFullscreenControls}
                            aria-label={TEXT.showFullscreenControls}
                          >
                            {"\u25b4"}
                          </button>
                        )}
                      </div>
                    ) : null}
                  </div>

                  {["top-left", "top-right", "bottom-left", "bottom-right"].map((corner) => (
                    <button
                      key={corner}
                      type="button"
                      className={`workspaceResizeHandle chronologyResizeHandle ${corner} ${fullscreenTileId === tile.id ? "hidden" : ""}`}
                      title={TEXT.resize}
                      aria-label={TEXT.resize}
                      onPointerDown={(event) => startResize(event, tile, corner)}
                    />
                  ))}
                </div>
              );
            })}
          </div>
        </section>
      </div>
      {exportModal ? (
        <div className="modalBackdrop">
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <div className="modalHeader">
              <h2 style={{ margin: 0 }}>{TEXT.exportTitle}</h2>
              <button type="button" className="iconCloseButton" onClick={closeExportModal} disabled={exportBusy} aria-label={TEXT.close}>
                {"\u00d7"}
              </button>
            </div>
            <div className="archiveExportForm">
              <label className="archiveExportField">
                <span>{TEXT.exportCamera}</span>
                <select
                  className="select"
                  value={exportModal.cameraId}
                  onChange={(event) => setExportModal((prev) => ({ ...prev, cameraId: event.target.value }))}
                  disabled={exportBusy}
                >
                  {selectedCameraIds.length > 1 ? (
                    <option value="">{TEXT.exportPickCamera}</option>
                  ) : null}
                  {selectedCameraIds.map((cameraId) => (
                    <option key={cameraId} value={cameraId}>
                      {selectedCameraNames[cameraId] || `${TEXT.camera} ${cameraId}`}
                    </option>
                  ))}
                </select>
              </label>
              <div className="archiveExportHelp">{TEXT.exportHelp}</div>
              <div className="archiveExportLimits">
                <strong>{TEXT.exportLimits}</strong>
                <span>{describeArchiveExportLimits(exportLimits)}</span>
              </div>
              <label className="archiveExportField">
                <span>{TEXT.exportStart}</span>
                <input
                  className="input"
                  type="datetime-local"
                  step="1"
                  value={formatDateTimeLocalInput(exportModal.startTs)}
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
                  value={formatDateTimeLocalInput(exportModal.endTs)}
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
                  maxLength={500}
                  disabled={exportBusy}
                />
              </label>
              {exportStatus ? <div className="archiveExportStatus">{exportStatus}</div> : null}
              <div className="actions">
                <button type="button" className="button primary" onClick={submitExport} disabled={exportBusy}>
                  {TEXT.exportRun}
                </button>
                <button type="button" className="button secondary" onClick={downloadLastManifest} disabled={!lastExportId || exportBusy} title={TEXT.exportManifestHelp}>
                  {TEXT.exportManifest}
                </button>
                <button type="button" className="button secondary" onClick={closeExportModal} disabled={exportBusy}>
                  {TEXT.close}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </Layout>
  );
}
