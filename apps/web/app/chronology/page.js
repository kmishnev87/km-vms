"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { createSerializedLayoutWriter } from "../../lib/serializedLayoutWriter";
import { useAuthSession } from "../../lib/useAuthSession";
import useChronologyClock from "../../lib/useChronologyClock";
import useChronologySources from "../../lib/useChronologySources";
import useChronologyPlaybackCoordinator from "../../lib/useChronologyPlaybackCoordinator";
import { WORKSPACE_KEY, normalizeTile, readSavedTiles, migrationMarkerKey, saveTiles, dedupeTiles, nextZIndex, chooseAutoGrid, layoutTiles, backendPayload, clamp, MIN_TILE_W, MIN_TILE_H, DEFAULT_W_PCT, DEFAULT_H_PCT } from "../../lib/chronologyWorkspaceLayout";
import Layout from "../../components/Layout";
import ArchiveTilePlayer from "../../components/ArchiveTilePlayer";
import ChronologyTimeline from "../../components/ChronologyTimeline";
import ArchiveExportResult from "../../components/ArchiveExportResult";
import useArchiveExport from "../../lib/useArchiveExport";
import { reconcilePlaybackMembership, timelineRangeWindow, timelineRangeWindowCovers, timelineRangesNeedReload } from "../../lib/workspaceContinuity";
import { OperationToast, useModalBodyScrollLock } from "../../components/OperationFeedback";
import { apiFetch, getAuthToken } from "../../lib/api";
import {
  buildArchiveExportPayload,
  canExportRecordings,
  describeArchiveExportLimits,
  getArchiveExportLimits,
  normalizeChronologyDownloadError,
  startChronologyCurrentRecordingDownload,
  validateArchiveExportSelection,
} from "../../lib/archiveExports";
import { productLocalInputToApi, productInstantInputValue, productCalendarDate } from "../../lib/timezone";
import { useI18n } from "../../lib/i18n";
import {
  consumeTouchDoubleTapSuppressionToken,
  createTouchDoubleTapSuppressionToken,
} from "../../lib/videoZoomPanCore";
import {
  CHRONOLOGY_CAMERA_DROP_MIME,
  SIDEBAR_CAMERA_REORDER_MIME,
  mergeSidebarCameraOrder,
  resizeWorkspaceTile,
  sanitizeSidebarCameraOrder,
  visibleWorkspaceTiles,
  workspaceCameraIds,
} from "../../lib/workspaceLayoutCore";

const POINTER_DRAG_THRESHOLD_PX = 8;
const DOUBLE_TAP_MS = 360;
const DOUBLE_TAP_DISTANCE_PX = 22;
const ZOOM_KEYS = ["24h", "3d", "7d"];
const ZOOM_HOURS = {
  "24h": 24,
  "3d": 72,
  "7d": 168,
};
const HYDRATION_INITIAL_TS = new Date(Date.UTC(2000, 0, 1, 0, 0, 0));

const SPEED_OPTIONS = [
  { value: 0.25, label: "0.25x" },
  { value: 0.5, label: "0.5x" },
  { value: 1, label: "1x" },
  { value: 2, label: "2x" },
  { value: 4, label: "4x" },
];

const CHRONOLOGY_TEXT = {
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
  collapseTimeline: "\u0421\u043a\u0440\u044b\u0442\u044c timeline",
  expandTimeline: "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c timeline",
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
  exportManifest: "\u0421\u043a\u0430\u0447\u0430\u0442\u044c \u043f\u0430\u0441\u043f\u043e\u0440\u0442",
  exportManifestHelp: "\u041f\u0430\u0441\u043f\u043e\u0440\u0442 \u043a\u043b\u0438\u043f\u0430 \u2014 \u0442\u0435\u0445\u043d\u0438\u0447\u0435\u0441\u043a\u0438\u0439 \u0444\u0430\u0439\u043b \u0434\u043b\u044f \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0438, \u0441 \u043a\u0430\u043a\u043e\u0439 \u043a\u0430\u043c\u0435\u0440\u044b \u0438 \u0437\u0430 \u043a\u0430\u043a\u043e\u0439 \u043f\u0435\u0440\u0438\u043e\u0434 \u0441\u043e\u0437\u0434\u0430\u043d \u043a\u043b\u0438\u043f.",
  exportReady: "\u041a\u043b\u0438\u043f \u0433\u043e\u0442\u043e\u0432.",
};

function AlignGridIcon() {
  return (
    <span className="workspaceAlignGridIcon" data-workspace-align-icon="grid-2x2" aria-hidden="true">
      <span className="workspaceAlignGridIconLine vertical left" />
      <span className="workspaceAlignGridIconLine vertical right" />
      <span className="workspaceAlignGridIconLine horizontal top" />
      <span className="workspaceAlignGridIconLine horizontal bottom" />
    </span>
  );
}

function ChronologyScissorsIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="5.5" cy="6.5" r="3" className="chronologyIconCutout"></circle>
      <circle cx="5.5" cy="17.5" r="3" className="chronologyIconCutout"></circle>
      <path d="M8.3 8.3 20.5 19.2"></path>
      <path d="M8.3 15.7 20.5 4.8"></path>
    </svg>
  );
}

function ChronologyDownloadIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 4.6v10.2"></path>
      <path d="m7.8 10.8 4.2 4.2 4.2-4.2"></path>
      <path d="M5.8 19.4h12.4"></path>
    </svg>
  );
}


function pointerDistance(a, b) {
  if (!a || !b) return Number.POSITIVE_INFINITY;
  return Math.hypot(Number(a.x) - Number(b.x), Number(a.y) - Number(b.y));
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function getNow(timezone = "UTC") {
  return productCalendarDate(productInstantInputValue(new Date(), timezone));
}

function formatPlaybackDateTime(dt) {
  if (!dt) return "--";
  return `${pad(dt.getUTCDate())}.${pad(dt.getUTCMonth() + 1)}.${dt.getUTCFullYear()} ${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}:${pad(dt.getUTCSeconds())}`;
}

function formatLocalNaiveTs(dt) {
  return `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}T${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}:${pad(dt.getUTCSeconds())}`;
}

function formatProductTimestampParam(dt) {
  return productLocalInputToApi(formatLocalNaiveTs(dt));
}

function dateTimeFromDate(dt) {
  return {
    date: `${dt.getUTCFullYear()}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`,
    time: `${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}:${pad(dt.getUTCSeconds())}`,
  };
}


export default function ChronologyPage() {
  const { text } = useI18n();
  const TEXT = useMemo(
    () => Object.fromEntries(Object.entries(CHRONOLOGY_TEXT).map(([key, value]) => [key, text(value)])),
    [text]
  );
  const initialTs = HYDRATION_INITIAL_TS;
  const initialForm = dateTimeFromDate(initialTs);
  const workspaceRef = useRef(null);
  const timelinePanelRef = useRef(null);
  const hydratedRef = useRef(false);
  const backendReadyRef = useRef(false);
  const saveTimerRef = useRef(null);
  const pendingLayoutSaveRef = useRef(null);
  const lastBackendLayoutPayloadRef = useRef("");
  const calendarTouchedRef = useRef(false);
  const [productTimezone, setProductTimezone] = useState("UTC");
  const tilesRef = useRef([]);
  const isScrubbingRef = useRef(false);
  const rangesRequestIdRef = useRef(0);
  const loadedRangesWindowRef = useRef(null);
  const rangesInFlightRef = useRef(null);
  const seekActionIdRef = useRef(0);
  const activeSeekActionRef = useRef(null);
  const playWasActiveRef = useRef(false);
  const shellRef = useRef(null);
  const tileRefs = useRef(new Map());
  const tileVideoRefs = useRef(new Map());
  const fullscreenControlsTimerRef = useRef(null);
  const downloadChooserRef = useRef(null);
  const sidebarPointerDragRef = useRef(null);
  const lastSidebarTapRef = useRef(null);
  const sidebarTouchDoubleTapSuppressionRef = useRef(null);
  const lastTileTapRef = useRef(null);
  const tileFullscreenReturnStateRef = useRef(null);

  const [cameras, setCameras] = useState([]);
  const [exportCameraOptions, setExportCameraOptions] = useState([]);
  const [camerasLoaded, setCamerasLoaded] = useState(false);
  const [tiles, setTiles] = useState([]);
  const [sidebarCameraOrder, setSidebarCameraOrder] = useState([]);
  const [lastPersistedSidebarCameraOrder, setLastPersistedSidebarCameraOrder] = useState([]);
  const layoutWriterRef = useRef(null);
  const workspaceSession = useAuthSession();
  const workspaceGenerationRef = useRef(0);
  const cacheOwnerRef = useRef(null);
  const [operationToast, setOperationToast] = useState(null);
  const cameraLoadErrorRef = useRef("");
  const [dragState, setDragState] = useState(null);
  const [resizeState, setResizeState] = useState(null);
  const [draggedSidebarCameraId, setDraggedSidebarCameraId] = useState("");
  const [sidebarDropTargetCameraId, setSidebarDropTargetCameraId] = useState("");
  const [sidebarPointerDragMode, setSidebarPointerDragMode] = useState("");
  const [sidebarDragPreview, setSidebarDragPreview] = useState(null);
  const { currentTs, setCurrentTs, currentTsRef, previewTs, setPreviewTs,
    speed, setSpeed, isPlaying, setIsPlaying } = useChronologyClock(initialTs, isScrubbingRef);
  const { playbackCoordinator, startPlaybackCoordinator, cancelPlaybackCoordinator,
    releasePlaybackCoordinator, handleTilePlaybackState } = useChronologyPlaybackCoordinator(setIsPlaying);
  const [date, setDate] = useState(initialForm.date);
  const [time, setTime] = useState(initialForm.time);
  const [zoomKey, setZoomKey] = useState("24h");
  const [rangesData, setRangesData] = useState({});
  const [rangesLoading, setRangesLoading] = useState(false);
  const [rangesError, setRangesError] = useState(false);
  const [isTimelinePreviewing, setIsTimelinePreviewing] = useState(false);
  const [fullscreenTileId, setFullscreenTileId] = useState(null);
  const [fullscreenControlsVisible, setFullscreenControlsVisible] = useState(true);
  const [isSystemFullscreen, setIsSystemFullscreen] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(true);
  const [isTimelineCollapsed, setIsTimelineCollapsed] = useState(false);
  const [currentUser, setCurrentUser] = useState(null);
  const [exportModal, setExportModal] = useState(null);
  const exportController = useArchiveExport();
  const exportDraftRef = useRef(null);
  const exportBusy = exportController.busy;
  useModalBodyScrollLock(Boolean(exportModal));
  const [exportLimits, setExportLimits] = useState(null);
  const [quickDownloadBusy, setQuickDownloadBusy] = useState(false);
  const [downloadChooserOpen, setDownloadChooserOpen] = useState(false);
  const [downloadResults, setDownloadResults] = useState([]);

  function showWorkspaceError(message) {
    setOperationToast({ id: `chronology-action-error-${Date.now()}`, title: message || TEXT.loadError, tone: "error" });
  }

  function isWorkspaceCurrent(ownerSession, generation) {
    return generation === workspaceGenerationRef.current && getAuthToken() === ownerSession;
  }

  async function loadCameras(ownerSession = workspaceSession, generation = workspaceGenerationRef.current) {
    if (!isWorkspaceCurrent(ownerSession, generation)) return;
    try {
      const data = await apiFetch("/viewer/cameras");
      if (!isWorkspaceCurrent(ownerSession, generation)) return;
      cameraLoadErrorRef.current = "";
      setCameras(Array.isArray(data) ? data : []);
    } catch (err) {
      if (!isWorkspaceCurrent(ownerSession, generation)) return;
      if (cameraLoadErrorRef.current !== (err.message || TEXT.loadError)) {
        cameraLoadErrorRef.current = err.message || TEXT.loadError;
        showWorkspaceError(cameraLoadErrorRef.current);
      }
    } finally {
      if (isWorkspaceCurrent(ownerSession, generation)) setCamerasLoaded(true);
    }
  }

  async function loadExportCameras(ownerSession = workspaceSession, generation = workspaceGenerationRef.current) {
    if (!isWorkspaceCurrent(ownerSession, generation)) return;
    try {
      const data = await apiFetch("/recordings/cameras");
      if (!isWorkspaceCurrent(ownerSession, generation)) return;
      setExportCameraOptions(Array.isArray(data?.export_items) ? data.export_items : []);
    } catch (_) {
      if (!isWorkspaceCurrent(ownerSession, generation)) return;
      setExportCameraOptions([]);
    }
  }

  async function loadWorkspaceLayout(ownerSession, generation) {
    const writer = layoutWriterRef.current;
    try {
      await writer.flush();
      if (!isWorkspaceCurrent(ownerSession, generation)) return;
      const [user, layout] = await Promise.all([
        apiFetch("/users/me"),
        apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`),
      ]);
      if (!isWorkspaceCurrent(ownerSession, generation)) return;
      cacheOwnerRef.current = user?.id ?? null;
      setCurrentUser(user);
      backendReadyRef.current = true;
      const backendTiles = Array.isArray(layout?.tiles) ? dedupeTiles(layout.tiles.map(normalizeTile)) : [];
      const backendSidebarOrder = sanitizeSidebarCameraOrder(layout?.sidebarCameraOrder);
      const markerKey = migrationMarkerKey(user?.id);
      setSidebarCameraOrder(backendSidebarOrder);
      setLastPersistedSidebarCameraOrder(backendSidebarOrder);
      lastBackendLayoutPayloadRef.current = JSON.stringify(backendPayload(backendTiles, backendSidebarOrder));

      if (backendTiles.length) {
        setTiles(backendTiles);
        saveTiles(backendTiles, cacheOwnerRef.current);
        localStorage.setItem(markerKey, "1");
        return;
      }

      const localTiles = readSavedTiles(cacheOwnerRef.current);
      const shouldMigrate = localTiles.length && !localStorage.getItem(markerKey);
      if (shouldMigrate) {
        setTiles(localTiles);
        saveTiles(localTiles, cacheOwnerRef.current);
        const payload = backendPayload(localTiles, backendSidebarOrder);
        let writeError = null;
        writer.save(payload, () => {}, (error) => { writeError = error; }, () => getAuthToken() === ownerSession);
        await writer.flush();
        if (!isWorkspaceCurrent(ownerSession, generation)) return;
        if (writeError) throw writeError;
        lastBackendLayoutPayloadRef.current = JSON.stringify(payload);
      } else {
        setTiles([]);
      }
      localStorage.setItem(markerKey, "1");
    } catch (err) {
      if (!isWorkspaceCurrent(ownerSession, generation)) return;
      backendReadyRef.current = false;
      setTiles(readSavedTiles(cacheOwnerRef.current));
      setSidebarCameraOrder([]);
      setLastPersistedSidebarCameraOrder([]);
      showWorkspaceError(err.message || TEXT.loadError);
    } finally {
      if (isWorkspaceCurrent(ownerSession, generation)) hydratedRef.current = true;
    }
  }

  useEffect(() => {
    let cancelled = false;
    function initialize(timezone) {
      if (cancelled) return;
      setProductTimezone(timezone || "UTC");
      if (calendarTouchedRef.current || currentTsRef.current !== initialTs) return;
      const now = getNow(timezone);
      const next = dateTimeFromDate(now);
      currentTsRef.current = now;
      setCurrentTs(now);
      setPreviewTs(now);
      setDate(next.date);
      setTime(next.time);
    }
    apiFetch("/system/status").then(status => initialize(status?.timezone)).catch(() => initialize("UTC"));
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const ownerSession = workspaceSession;
    const generation = ++workspaceGenerationRef.current;
    hydratedRef.current = backendReadyRef.current = false;
    cacheOwnerRef.current = null;
    lastBackendLayoutPayloadRef.current = "";
    setTiles([]); setSidebarCameraOrder([]); setLastPersistedSidebarCameraOrder([]);
    setCameras([]); setCamerasLoaded(false); setCurrentUser(null); setExportCameraOptions([]);
    setExportModal(null); setIsPlaying(false); setFullscreenTileId(null);
    exportDraftRef.current = null;
    setQuickDownloadBusy(false); setDownloadChooserOpen(false); setDownloadResults([]);
    const writer = createSerializedLayoutWriter((payload) => apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    }), { scope: WORKSPACE_KEY, ownerSession });
    layoutWriterRef.current = writer;
    if (ownerSession) {
      loadWorkspaceLayout(ownerSession, generation);
      loadCameras(ownerSession, generation);
      loadExportCameras(ownerSession, generation);
    }
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      pendingLayoutSaveRef.current?.();
      pendingLayoutSaveRef.current = null;
      workspaceGenerationRef.current += 1;
      hydratedRef.current = backendReadyRef.current = false;
      writer.dispose();
      rangesInFlightRef.current?.controller.abort();
      rangesRequestIdRef.current += 1;
    };
  }, [workspaceSession]);

  useEffect(() => {
    if (!hydratedRef.current) return;
    const ownerSession = workspaceSession;
    const generation = workspaceGenerationRef.current;
    if (!isWorkspaceCurrent(ownerSession, generation)) return;
    const writer = layoutWriterRef.current;
    writer.invalidate();
    saveTiles(tiles, cacheOwnerRef.current);
    if (!backendReadyRef.current) return;
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
    const submitLayoutSave = () => {
        if (pendingLayoutSaveRef.current === submitLayoutSave) pendingLayoutSaveRef.current = null;
        if (!isWorkspaceCurrent(ownerSession, generation)) return;
        const payload = backendPayload(tiles, sidebarCameraOrder);
        const payloadText = JSON.stringify(payload);
        if (payloadText === lastBackendLayoutPayloadRef.current) return;
        writer.save(payload, (saved) => {
        const savedOrder = sanitizeSidebarCameraOrder(saved?.sidebarCameraOrder);
        setLastPersistedSidebarCameraOrder(savedOrder);
        setSidebarCameraOrder(savedOrder);
        lastBackendLayoutPayloadRef.current = JSON.stringify(backendPayload(saved?.tiles || tiles, savedOrder));
        }, (err) => {
        showWorkspaceError(err.message || TEXT.loadError);
        }, () => getAuthToken() === ownerSession);
    };
    pendingLayoutSaveRef.current = submitLayoutSave;
    saveTimerRef.current = setTimeout(submitLayoutSave, 500);
  }, [tiles, sidebarCameraOrder, lastPersistedSidebarCameraOrder, workspaceSession]);


  const cameraMap = useMemo(() => {
    const map = new Map();
    cameras.forEach((camera) => map.set(String(camera.id), camera));
    return map;
  }, [cameras]);

  const visibleTiles = useMemo(() => {
    if (!camerasLoaded) return tiles;
    return visibleWorkspaceTiles(tiles, cameras);
  }, [tiles, cameras, camerasLoaded]);

  const tileSourceKey = visibleTiles.map((tile) => `${tile.id}:${tile.cameraId}`).sort().join("|");
  useEffect(() => {
    tilesRef.current = visibleTiles;
  }, [visibleTiles]);
  const { playbackMap, playbackMapRef, playbackResolverRef, resolvePlaybackForTimestamp } = useChronologySources({
    tilesRef, tileSourceKey, currentTs, isPlaying, isScrubbingRef, formatProductTimestampParam,
  });

  const orderedCameras = useMemo(
    () => mergeSidebarCameraOrder(cameras, sidebarCameraOrder),
    [cameras, sidebarCameraOrder]
  );

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

  const quickDownloadCameraIds = useMemo(() => {
    const result = [];
    visibleTiles.forEach((tile) => {
      const cameraId = String(tile.cameraId || "");
      const playback = playbackMap[tile.id];
      if (!cameraId || !playback?.hasVideo || result.includes(cameraId)) return;
      result.push(cameraId);
    });
    return result;
  }, [visibleTiles, playbackMap]);

  const quickDownloadCameraNames = useMemo(() => {
    const result = {};
    quickDownloadCameraIds.forEach((id) => {
      const camera = cameraMap.get(String(id));
      result[String(id)] = camera?.name || `${TEXT.camera} ${id}`;
    });
    return result;
  }, [quickDownloadCameraIds, cameraMap]);

  const selectedCameraKey = selectedCameraIds.slice().sort().join(",");
  const quickDownloadCameraKey = quickDownloadCameraIds.join(",");
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
  }, [selectedCameraKey, quickDownloadCameraKey, tileSourceKey, date, time]);

  useEffect(() => {
    setDownloadChooserOpen(false);
  }, [timelineTs?.getTime?.()]);

  function workspaceBounds() {
    return workspaceRef.current?.getBoundingClientRect() || null;
  }

  function normalizeTargetTs() {
    const normalizedDate = date || dateTimeFromDate(getNow(productTimezone)).date;
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
        setOperationToast({
          id: `chronology-duplicate-${Date.now()}`,
          title: TEXT.duplicate,
          tone: "info",
        });
        return prev;
      }
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

  function addTileFromSidebar(cameraId) {
    const bounds = workspaceBounds();
    if (!bounds) return;
    const activeCount = activeLayoutTiles(tiles).length;
    const offset = Math.min(activeCount * 28, Math.max(bounds.width * 0.18, 1));
    addTile(cameraId, bounds.left + bounds.width / 2 + offset, bounds.top + bounds.height * 0.42 + offset / 2);
  }

  function updateTile(tileId, patch) {
    setTiles((prev) =>
      dedupeTiles(prev.map((tile) => (tile.id === tileId ? normalizeTile({ ...tile, ...patch }) : tile)))
    );
  }

  function removeTile(tileId) {
    cancelPlaybackCoordinator("tile-removed");
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
      return layoutTiles([...active, ...additions], workspaceBounds());
    });
  }

  function handleDrop(event) {
    event.preventDefault();
    const cameraId = event.dataTransfer.getData(CHRONOLOGY_CAMERA_DROP_MIME);
    if (cameraId) addTile(cameraId, event.clientX, event.clientY);
    setDraggedSidebarCameraId("");
    setSidebarDropTargetCameraId("");
  }

  function sidebarDropToken(cameraId, position) {
    return `${String(cameraId || "")}:${position === "after" ? "after" : "before"}`;
  }

  function sidebarDropParts(token) {
    const [cameraId, position] = String(token || "").split(":");
    return { cameraId, position: position === "after" ? "after" : "before" };
  }

  function sidebarDropPosition(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    return event.clientY > rect.top + rect.height / 2 ? "after" : "before";
  }

  function sidebarDropPositionFromPoint(row, clientY) {
    const rect = row.getBoundingClientRect();
    return clientY > rect.top + rect.height / 2 ? "after" : "before";
  }

  function reorderSidebarCamera(targetCameraId, position = "before", sourceCameraId = draggedSidebarCameraId) {
    const sourceId = String(sourceCameraId || "");
    const targetId = String(targetCameraId || "");
    if (!sourceId || !targetId || sourceId === targetId) return;
    const current = orderedCameras.map((camera) => String(camera.id));
    const from = current.indexOf(sourceId);
    const to = current.indexOf(targetId);
    if (from < 0 || to < 0) return;
    const next = [...current];
    const [moved] = next.splice(from, 1);
    const targetIndex = next.indexOf(targetId);
    const insertAt = position === "after" ? targetIndex + 1 : targetIndex;
    next.splice(insertAt, 0, moved);
    setSidebarCameraOrder(next);
    setDraggedSidebarCameraId("");
    setSidebarDropTargetCameraId("");
  }

  function clearSidebarPointerDrag() {
    sidebarPointerDragRef.current = null;
    setDraggedSidebarCameraId("");
    setSidebarDropTargetCameraId("");
    setSidebarPointerDragMode("");
    setSidebarDragPreview(null);
  }

  function cancelSidebarPointerDrag() {
    lastSidebarTapRef.current = null;
    clearSidebarPointerDrag();
  }

  function consumeSidebarTouchDoubleClick(event, cameraId) {
    const nativeEvent = event?.nativeEvent || event;
    const point = { x: Number(nativeEvent?.clientX || 0), y: Number(nativeEvent?.clientY || 0) };
    const suppression = consumeTouchDoubleTapSuppressionToken(
      sidebarTouchDoubleTapSuppressionRef.current,
      { time: Date.now(), point, ownerKey: String(cameraId) }
    );
    if (!suppression.consumed) return false;
    sidebarTouchDoubleTapSuppressionRef.current = suppression.nextToken;
    event.preventDefault();
    event.stopPropagation();
    nativeEvent?.stopImmediatePropagation?.();
    return true;
  }

  function sidebarCameraRowFromPoint(clientX, clientY) {
    const node = document.elementFromPoint(clientX, clientY);
    return node?.closest?.("[data-sidebar-camera-row]") || null;
  }

  function isPointInsideWorkspace(clientX, clientY) {
    const bounds = workspaceBounds();
    return Boolean(
      bounds &&
        clientX >= bounds.left &&
        clientX <= bounds.right &&
        clientY >= bounds.top &&
        clientY <= bounds.bottom
    );
  }

  function startSidebarPointerDrag(event, camera) {
    if (event.button !== 0) return;
    const handle = Boolean(event.target?.closest?.("[data-sidebar-drag-handle]"));
    if (!handle && event.target?.closest?.("button, select, input, textarea, a")) return;
    const dragAllowed = event.pointerType !== "touch" || handle;
    if (event.pointerType !== "mouse" && dragAllowed) event.preventDefault();
    if (event.pointerType === "mouse" && !event.nativeEvent?.sourceCapabilities?.firesTouchEvents) {
      sidebarTouchDoubleTapSuppressionRef.current = null;
    }
    sidebarPointerDragRef.current = {
      pointerId: event.pointerId,
      cameraId: String(camera.id),
      label: camera.name || TEXT.camera,
      meta: camera.host || "",
      startX: event.clientX,
      startY: event.clientY,
      active: false,
      dragAllowed,
      handle,
    };
    if (dragAllowed) event.currentTarget?.setPointerCapture?.(event.pointerId);
  }

  function updateSidebarPointerDrag(event) {
    const state = sidebarPointerDragRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    const distance = Math.hypot(event.clientX - state.startX, event.clientY - state.startY);
    if (!state.active && distance < POINTER_DRAG_THRESHOLD_PX) return;
    if (!state.dragAllowed) { sidebarPointerDragRef.current = null; lastSidebarTapRef.current = null; return; }

    if (!state.active) {
      state.active = true;
      lastSidebarTapRef.current = null;
      setDraggedSidebarCameraId(state.cameraId);
    }

    if (isPointInsideWorkspace(event.clientX, event.clientY)) {
      setSidebarPointerDragMode("workspace");
      setSidebarDropTargetCameraId("");
      setSidebarDragPreview({ cameraId: state.cameraId, label: state.label, meta: state.meta, x: event.clientX, y: event.clientY, mode: "workspace" });
      return;
    }

    const row = sidebarCameraRowFromPoint(event.clientX, event.clientY);
    const rowCameraId = row?.getAttribute("data-sidebar-camera-row") || "";
    if (rowCameraId && rowCameraId !== state.cameraId) {
      setSidebarPointerDragMode("reorder");
      setSidebarDropTargetCameraId(sidebarDropToken(rowCameraId, sidebarDropPositionFromPoint(row, event.clientY)));
      setSidebarDragPreview({ cameraId: state.cameraId, label: state.label, meta: state.meta, x: event.clientX, y: event.clientY, mode: "reorder" });
      return;
    }

    setSidebarPointerDragMode("cancel");
    setSidebarDropTargetCameraId("");
    setSidebarDragPreview({ cameraId: state.cameraId, label: state.label, meta: state.meta, x: event.clientX, y: event.clientY, mode: "cancel" });
  }

  function finishSidebarPointerDrag(event, cameraId) {
    const state = sidebarPointerDragRef.current;
    if (!state || state.pointerId !== event.pointerId) return;
    event.currentTarget?.releasePointerCapture?.(event.pointerId);

    if (state.active) {
      lastSidebarTapRef.current = null;
      if (isPointInsideWorkspace(event.clientX, event.clientY)) {
        addTile(state.cameraId, event.clientX, event.clientY);
      } else {
        const row = sidebarCameraRowFromPoint(event.clientX, event.clientY);
        const rowCameraId = row?.getAttribute("data-sidebar-camera-row") || "";
        if (rowCameraId && rowCameraId !== state.cameraId) {
          reorderSidebarCamera(rowCameraId, sidebarDropPositionFromPoint(row, event.clientY), state.cameraId);
        }
      }
      clearSidebarPointerDrag();
      return;
    }

    clearSidebarPointerDrag();
    if (state.handle) return;
    if (event.pointerType === "mouse") return;
    const now = Date.now();
    const point = { x: event.clientX, y: event.clientY };
    const previous = lastSidebarTapRef.current;
    if (
      previous?.cameraId === String(cameraId) &&
      now - previous.time <= DOUBLE_TAP_MS &&
      pointerDistance(previous.point, point) <= DOUBLE_TAP_DISTANCE_PX
    ) {
      lastSidebarTapRef.current = null;
      sidebarTouchDoubleTapSuppressionRef.current = createTouchDoubleTapSuppressionToken({
        ownerKey: String(cameraId),
        time: now,
        point,
      });
      addTileFromSidebar(cameraId);
      return;
    }
    lastSidebarTapRef.current = { cameraId: String(cameraId), time: now, point };
  }

  function startMove(event, tile) {
    if (fullscreenTileId) return;
    if (event.button !== 0) return;
    if (event.target?.closest?.("[data-chronology-tile-video-id], button, select, input, textarea, a")) return;
    const bounds = workspaceBounds();
    if (!bounds) return;

    event.preventDefault();
    event.currentTarget?.setPointerCapture?.(event.pointerId);
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
    event.currentTarget?.setPointerCapture?.(event.pointerId);
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
    const returnState = { isSystemFullscreen, isSidebarCollapsed };
    tileFullscreenReturnStateRef.current = returnState;
    if (!returnState.isSystemFullscreen) setIsSystemFullscreen(false);
    setFullscreenTileId(tileId);
    setFullscreenControlsVisible(true);
    const fullscreenEl = tileVideoRefs.current.get(tileId) || tileRefs.current.get(tileId);
    if (!fullscreenEl || document.fullscreenElement === fullscreenEl) return;

    try {
      await fullscreenEl.requestFullscreen?.();
    } catch (_) {
      tileFullscreenReturnStateRef.current = null;
      setIsSystemFullscreen(returnState.isSystemFullscreen);
      setIsSidebarCollapsed(returnState.isSidebarCollapsed);
    }
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

  async function toggleTileFullscreen(tileId) {
    if (fullscreenTileId === tileId) {
      await exitTileFullscreen();
    } else {
      await enterTileFullscreen(tileId);
    }
  }

  function handleTileVideoTouchDoubleTap(tileId, zone) {
    if (zone === "left") {
      seekBySeconds(-10);
      return;
    }
    if (zone === "right") {
      seekBySeconds(10);
      return;
    }
    toggleTileFullscreen(tileId);
  }

  function handleTileSurfaceTap(tileId, point) {
    const now = Date.now();
    const previous = lastTileTapRef.current;
    if (
      previous?.tileId === tileId &&
      now - previous.time <= DOUBLE_TAP_MS &&
      pointerDistance(previous.point, point) <= DOUBLE_TAP_DISTANCE_PX
    ) {
      lastTileTapRef.current = null;
      toggleTileFullscreen(tileId);
      return;
    }
    lastTileTapRef.current = { tileId, time: now, point };
  }

  function handleTileSurfacePointerUp(event, tileId) {
    if (event.target?.closest?.("button, select, input, textarea, a")) return;
    if (dragState || resizeState) return;
    if (event.pointerType === "mouse" || event.pointerType === "touch") return;
    event.stopPropagation();
    handleTileSurfaceTap(tileId, { x: event.clientX, y: event.clientY });
  }

  useEffect(() => {
    if (!dragState) return undefined;

    function onMove(event) {
      if (event.pointerType === "mouse" && event.buttons === 0) {
        setDragState(null);
        return;
      }
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
    window.addEventListener("pointercancel", onUp, { once: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
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
    window.addEventListener("pointercancel", onUp, { once: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [resizeState]);


  async function handleFind() {
    invalidateSeekActions();
    const targetDate = productCalendarDate(normalizeTargetTs());
    const targetTs = formatProductTimestampParam(targetDate);

    setIsPlaying(false);
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(targetDate);
    const result = await resolvePlaybackForTimestamp(targetTs, false);
    if (!result.applied) {
      return;
    }
    const expectedTileIds = tilesRef.current.map((tile) => tile.id);
    const coordinator = startPlaybackCoordinator({ targetTs, shouldResume: false, expectedTileIds });
    if (!expectedTileIds.length) {
      releasePlaybackCoordinator(coordinator.id, "released");
    }
  }

  function handlePlay() {
    invalidateSeekActions();
    if (!currentTsRef.current) {
      commitCurrentTimestamp(productCalendarDate(normalizeTargetTs()));
    }
    const targetDate = currentTsRef.current || productCalendarDate(normalizeTargetTs());
    const expectedTileIds = tilesRef.current.map((tile) => tile.id);
    startPlaybackCoordinator({
      targetTs: formatProductTimestampParam(targetDate),
      shouldResume: true,
      expectedTileIds,
    });
    setIsPlaying(false);
  }

  function handlePause() {
    invalidateSeekActions();
    playbackResolverRef.current.invalidate();
    cancelPlaybackCoordinator("paused");
    setIsPlaying(false);
  }

  function formatDateTimeLocalInput(value) {
    const dt = productCalendarDate(value);
    if (!Number.isFinite(dt.getTime())) return "";
    return formatLocalNaiveTs(dt);
  }

  function openExportModal() {
    if (!canExport || exportBusy) return;
    if (exportDraftRef.current) { setExportModal(exportDraftRef.current); return; }
    const center = timelineTs || currentTs || productCalendarDate(normalizeTargetTs());
    const start = new Date(center.getTime() - 15_000);
    const end = new Date(center.getTime() + 15_000);
    setExportModal({
      cameraId: "",
      title: `${TEXT.exportEvidence} ${formatPlaybackDateTime(center)}`,
      reason: "",
      startTs: formatProductTimestampParam(start),
      endTs: formatProductTimestampParam(end),
    });
  }

  function closeExportModal() {
    exportDraftRef.current = exportModal;
    exportController.stopWaiting();
    setExportModal(null);
  }

  async function submitExport() {
    if (!exportModal || exportBusy) return;
    if (!exportModal.cameraId) {
      setOperationToast({ id: `clip-validation-${Date.now()}`, title: TEXT.exportPickCamera, tone: "warning" });
      return;
    }
    const validation = validateArchiveExportSelection({ ...exportModal, productTimezone }, exportLimits);
    if (validation) {
      setOperationToast({ id: `clip-validation-${Date.now()}`, title: validation, tone: "warning" });
      return;
    }
    const payload = buildArchiveExportPayload(exportModal);
    const ownerSession = workspaceSession;
    const result = await exportController.prepare(payload);
    if (result && getAuthToken() === ownerSession) {
      setOperationToast({
        id: `chronology-clip-${result?.job?.id || Date.now()}`,
        title: TEXT.exportReady,
        tone: "success",
      });
    }
  }

  async function handleQuickDownload() {
    if (!quickDownloadCameraIds.length || quickDownloadBusy) return;
    if (quickDownloadCameraIds.length > 1) {
      setDownloadChooserOpen((prev) => !prev);
      return;
    }
    const [onlyCameraId] = quickDownloadCameraIds;
    await startQuickDownloadForCamera(onlyCameraId);
  }

  function quickDownloadTimestamp() {
    return formatProductTimestampParam(timelineTs || currentTs || productCalendarDate(normalizeTargetTs()));
  }

  async function startQuickDownloadForCamera(cameraId, { notify = true } = {}) {
    const ownerSession = workspaceSession;
    const generation = workspaceGenerationRef.current;
    const operation = { isCurrent: () => isWorkspaceCurrent(ownerSession, generation) };
    setDownloadChooserOpen(false);
    try {
      setQuickDownloadBusy(true);
      const timestamp = quickDownloadTimestamp();
      const playback = visibleTiles.map((tile) => playbackMapRef.current[tile.id])
        .find((item) => item?.hasVideo && String(item.cameraId) === String(cameraId));
      await startChronologyCurrentRecordingDownload(cameraId, timestamp, playback, operation);
      if (!operation.isCurrent()) return;
      const name = quickDownloadCameraNames[cameraId] || selectedCameraNames[cameraId] || `${TEXT.camera} ${cameraId}`;
      setDownloadResults((prev) => [...prev, { cameraId, name, status: TEXT.downloadStartedShort }].slice(-12));
      if (notify) {
        setOperationToast({
          id: `chronology-download-${cameraId}-${Date.now()}`,
          title: TEXT.quickDownloadReady,
          message: name,
          tone: "success",
        });
      }
    } catch (err) {
      if (!operation.isCurrent() || err?.name === "AbortError") return;
      const message = normalizeChronologyDownloadError(err.message);
      const name = quickDownloadCameraNames[cameraId] || selectedCameraNames[cameraId] || `${TEXT.camera} ${cameraId}`;
      setDownloadResults((prev) => [...prev, { cameraId, name, status: message }].slice(-12));
      showWorkspaceError(`${name}: ${message}`);
    } finally {
      if (operation.isCurrent()) setQuickDownloadBusy(false);
    }
  }

  async function startQuickDownloadForAllCameras() {
    if (quickDownloadBusy) return;
    setDownloadChooserOpen(false);
    setDownloadResults([]);
    for (const cameraId of quickDownloadCameraIds) {
      await startQuickDownloadForCamera(cameraId, { notify: false });
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
      : productCalendarDate(normalizeTargetTs());
    const action = startSeekAction();
    const targetTs = formatProductTimestampParam(nextDate);

    setIsPlaying(false);
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(targetTs, false);
    if (!result.applied || seekActionIdRef.current !== action.id) {
      return;
    }
    const expectedTileIds = tilesRef.current.map((tile) => tile.id);
    const coordinator = startPlaybackCoordinator({ targetTs, shouldResume: action.shouldResume, expectedTileIds });
    if (!expectedTileIds.length) {
      releasePlaybackCoordinator(coordinator.id, "released");
    }
  }

  async function handleTimelineSelect(nextDate) {
    const action = startSeekAction();
    const targetTs = formatProductTimestampParam(nextDate);

    setIsPlaying(false);
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(targetTs, false);
    if (!result.applied || seekActionIdRef.current !== action.id) {
      return;
    }
    const expectedTileIds = tilesRef.current.map((tile) => tile.id);
    const coordinator = startPlaybackCoordinator({ targetTs, shouldResume: action.shouldResume, expectedTileIds });
    if (!expectedTileIds.length) {
      releasePlaybackCoordinator(coordinator.id, "released");
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
    setPreviewTs(currentTsRef.current || productCalendarDate(normalizeTargetTs()));
    if (action.shouldResume) {
      setIsPlaying(false);
    }
  }

  async function handleTimelineDragEnd(nextDate) {
    const action = activeSeekActionRef.current;
    const targetTs = formatProductTimestampParam(nextDate);

    isScrubbingRef.current = false;
    setIsTimelinePreviewing(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(targetTs, false);
    if (!action || !result.applied || seekActionIdRef.current !== action.id) {
      return;
    }
    const expectedTileIds = tilesRef.current.map((tile) => tile.id);
    const coordinator = startPlaybackCoordinator({ targetTs, shouldResume: Boolean(action.shouldResume), expectedTileIds });
    if (!expectedTileIds.length) {
      releasePlaybackCoordinator(coordinator.id, "released");
    }
  }


  useEffect(() => {
    async function loadRanges() {
      if (!currentTs || !selectedCameraIds.length || isScrubbingRef.current) {
        if (!selectedCameraIds.length) {
          rangesInFlightRef.current?.controller.abort();
          rangesInFlightRef.current = null;
          rangesRequestIdRef.current += 1;
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
      const desired = { fromMs, toMs, zoomKey, cameraKey: selectedCameraKey };
      if (timelineRangeWindowCovers(rangesInFlightRef.current, desired)) return;
      if (rangesInFlightRef.current) {
        rangesInFlightRef.current.controller.abort();
        rangesInFlightRef.current = null;
        rangesRequestIdRef.current += 1;
      }
      if (!timelineRangesNeedReload(loaded, desired, Date.now())) {
        setRangesLoading(false);
        return;
      }
      const requestId = ++rangesRequestIdRef.current;
      const controller = new AbortController();
      const window = { ...timelineRangeWindow(centerMs, spanMs), zoomKey, cameraKey: selectedCameraKey, controller };
      rangesInFlightRef.current = window;
      const hasCurrentData = loaded?.cameraKey === selectedCameraKey;

      try {
        setRangesLoading(!timelineRangeWindowCovers(loaded, desired));
        if (!hasCurrentData) { setRangesData({}); setRangesError(false); }
        const response = await apiFetch(
          `/chronology/ranges?camera_ids=${selectedCameraKey}&from=${encodeURIComponent(formatProductTimestampParam(new Date(window.fromMs)))}&to=${encodeURIComponent(formatProductTimestampParam(new Date(window.toMs)))}`,
          { signal: controller.signal }
        );

        if (requestId !== rangesRequestIdRef.current) return;

        setRangesData(response?.items || {});
        setRangesError(false);
        loadedRangesWindowRef.current = {
          fromMs: window.fromMs,
          toMs: window.toMs,
          fetchedAt: Date.now(),
          zoomKey,
          cameraKey: selectedCameraKey,
        };
      } catch (_) {
        if (requestId !== rangesRequestIdRef.current || controller.signal.aborted) return;
        setRangesError(true);
      } finally {
        if (requestId === rangesRequestIdRef.current) {
          rangesInFlightRef.current = null;
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
        if (!fullscreenTileId) {
          setIsSystemFullscreen(false);
          setIsTimelineCollapsed(false);
        }
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
    if (!isSystemFullscreen || !timelinePanelRef.current) return undefined;
    let frameId = 0;

    function syncTimelineTabPosition() {
      if (frameId) cancelAnimationFrame(frameId);
      frameId = requestAnimationFrame(() => {
        frameId = 0;
        const panel = timelinePanelRef.current;
        const centerLine = panel?.querySelector(".chronologyTimelineCenterLine");
        if (!panel || !centerLine) return;
        const panelRect = panel.getBoundingClientRect();
        const lineRect = centerLine.getBoundingClientRect();
        if (!panelRect.width || !lineRect.width) return;
        const lineCenter = lineRect.left + lineRect.width / 2 - panelRect.left;
        panel.style.setProperty("--chronology-timeline-tab-left", `${lineCenter}px`);
      });
    }

    syncTimelineTabPosition();
    window.addEventListener("resize", syncTimelineTabPosition);
    const resizeObserver = typeof ResizeObserver !== "undefined" ? new ResizeObserver(syncTimelineTabPosition) : null;
    resizeObserver?.observe(timelinePanelRef.current);

    return () => {
      if (frameId) cancelAnimationFrame(frameId);
      window.removeEventListener("resize", syncTimelineTabPosition);
      resizeObserver?.disconnect();
    };
  }, [isSystemFullscreen, isTimelineCollapsed, timelineTs?.getTime?.(), selectedCameraKey]);

  useEffect(() => {
    function handleFullscreenChange() {
      const fullscreenElement = document.fullscreenElement;
      if (!fullscreenElement) {
        const returnState = tileFullscreenReturnStateRef.current;
        tileFullscreenReturnStateRef.current = null;
        if (returnState?.isSystemFullscreen) {
          setIsSystemFullscreen(true);
          setIsSidebarCollapsed(returnState.isSidebarCollapsed);
        } else {
          setIsSystemFullscreen(false);
          setIsTimelineCollapsed(false);
        }
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
        if (!tileFullscreenReturnStateRef.current?.isSystemFullscreen) setIsSystemFullscreen(false);
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
    tileFullscreenReturnStateRef.current = null;
    setFullscreenTileId(null);
    setIsSystemFullscreen(true);
    setIsSidebarCollapsed(false);
    setIsTimelineCollapsed(false);
    try {
      await shellRef.current?.requestFullscreen?.();
    } catch (_) {}
  }

  async function exitSystemFullscreen() {
    tileFullscreenReturnStateRef.current = null;
    setIsSystemFullscreen(false);
    setIsTimelineCollapsed(false);
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
        className={`chronologyShell ${isSystemFullscreen ? "systemFullscreen" : ""} ${isSidebarCollapsed ? "sidebarCollapsed" : "sidebarOpen"} ${isTimelineCollapsed ? "timelineCollapsed" : "timelineOpen"}`}
        data-chronology-fullscreen-active={isSystemFullscreen ? "true" : "false"}
        data-chronology-sidebar-collapsed={isSidebarCollapsed ? "true" : "false"}
        data-chronology-timeline-collapsed={isTimelineCollapsed ? "true" : "false"}
      >
        <OperationToast toast={operationToast} onClose={() => setOperationToast(null)} />
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
        {sidebarDragPreview ? (
          <div
            className={`chronologySidebarDragGhost ${sidebarDragPreview.mode}`}
            style={{ left: sidebarDragPreview.x, top: sidebarDragPreview.y }}
            aria-hidden="true"
          >
            <div className="chronologySidebarDragGhostName">{sidebarDragPreview.label}</div>
            {sidebarDragPreview.meta ? <div className="chronologySidebarDragGhostMeta">{sidebarDragPreview.meta}</div> : null}
          </div>
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
                title={TEXT.align}
                aria-label={TEXT.align}
                data-workspace-align-button="grid-2x2"
              >
                <AlignGridIcon />
              </button>
              <button
                type="button"
                className="chronologyAlignButton chronologyFullscreenButton"
                onClick={isSystemFullscreen ? exitSystemFullscreen : enterSystemFullscreen}
                title={isSystemFullscreen ? TEXT.exitFullscreen : TEXT.enterFullscreen}
                aria-label={isSystemFullscreen ? TEXT.exitFullscreen : TEXT.enterFullscreen}
              >
                {isSystemFullscreen ? "\u2715" : "\u26f6"}
              </button>
            </div>
          </div>


          <div className="chronologyCameraList">
            {orderedCameras.map((camera) => {
              const dropParts = sidebarDropParts(sidebarDropTargetCameraId);
              const isDropTarget = dropParts.cameraId === String(camera.id);
              return (
              <div
                key={camera.id}
                className={`chronologyCameraItem ${draggedSidebarCameraId === String(camera.id) ? "isReorderDragging" : ""} ${isDropTarget ? "isReorderDropTarget" : ""} ${isDropTarget && dropParts.position === "after" ? "isReorderDropAfter" : ""} ${isDropTarget && dropParts.position === "before" ? "isReorderDropBefore" : ""}`}
                data-sidebar-camera-row={String(camera.id)}
                data-touch-add-path="double-tap-card"
                data-sidebar-reorder-dragging={draggedSidebarCameraId === String(camera.id) ? "true" : "false"}
                data-sidebar-reorder-drop-target={isDropTarget ? "true" : "false"}
                data-sidebar-reorder-drop-position={isDropTarget ? dropParts.position : ""}
                data-sidebar-pointer-drag-mode={draggedSidebarCameraId === String(camera.id) ? sidebarPointerDragMode : ""}
                draggable
                onPointerDown={(event) => startSidebarPointerDrag(event, camera)}
                onPointerMove={updateSidebarPointerDrag}
                onPointerUp={(event) => finishSidebarPointerDrag(event, camera.id)}
                onPointerCancel={cancelSidebarPointerDrag}
                onDoubleClick={(event) => {
                  if (event.target?.closest?.("button, select, input, textarea, a")) return;
                  if (consumeSidebarTouchDoubleClick(event, camera.id)) return;
                  event.preventDefault();
                  addTileFromSidebar(camera.id);
                }}
                onDragStart={(event) => {
                  setDraggedSidebarCameraId(String(camera.id));
                  event.dataTransfer.setData(CHRONOLOGY_CAMERA_DROP_MIME, String(camera.id));
                  event.dataTransfer.setData(SIDEBAR_CAMERA_REORDER_MIME, String(camera.id));
                  event.dataTransfer.effectAllowed = "copyMove";
                }}
                onDragEnd={() => {
                  cancelSidebarPointerDrag();
                }}
                onDragOver={(event) => {
                  if (!event.dataTransfer.types.includes(SIDEBAR_CAMERA_REORDER_MIME)) return;
                  event.preventDefault();
                  event.dataTransfer.dropEffect = "move";
                  setSidebarDropTargetCameraId(sidebarDropToken(camera.id, sidebarDropPosition(event)));
                }}
                onDragLeave={(event) => {
                  if (!event.currentTarget.contains(event.relatedTarget)) {
                    setSidebarDropTargetCameraId((value) => (sidebarDropParts(value).cameraId === String(camera.id) ? "" : value));
                  }
                }}
                onDrop={(event) => {
                  const reorderId = event.dataTransfer.getData(SIDEBAR_CAMERA_REORDER_MIME);
                  if (!reorderId) return;
                  event.preventDefault();
                  event.stopPropagation();
                  reorderSidebarCamera(camera.id, sidebarDropPosition(event), reorderId);
                }}
              >
                <div className="chronologyCameraName">{camera.name}</div>
                <button type="button" className="workspaceSidebarDragHandle" data-sidebar-drag-handle="true" title="Перетащить камеру" aria-label={`Перетащить камеру: ${camera.name}`} onKeyDown={(event) => {
                  if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
                  event.preventDefault();
                  const index = orderedCameras.findIndex((item) => String(item.id) === String(camera.id));
                  const target = orderedCameras[index + (event.key === "ArrowUp" ? -1 : 1)];
                  if (target) reorderSidebarCamera(target.id, event.key === "ArrowUp" ? "before" : "after", camera.id);
                }}>⠿</button>
                <div className="chronologyCameraMeta">{camera.host}:{camera.port}</div>
              </div>
              );
            })}
          </div>
        </aside>

        <section className="chronologyMain">
          <div className="chronologyTimelinePanel" ref={timelinePanelRef}>
            {isSystemFullscreen ? (
              <button
                type="button"
                className="chronologyTimelineTab"
                title={isTimelineCollapsed ? TEXT.expandTimeline : TEXT.collapseTimeline}
                aria-label={isTimelineCollapsed ? TEXT.expandTimeline : TEXT.collapseTimeline}
                onClick={() => setIsTimelineCollapsed((value) => !value)}
              >
                {isTimelineCollapsed ? "\u25be" : "\u25b4"}
              </button>
            ) : null}

            <div className={`chronologyToolbar ${isTimelinePreviewing ? "isPreviewing" : ""}`}>
              <input
                type="date"
                className="chronologyDateInput"
                value={date}
                title={TEXT.currentTime}
                aria-label={TEXT.currentTime}
                onChange={(event) => { calendarTouchedRef.current = true; setDate(event.target.value); }}
              />
              <input
                type="time"
                step="1"
                className="chronologyTimeInput"
                value={time}
                title={isTimelinePreviewing ? TEXT.previewTime : TEXT.currentTime}
                aria-label={isTimelinePreviewing ? TEXT.previewTime : TEXT.currentTime}
                onChange={(event) => { calendarTouchedRef.current = true; setTime(event.target.value); }}
              />
              <button type="button" className="chronologyPrimaryButton" onClick={handleFind}>
                {TEXT.find}
              </button>
              <div className="chronologyActionWithHelp" ref={downloadChooserRef}>
                <button
                  type="button"
                  className="chronologyIconButton chronologyDownloadButton"
                  onClick={handleQuickDownload}
                  disabled={!quickDownloadCameraIds.length || quickDownloadBusy}
                  title={TEXT.quickDownload}
                  aria-label={TEXT.quickDownload}
                >
                  <ChronologyDownloadIcon />
                </button>
                <span className="chronologyHelpTooltip" tabIndex={0} aria-label={quickDownloadCameraIds.length > 1 ? TEXT.quickDownloadChooseHelp : TEXT.quickDownloadHelp}>
                  ?
                  <span role="tooltip">{quickDownloadCameraIds.length > 1 ? TEXT.quickDownloadChooseHelp : TEXT.quickDownloadHelp}</span>
                </span>
                {downloadChooserOpen ? (
                  <div className="chronologyDownloadChooser" role="menu">
                    <div className="chronologyDownloadChooserTitle">{TEXT.quickDownloadChoose}</div>
                    {quickDownloadCameraIds.map((cameraId) => (
                      <button
                        key={cameraId}
                        type="button"
                        className="chronologyDownloadChoice"
                        onClick={() => startQuickDownloadForCamera(cameraId)}
                        disabled={quickDownloadBusy}
                        role="menuitem"
                      >
                        {quickDownloadCameraNames[cameraId] || `${TEXT.camera} ${cameraId}`}
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
              <div className="chronologyActionWithHelp">
                <button
                  type="button"
                  className="chronologyIconButton chronologyEvidenceButton"
                  onClick={openExportModal}
                  disabled={!canExport || exportBusy}
                  title={TEXT.exportEvidence}
                  aria-label={TEXT.exportEvidence}
                >
                  <ChronologyScissorsIcon />
                </button>
                <span className="chronologyHelpTooltip" tabIndex={0} aria-label={TEXT.exportEvidenceHelpShort}>
                  ?
                  <span role="tooltip">{TEXT.exportEvidenceHelpShort}</span>
                </span>
              </div>
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
          </div>

          <div
            ref={workspaceRef}
            className={`chronologyWorkspace ${dragState || resizeState ? "isEditing" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = event.dataTransfer.types.includes(CHRONOLOGY_CAMERA_DROP_MIME) ? "copy" : "none";
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
                  onPointerUpCapture={(event) => handleTileSurfacePointerUp(event, tile.id)}
                  onDoubleClickCapture={async (event) => {
                    if (event.target?.closest?.("button, select, input, textarea, a, [data-video-zoom-surface]")) return;
                    event.stopPropagation();
                    await toggleTileFullscreen(tile.id);
                  }}
                >
                  <div className="chronologyTileBar">
                    <div className="chronologyTileTitle">{camera?.name || TEXT.camera}</div>
                    <button
                      type="button"
                      className="chronologyTileButton"
                      title={TEXT.close}
                      aria-label={TEXT.close}
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
                    onPointerUp={(event) => handleTileSurfacePointerUp(event, tile.id)}
                  >
                    {camera ? (
                      <ArchiveTilePlayer
                        playback={playback}
                        speed={speed}
                        isPlaying={isPlaying && playbackCoordinator?.releaseState !== "holding"}
                        allowFullscreen={false}
                        coordination={{
                          operationId: playbackCoordinator?.id || "",
                          releaseState: playbackCoordinator?.releaseState || "released",
                          targetTs: playbackCoordinator?.targetTs || "",
                          shouldResume: Boolean(playbackCoordinator?.shouldResume),
                        }}
                        tileId={tile.id}
                        onTilePlaybackState={handleTilePlaybackState}
                        onDesktopDoubleClick={() => toggleTileFullscreen(tile.id)}
                        onTouchDoubleTap={({ zone }) => handleTileVideoTouchDoubleTap(tile.id, zone)}
                      />
                    ) : (
                      <div className="chronologyMissing">{TEXT.missing}</div>
                    )}

                    {fullscreenTileId === tile.id ? (
                      <div className="chronologyFullscreenIdentity" hidden={fullscreenControlsVisible} data-fullscreen-archive-context="true">
                        <strong title={camera?.name || TEXT.camera}>{camera?.name || TEXT.camera}</strong>
                        <span>Архив — {formatPlaybackDateTime(currentTs)}{!playback.hasVideo ? ` — ${TEXT.noArchiveAtTime}` : ""}</span>
                      </div>
                    ) : null}
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
      {exportModal ? (
        <div className="modalBackdrop">
          <div className="modal archiveExportModal" role="dialog" aria-modal="true" aria-label={TEXT.exportTitle} onClick={(event) => event.stopPropagation()}>
            <div className="modalHeader">
              <h2 className="archiveExportTitle">{TEXT.exportTitle}</h2>
              <button type="button" className="iconCloseButton" onClick={closeExportModal} aria-label={TEXT.close}>
                {"\u00d7"}
              </button>
            </div>
            <div className="archiveExportForm">
              <div className="archiveExportInfo">
                {TEXT.exportHelp} {describeArchiveExportLimits(exportLimits)}
              </div>
              <div className="archiveExportFields">
                <label className="archiveExportField">
                  <span>{TEXT.exportCamera}</span>
                  <select
                    className="select"
                    value={exportModal.cameraId}
                    onChange={(event) => setExportModal((prev) => ({ ...prev, cameraId: event.target.value }))}
                    disabled={exportBusy}
                  >
                    <option value="">{TEXT.exportPickCamera}</option>
                    {exportCameraOptions.map((camera) => (
                      <option key={camera.id} value={camera.id}>
                        {camera.name}
                      </option>
                    ))}
                  </select>
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
              </div>
              <ArchiveExportResult controller={exportController} onClose={closeExportModal} />
              <div className="actions archiveExportActions">
                <button type="button" className="button primary" onClick={submitExport} disabled={exportBusy}>
                  {TEXT.exportRun}
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
