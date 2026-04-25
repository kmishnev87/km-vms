"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import ArchiveTilePlayer from "../../components/ArchiveTilePlayer";
import ChronologyTimeline from "../../components/ChronologyTimeline";
import { apiFetch } from "../../lib/api";

const STORAGE_KEY = "vms_chronology2_workspace_v1";
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
  loadError: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043a\u0430\u043c\u0435\u0440\u044b",
  empty: "\u041f\u0435\u0440\u0435\u0442\u0430\u0449\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443 \u043d\u0430 workspace",
  camera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
  missing: "\u041a\u0430\u043c\u0435\u0440\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430",
  resize: "\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0440\u0430\u0437\u043c\u0435\u0440",
  find: "\u041d\u0430\u0439\u0442\u0438",
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
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.map(normalizeTile) : [];
  } catch {
    return [];
  }
}

function saveTiles(tiles) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tiles.map(normalizeTile)));
  } catch {}
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

export default function Chronology2Page() {
  const initialTs = getNow();
  const initialForm = dateTimeFromDate(initialTs);
  const workspaceRef = useRef(null);
  const hydratedRef = useRef(false);
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

  const [cameras, setCameras] = useState([]);
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

  async function loadCameras() {
    try {
      setError("");
      const data = await apiFetch("/cameras");
      setCameras(Array.isArray(data) ? data : []);
    } catch (err) {
      setError(err.message || TEXT.loadError);
    }
  }

  useEffect(() => {
    setTiles(readSavedTiles());
    loadCameras();
    const timer = setTimeout(() => {
      hydratedRef.current = true;
    }, 0);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!hydratedRef.current) return;
    saveTiles(tiles);
  }, [tiles]);

  useEffect(() => {
    currentTsRef.current = currentTs;
  }, [currentTs]);

  useEffect(() => {
    tilesRef.current = tiles;
  }, [tiles]);

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

  const selectedCameraIds = useMemo(() => (
    tiles
      .map((tile) => String(tile.cameraId || ""))
      .filter(Boolean)
      .filter((value, index, arr) => arr.indexOf(value) === index)
  ), [tiles]);

  const selectedCameraNames = useMemo(() => {
    const result = {};
    selectedCameraIds.forEach((id) => {
      const camera = cameraMap.get(String(id));
      result[String(id)] = camera?.name || `${TEXT.camera} ${id}`;
    });
    return result;
  }, [selectedCameraIds, cameraMap]);

  const selectedCameraKey = selectedCameraIds.join(",");
  const tileSourceKey = tiles.map((tile) => `${tile.id}:${tile.cameraId}`).join("|");
  const timelineTs = previewTs || currentTs;

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

    const minWPct = MIN_TILE_W / Math.max(bounds.width, 1);
    const minHPct = MIN_TILE_H / Math.max(bounds.height, 1);
    const wPct = clamp(DEFAULT_W_PCT, minWPct, 0.95);
    const hPct = clamp(DEFAULT_H_PCT, minHPct, 0.95);
    const xPct = clamp((clientX - bounds.left) / bounds.width - wPct / 2, 0, 1 - wPct);
    const yPct = clamp((clientY - bounds.top) / bounds.height - 0.03, 0, 1 - hPct);

    setTiles((prev) => [
      ...prev,
      {
        id: `${cameraId}-${Date.now()}`,
        cameraId: String(cameraId),
        xPct,
        yPct,
        wPct,
        hPct,
        z: nextZIndex(prev),
      },
    ]);
  }

  function updateTile(tileId, patch) {
    setTiles((prev) =>
      prev.map((tile) => (tile.id === tileId ? normalizeTile({ ...tile, ...patch }) : tile))
    );
  }

  function removeTile(tileId) {
    setTiles((prev) => prev.filter((tile) => tile.id !== tileId));
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
    setTiles((prev) => {
      if (!prev.length) return prev;

      const bounds = workspaceBounds();
      const workspaceW = bounds?.width || 16;
      const workspaceH = bounds?.height || 9;
      const { cols, rows } = chooseAutoGrid(prev.length, workspaceW, workspaceH);
      const wPct = 1 / cols;
      const hPct = 1 / rows;

      return prev.map((tile, idx) =>
        normalizeTile({
          ...tile,
          xPct: (idx % cols) * wPct,
          yPct: Math.floor(idx / cols) * hPct,
          wPct,
          hPct,
          z: idx + 2,
        })
      );
    });
  }

  function handleDrop(event) {
    event.preventDefault();
    const cameraId = event.dataTransfer.getData("application/x-chronology-camera-id");
    if (cameraId) addTile(cameraId, event.clientX, event.clientY);
  }

  function startMove(event, tile) {
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

  function startResize(event, tile) {
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
    });
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
      const minWPct = MIN_TILE_W / Math.max(resizeState.workspaceW, 1);
      const minHPct = MIN_TILE_H / Math.max(resizeState.workspaceH, 1);
      const nextW = clamp(
        resizeState.tileW + (event.clientX - resizeState.startX) / resizeState.workspaceW,
        minWPct,
        1 - resizeState.tileX
      );
      const nextH = clamp(
        resizeState.tileH + (event.clientY - resizeState.startY) / resizeState.workspaceH,
        minHPct,
        1 - resizeState.tileY
      );
      updateTile(resizeState.id, { wPct: nextW, hPct: nextH });
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
        playbackKey: sameSource
          ? prev.playbackKey
          : `${tile.id}-${cameraId || "empty"}-${relPath || "empty"}-${offsetSec}-${Date.now()}`,
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
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(targetDate);
    await resolvePlaybackForTimestamp(formatLocalNaiveTs(targetDate), true);
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

  async function seekBySeconds(seconds) {
    const nextDate = currentTsRef.current
      ? new Date(currentTsRef.current.getTime() + seconds * 1000)
      : new Date(normalizeTargetTs());
    const action = startSeekAction();

    setIsPlaying(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(formatLocalNaiveTs(nextDate), true);
    if (result.applied && action.shouldResume && seekActionIdRef.current === action.id) {
      setIsPlaying(true);
    }
  }

  async function handleTimelineSelect(nextDate) {
    const action = startSeekAction();

    setIsPlaying(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(formatLocalNaiveTs(nextDate), true);
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
    setPreviewTs(nextDate);
    syncFormDateTime(nextDate);
  }

  function handleTimelineDragStart() {
    const action = startSeekAction();
    isScrubbingRef.current = true;
    playWasActiveRef.current = action.shouldResume;
    setPreviewTs(currentTsRef.current || new Date(normalizeTargetTs()));
    if (action.shouldResume) {
      setIsPlaying(false);
    }
  }

  async function handleTimelineDragEnd(nextDate) {
    const action = activeSeekActionRef.current;

    isScrubbingRef.current = false;
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(nextDate);

    const result = await resolvePlaybackForTimestamp(formatLocalNaiveTs(nextDate), true);
    if (action && result.applied && action.shouldResume && seekActionIdRef.current === action.id) {
      setIsPlaying(true);
    }
  }

  useEffect(() => {
    if (!currentTs || !tileSourceKey) {
      setPlaybackMap({});
      return;
    }

    resolvePlaybackForTimestamp(formatLocalNaiveTs(currentTs), true);
  }, [tileSourceKey]);

  useEffect(() => {
    if (!isPlaying || !currentTs || isScrubbingRef.current) return;
    resolvePlaybackForTimestamp(formatLocalNaiveTs(currentTs), false);
  }, [currentTs, isPlaying]);

  useEffect(() => {
    const requestId = ++rangesRequestIdRef.current;

    async function loadRanges() {
      if (!currentTs || !selectedCameraIds.length || isScrubbingRef.current) {
        if (!selectedCameraIds.length) {
          setRangesData({});
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
        const response = await apiFetch(
          `/chronology/ranges?camera_ids=${selectedCameraKey}&from=${encodeURIComponent(formatLocalNaiveTs(new Date(fromMs)))}&to=${encodeURIComponent(formatLocalNaiveTs(new Date(toMs)))}`
        );

        if (requestId !== rangesRequestIdRef.current) return;

        setRangesData(response?.items || {});
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
      }
    }

    loadRanges();
  }, [currentTs, zoomKey, selectedCameraKey]);

  return (
    <Layout>
      <div className="chronology2Shell">
        <aside className="chronology2CameraPanel">
          <div className="chronology2PanelHeader">
            <div className="chronology2PanelTitle">{TEXT.cameras}</div>
            <button
              type="button"
              className="chronology2AlignButton"
              onClick={autoLayoutTiles}
              disabled={!tiles.length}
            >
              {TEXT.align}
            </button>
          </div>

          {error ? <div className="chronology2Error">{error}</div> : null}

          <div className="chronology2CameraList">
            {cameras.map((camera) => (
              <div
                key={camera.id}
                className="chronology2CameraItem"
                draggable
                onDragStart={(event) => {
                  event.dataTransfer.setData("application/x-chronology-camera-id", String(camera.id));
                  event.dataTransfer.effectAllowed = "copy";
                }}
              >
                <div className="chronology2CameraName">{camera.name}</div>
                <div className="chronology2CameraMeta">{camera.host}:{camera.port}</div>
              </div>
            ))}
          </div>
        </aside>

        <section className="chronology2Main">
          <div className="chronology2Toolbar">
            <input
              type="date"
              className="chronology2DateInput"
              value={date}
              onChange={(event) => setDate(event.target.value)}
            />
            <input
              type="time"
              step="1"
              className="chronology2TimeInput"
              value={time}
              onChange={(event) => setTime(event.target.value)}
            />
            <button type="button" className="chronology2PrimaryButton" onClick={handleFind}>
              {TEXT.find}
            </button>
            <button type="button" className="chronology2IconButton" onClick={() => seekBySeconds(-10)} title="-10">-10</button>
            <button type="button" className="chronology2IconButton" onClick={handlePlay} title="Play">{"\u25b6"}</button>
            <button type="button" className="chronology2IconButton" onClick={handlePause} title="Pause">{"\u275a\u275a"}</button>
            <button type="button" className="chronology2IconButton" onClick={() => seekBySeconds(10)} title="+10">+10</button>

            <div className="chronology2SpeedGroup">
              {SPEED_OPTIONS.map((item) => (
                <button
                  key={item.value}
                  type="button"
                  className={`chronology2SpeedButton ${speed === item.value ? "active" : ""}`}
                  onClick={() => setSpeed(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>

            <div className="chronology2TimeBox">{formatPlaybackDateTime(timelineTs)}</div>
          </div>

          <div className="chronology2TimelineWrap">
            <ChronologyTimeline
              currentTs={timelineTs}
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
              compact
            />
          </div>

          <div
            ref={workspaceRef}
            className={`chronology2Workspace ${dragState || resizeState ? "isEditing" : ""}`}
            onDragOver={(event) => {
              event.preventDefault();
              event.dataTransfer.dropEffect = "copy";
            }}
            onDrop={handleDrop}
          >
            {!tiles.length ? (
              <div className="chronology2Empty">
                <div className="chronology2EmptyTitle">{TEXT.empty}</div>
              </div>
            ) : null}

            {tiles.map((tile) => {
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
                  className="chronology2Tile"
                  style={{
                    left: `${tile.xPct * 100}%`,
                    top: `${tile.yPct * 100}%`,
                    width: `${tile.wPct * 100}%`,
                    height: `${tile.hPct * 100}%`,
                    zIndex: tile.z || 2,
                  }}
                  onPointerDown={(event) => startMove(event, tile)}
                >
                  <div className="chronology2TileBar">
                    <div className="chronology2TileTitle">{camera?.name || TEXT.camera}</div>
                    <button
                      type="button"
                      className="chronology2TileButton"
                      title={TEXT.close}
                      onPointerDown={(event) => event.stopPropagation()}
                      onClick={() => removeTile(tile.id)}
                    >
                      {"\u00d7"}
                    </button>
                  </div>

                  <div className="chronology2TileVideo">
                    {camera ? (
                      <ArchiveTilePlayer
                        playback={playback}
                        speed={speed}
                        isPlaying={isPlaying}
                        allowFullscreen={false}
                      />
                    ) : (
                      <div className="chronology2Missing">{TEXT.missing}</div>
                    )}
                  </div>

                  <button
                    type="button"
                    className="chronology2ResizeHandle"
                    title={TEXT.resize}
                    onPointerDown={(event) => startResize(event, tile)}
                  />
                </div>
              );
            })}
          </div>
        </section>
      </div>
    </Layout>
  );
}
