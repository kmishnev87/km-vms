"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import OperatorProblemBanners from "../../components/OperatorProblemBanners";
import TilePlayer from "../../components/TilePlayer";
import { apiFetch } from "../../lib/api";
import { useI18n } from "../../lib/i18n";
import {
  LIVE_CAMERA_DROP_MIME,
  LIVE_CAMERA_STREAM_DROP_MIME,
  SIDEBAR_CAMERA_REORDER_MIME,
  mergeSidebarCameraOrder,
  resizeWorkspaceTile,
  sanitizeSidebarCameraOrder,
  visibleWorkspaceTiles,
  workspaceCameraIds,
} from "../../lib/workspaceLayoutCore";

const STORAGE_KEY = "vms_live_workspace_v1";
const WORKSPACE_KEY = "live";
const MIGRATION_MARKER_PREFIX = `${STORAGE_KEY}_backend_migrated`;
const LEGACY_CANVAS_W = 1600;
const LEGACY_CANVAS_H = 900;
const MIN_TILE_W = 220;
const MIN_TILE_H = 150;
const DEFAULT_W_PCT = 0.28;
const DEFAULT_H_PCT = 0.29;

const LIVE_TEXT = {
  cameras: "\u041a\u0430\u043c\u0435\u0440\u044b",
  align: "\u0412\u044b\u0440\u043e\u0432\u043d\u044f\u0442\u044c",
  addAll: "\u0412\u0441\u0435",
  loadError: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043a\u0430\u043c\u0435\u0440\u044b",
  empty: "\u041f\u0435\u0440\u0435\u0442\u0430\u0449\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443 \u043d\u0430 canvas",
  camera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
  audioOn: "\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0437\u0432\u0443\u043a",
  audioOff: "\u0412\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0437\u0432\u0443\u043a",
  audioUnavailable: "\u0410\u0443\u0434\u0438\u043e \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e",
  audioDisabled: "\u0410\u0443\u0434\u0438\u043e \u043e\u0442\u043a\u043b\u044e\u0447\u0435\u043d\u043e",
  audioBlocked: "\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u0437\u0430\u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u0430\u043b \u0437\u0432\u0443\u043a",
  enterFullscreen: "\u0412\u043e \u0432\u0435\u0441\u044c \u044d\u043a\u0440\u0430\u043d",
  exitFullscreen: "\u0412\u044b\u0439\u0442\u0438 \u0438\u0437 fullscreen",
  collapseSidebar: "\u0421\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c \u043a\u0430\u043c\u0435\u0440",
  expandSidebar: "\u041e\u0442\u043a\u0440\u044b\u0442\u044c \u043f\u0430\u043d\u0435\u043b\u044c \u043a\u0430\u043c\u0435\u0440",
  unavailable: "\u041a\u0430\u043c\u0435\u0440\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430",
  resize: "\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0440\u0430\u0437\u043c\u0435\u0440",
  duplicate: "\u041a\u0430\u043c\u0435\u0440\u0430 \u0443\u0436\u0435 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0430",
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

function detectStreams(camera) {
  const result = [];
  if (camera?.rtsp_main_url) result.push({ key: "main", label: "Main" });
  if (camera?.rtsp_sub_url) result.push({ key: "sub", label: "Sub" });
  if (!result.length) result.push({ key: "main", label: "Main" });
  return result;
}

function defaultStream(camera) {
  const streams = detectStreams(camera);
  const preferred = String(camera?.default_live_stream || "").toLowerCase();
  if (streams.some((item) => item.key === preferred)) return preferred;
  if (streams.some((item) => item.key === "sub")) return "sub";
  return streams[0]?.key || "main";
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
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
      id: String(tile.id || `${tile.cameraId}-${tile.stream}-${Date.now()}`),
      cameraId: String(tile.cameraId || ""),
      stream: tile.stream || "sub",
      xPct: clamp(Number(tile.xPct), 0, 1 - wPct),
      yPct: clamp(Number(tile.yPct), 0, 1 - hPct),
      wPct,
      hPct,
      z: Number(tile.z || 2),
    };
  }

  const wPct = clamp(Number(tile?.w || 420) / LEGACY_CANVAS_W, 0.05, 1);
  const hPct = clamp(Number(tile?.h || 260) / LEGACY_CANVAS_H, 0.05, 1);
  return {
    id: String(tile?.id || `${tile?.cameraId || "camera"}-${tile?.stream || "sub"}-${Date.now()}`),
    cameraId: String(tile?.cameraId || ""),
    stream: tile?.stream || "sub",
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
    return Array.isArray(parsed) ? dedupeTiles(parsed.map(normalizeTile)) : [];
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

function backendPayload(tiles, sidebarCameraOrder = []) {
  return {
    layout_version: 1,
    tiles: dedupeTiles(tiles.map(normalizeTile)),
    sidebarCameraOrder: sanitizeSidebarCameraOrder(sidebarCameraOrder),
  };
}

function defaultAudioFact() {
  return {
    audioAvailable: false,
    audioDisabledByConfig: false,
    audioReason: "unknown",
  };
}

export default function LivePage() {
  const { text } = useI18n();
  const TEXT = useMemo(
    () => Object.fromEntries(Object.entries(LIVE_TEXT).map(([key, value]) => [key, text(value)])),
    [text]
  );
  const workspaceRef = useRef(null);
  const shellRef = useRef(null);
  const hydratedRef = useRef(false);
  const backendReadyRef = useRef(false);
  const saveTimerRef = useRef(null);
  const lastBackendLayoutPayloadRef = useRef("");
  const [cameras, setCameras] = useState([]);
  const [camerasLoaded, setCamerasLoaded] = useState(false);
  const [tiles, setTiles] = useState([]);
  const [sidebarCameraOrder, setSidebarCameraOrder] = useState([]);
  const [lastPersistedSidebarCameraOrder, setLastPersistedSidebarCameraOrder] = useState([]);
  const [error, setError] = useState("");
  const [dragState, setDragState] = useState(null);
  const [resizeState, setResizeState] = useState(null);
  const [draggedSidebarCameraId, setDraggedSidebarCameraId] = useState("");
  const [sidebarDropTargetCameraId, setSidebarDropTargetCameraId] = useState("");
  const [isSystemFullscreen, setIsSystemFullscreen] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [activeAudioTileId, setActiveAudioTileId] = useState("");
  const [audioRequestId, setAudioRequestId] = useState(0);
  const [audioFactsByTileId, setAudioFactsByTileId] = useState({});

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
      backendReadyRef.current = true;
      const backendTiles = Array.isArray(layout?.tiles) ? dedupeTiles(layout.tiles.map(normalizeTile)) : [];
      const backendSidebarOrder = sanitizeSidebarCameraOrder(layout?.sidebarCameraOrder);
      const markerKey = migrationMarkerKey(user?.id);
      setSidebarCameraOrder(backendSidebarOrder);
      setLastPersistedSidebarCameraOrder(backendSidebarOrder);
      lastBackendLayoutPayloadRef.current = JSON.stringify(backendPayload(backendTiles, backendSidebarOrder));

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
        const payload = backendPayload(localTiles, backendSidebarOrder);
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
      setSidebarCameraOrder([]);
      setLastPersistedSidebarCameraOrder([]);
      setError((prev) => prev || err.message || TEXT.loadError);
    } finally {
      hydratedRef.current = true;
    }
  }

  useEffect(() => {
    loadWorkspaceLayout();
    loadCameras();
    const timer = setInterval(loadCameras, 8000);
    return () => {
      clearInterval(timer);
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
        const payload = backendPayload(tiles, sidebarCameraOrder);
        const payloadText = JSON.stringify(payload);
        if (payloadText === lastBackendLayoutPayloadRef.current) return;
        const saved = await apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: payloadText,
        });
        const savedOrder = sanitizeSidebarCameraOrder(saved?.sidebarCameraOrder);
        setLastPersistedSidebarCameraOrder(savedOrder);
        setSidebarCameraOrder(savedOrder);
        lastBackendLayoutPayloadRef.current = JSON.stringify(backendPayload(saved?.tiles || tiles, savedOrder));
      } catch (err) {
        setSidebarCameraOrder(lastPersistedSidebarCameraOrder);
        setError((prev) => prev || err.message || TEXT.loadError);
      }
    }, 500);
  }, [tiles, sidebarCameraOrder, lastPersistedSidebarCameraOrder]);

  const cameraMap = useMemo(() => {
    const map = new Map();
    cameras.forEach((camera) => map.set(String(camera.id), camera));
    return map;
  }, [cameras]);

  const visibleTiles = useMemo(() => {
    if (!camerasLoaded) return tiles;
    return visibleWorkspaceTiles(tiles, cameras);
  }, [tiles, cameras, camerasLoaded]);

  useEffect(() => {
    if (!activeAudioTileId) return;
    if (!visibleTiles.some((tile) => tile.id === activeAudioTileId)) {
      setActiveAudioTileId("");
    }
  }, [activeAudioTileId, visibleTiles]);

  const orderedCameras = useMemo(
    () => mergeSidebarCameraOrder(cameras, sidebarCameraOrder),
    [cameras, sidebarCameraOrder]
  );

  function activeLayoutTiles(source) {
    return camerasLoaded ? visibleWorkspaceTiles(source, cameras) : dedupeTiles(source);
  }

  function workspaceBounds() {
    return workspaceRef.current?.getBoundingClientRect() || null;
  }

  function addTile(cameraId, stream, clientX, clientY) {
    const bounds = workspaceBounds();
    if (!bounds) return;
    const normalizedCameraId = String(cameraId || "");
    const camera = cameraMap.get(normalizedCameraId);
    const streams = detectStreams(camera);
    const selectedStream = streams.some((item) => item.key === stream)
      ? stream
      : defaultStream(camera);

    const minWPct = MIN_TILE_W / Math.max(bounds.width, 1);
    const minHPct = MIN_TILE_H / Math.max(bounds.height, 1);
    const wPct = clamp(DEFAULT_W_PCT, minWPct, 0.9);
    const hPct = clamp(DEFAULT_H_PCT, minHPct, 0.9);
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
          id: `${cameraId}-${selectedStream}-${Date.now()}`,
          cameraId: normalizedCameraId,
          stream: selectedStream,
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
    if (patch?.stream) {
      setAudioFactsByTileId((prev) => ({ ...prev, [tileId]: defaultAudioFact() }));
      setActiveAudioTileId((current) => (current === tileId ? "" : current));
    }
    setTiles((prev) =>
      dedupeTiles(prev.map((tile) => (tile.id === tileId ? normalizeTile({ ...tile, ...patch }) : tile)))
    );
  }

  function removeTile(tileId) {
    setActiveAudioTileId((current) => (current === tileId ? "" : current));
    setAudioFactsByTileId((prev) => {
      if (!Object.prototype.hasOwnProperty.call(prev, tileId)) return prev;
      const next = { ...prev };
      delete next[tileId];
      return next;
    });
    setTiles((prev) => prev.filter((tile) => tile.id !== tileId));
  }

  function handleAudioStatusChange(tileId, fact) {
    setAudioFactsByTileId((prev) => ({ ...prev, [tileId]: { ...defaultAudioFact(), ...(fact || {}) } }));
    if (fact?.audioAvailable === false || fact?.audioDisabledByConfig) {
      setActiveAudioTileId((current) => (current === tileId ? "" : current));
    }
  }

  function toggleTileAudio(tileId) {
    const fact = audioFactsByTileId[tileId] || defaultAudioFact();
    if (fact.audioDisabledByConfig || !fact.audioAvailable) return;
    setActiveAudioTileId((current) => (current === tileId ? "" : tileId));
    setAudioRequestId((value) => value + 1);
  }

  function audioButtonState(tileId) {
    const fact = audioFactsByTileId[tileId] || defaultAudioFact();
    const active = activeAudioTileId === tileId && fact.audioAvailable && !fact.audioDisabledByConfig;
    const disabled = fact.audioDisabledByConfig || !fact.audioAvailable;
    const title = active
      ? TEXT.audioOff
      : fact.audioDisabledByConfig
      ? TEXT.audioDisabled
      : fact.audioAvailable
      ? TEXT.audioOn
      : TEXT.audioUnavailable;
    return { fact, active, disabled, title };
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
            id: `${camera.id}-${defaultStream(camera)}-${Date.now()}-${idx}`,
            cameraId: String(camera.id),
            stream: defaultStream(camera),
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
    const cameraId = event.dataTransfer.getData(LIVE_CAMERA_DROP_MIME);
    const camera = cameraMap.get(String(cameraId || ""));
    const stream = event.dataTransfer.getData(LIVE_CAMERA_STREAM_DROP_MIME) || defaultStream(camera);
    if (cameraId) addTile(cameraId, stream, event.clientX, event.clientY);
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

  function reorderSidebarCamera(targetCameraId, position = "before") {
    const sourceId = String(draggedSidebarCameraId || "");
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

  async function enterSystemFullscreen() {
    setIsSystemFullscreen(true);
    setIsSidebarCollapsed(false);
    try {
      await shellRef.current?.requestFullscreen?.();
    } catch (_) {}
  }

  async function exitSystemFullscreen() {
    setIsSystemFullscreen(false);
    setIsSidebarCollapsed(false);
    if (document.fullscreenElement) {
      try {
        await document.exitFullscreen?.();
      } catch (_) {}
    }
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

  function startResize(event, tile, corner = "bottom-right") {
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

  useEffect(() => {
    document.body.classList.toggle("liveSystemFullscreenBody", isSystemFullscreen);
    return () => document.body.classList.remove("liveSystemFullscreenBody");
  }, [isSystemFullscreen]);

  useEffect(() => {
    function handleFullscreenChange() {
      if (document.fullscreenElement === shellRef.current) {
        setIsSystemFullscreen(true);
        return;
      }
      setIsSystemFullscreen(false);
      setIsSidebarCollapsed(false);
    }

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  useEffect(() => {
    function handleKeyDown(event) {
      if (event.key === "Escape" && isSystemFullscreen) {
        setIsSystemFullscreen(false);
        setIsSidebarCollapsed(false);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isSystemFullscreen]);

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

  return (
    <Layout>
      <OperatorProblemBanners domains={["live"]} className="liveWorkspaceWarnings" limit={3} />
      <div
        ref={shellRef}
        className={`liveWorkspaceShell ${isSystemFullscreen ? "systemFullscreen" : ""} ${isSidebarCollapsed ? "sidebarCollapsed" : "sidebarOpen"}`}
        data-live-fullscreen-active={isSystemFullscreen ? "true" : "false"}
        data-live-sidebar-collapsed={isSidebarCollapsed ? "true" : "false"}
        data-live-sidebar-order={orderedCameras.map((camera) => String(camera.id)).join(",")}
      >
        {isSystemFullscreen ? (
          <button
            type="button"
            className="liveWorkspaceSidebarTab"
            title={isSidebarCollapsed ? TEXT.expandSidebar : TEXT.collapseSidebar}
            aria-label={isSidebarCollapsed ? TEXT.expandSidebar : TEXT.collapseSidebar}
            onClick={() => setIsSidebarCollapsed((value) => !value)}
          >
            {isSidebarCollapsed ? "\u203a" : "\u2039"}
          </button>
        ) : null}
        {isSystemFullscreen ? (
          <button
            type="button"
            className="liveWorkspaceFullscreenExitButton"
            onClick={exitSystemFullscreen}
            title={TEXT.exitFullscreen}
            aria-label={TEXT.exitFullscreen}
          >
            {"\u2715"}
          </button>
        ) : null}
        <aside className="liveWorkspaceCameraPanel" aria-hidden={isSystemFullscreen && isSidebarCollapsed}>
          <div className="liveWorkspacePanelHeader">
            <div className="liveWorkspacePanelTitle">{TEXT.cameras}</div>
            <div className="liveWorkspacePanelActions">
              <button
                type="button"
                className="liveWorkspaceAlignButton"
                onClick={addAllCameras}
                disabled={!cameras.length}
              >
                {TEXT.addAll}
              </button>
              <button
                type="button"
                className="liveWorkspaceAlignButton"
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
                className="liveWorkspaceAlignButton liveWorkspaceFullscreenButton"
                onClick={isSystemFullscreen ? exitSystemFullscreen : enterSystemFullscreen}
                title={isSystemFullscreen ? TEXT.exitFullscreen : TEXT.enterFullscreen}
                aria-label={isSystemFullscreen ? TEXT.exitFullscreen : TEXT.enterFullscreen}
              >
                {isSystemFullscreen ? "\u2715" : "\u26f6"}
              </button>
            </div>
          </div>
          {error ? <div className="liveWorkspaceError">{error}</div> : null}

          <div className="liveWorkspaceCameraList">
            {orderedCameras.map((camera) => {
              const streams = detectStreams(camera);
              const initialStream = defaultStream(camera);

              return (
                (() => {
                  const dropParts = sidebarDropParts(sidebarDropTargetCameraId);
                  const isDropTarget = dropParts.cameraId === String(camera.id);
                  return (
                <div
                  key={camera.id}
                  className={`liveWorkspaceCameraItem ${draggedSidebarCameraId === String(camera.id) ? "isReorderDragging" : ""} ${isDropTarget ? "isReorderDropTarget" : ""} ${isDropTarget && dropParts.position === "after" ? "isReorderDropAfter" : ""} ${isDropTarget && dropParts.position === "before" ? "isReorderDropBefore" : ""}`}
                  data-sidebar-camera-row={String(camera.id)}
                  data-sidebar-reorder-dragging={draggedSidebarCameraId === String(camera.id) ? "true" : "false"}
                  data-sidebar-reorder-drop-target={isDropTarget ? "true" : "false"}
                  data-sidebar-reorder-drop-position={isDropTarget ? dropParts.position : ""}
                  draggable
                  onDragStart={(event) => {
                    setDraggedSidebarCameraId(String(camera.id));
                    event.dataTransfer.setData(LIVE_CAMERA_DROP_MIME, String(camera.id));
                    event.dataTransfer.setData(LIVE_CAMERA_STREAM_DROP_MIME, initialStream);
                    event.dataTransfer.setData(SIDEBAR_CAMERA_REORDER_MIME, String(camera.id));
                    event.dataTransfer.effectAllowed = "copyMove";
                  }}
                  onDragEnd={() => {
                    setDraggedSidebarCameraId("");
                    setSidebarDropTargetCameraId("");
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
                    reorderSidebarCamera(camera.id, sidebarDropPosition(event));
                  }}
                >
                  <div className="liveWorkspaceCameraName">{camera.name}</div>
                  <div className="liveWorkspaceCameraMeta">{camera.host}:{camera.port}</div>
                  <div className="liveWorkspaceStreamButtons">
                    {streams.map((stream) => (
                      <button
                        key={stream.key}
                        type="button"
                        className="liveWorkspaceStreamButton"
                        draggable
                        onDragStart={(event) => {
                          event.stopPropagation();
                          event.dataTransfer.setData(LIVE_CAMERA_DROP_MIME, String(camera.id));
                          event.dataTransfer.setData(LIVE_CAMERA_STREAM_DROP_MIME, stream.key);
                          event.dataTransfer.effectAllowed = "copy";
                        }}
                      >
                        {stream.label}
                      </button>
                    ))}
                  </div>
                </div>
                  );
                })()
              );
            })}
          </div>
        </aside>

        <section
          ref={workspaceRef}
          className={`liveWorkspaceCanvas ${dragState || resizeState ? "isEditing" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = event.dataTransfer.types.includes(LIVE_CAMERA_DROP_MIME) ? "copy" : "none";
          }}
          onDrop={handleDrop}
        >
          {!visibleTiles.length ? (
            <div className="liveWorkspaceEmpty">
              <div className="liveWorkspaceEmptyTitle">{TEXT.empty}</div>
            </div>
          ) : null}

          {visibleTiles.map((tile) => {
            const camera = cameraMap.get(String(tile.cameraId));
            const streams = detectStreams(camera);
            const stream = streams.some((item) => item.key === tile.stream)
              ? tile.stream
              : defaultStream(camera);
            const audioState = audioButtonState(tile.id);

            return (
              <div
                key={tile.id}
                className={`liveWorkspaceTile ${audioState.active ? "audioActive" : ""}`}
                data-active-audio-tile={audioState.active ? "true" : "false"}
                data-audio-available={audioState.fact.audioAvailable ? "true" : "false"}
                data-audio-disabled-by-config={audioState.fact.audioDisabledByConfig ? "true" : "false"}
                data-audio-reason={audioState.fact.audioReason || "unknown"}
                style={{
                  left: `${tile.xPct * 100}%`,
                  top: `${tile.yPct * 100}%`,
                  width: `${tile.wPct * 100}%`,
                  height: `${tile.hPct * 100}%`,
                  zIndex: tile.z || 2,
                }}
                onPointerDown={(event) => startMove(event, tile)}
              >
                <div className="liveWorkspaceTileBar">
                  <div className="liveWorkspaceTileTitle">{camera?.name || TEXT.camera}</div>
                  <select
                    className="liveWorkspaceTileSelect"
                    value={stream}
                    onPointerDown={(event) => event.stopPropagation()}
                    onChange={(event) => updateTile(tile.id, { stream: event.target.value })}
                  >
                    {streams.map((item) => (
                      <option key={item.key} value={item.key}>{item.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className={`liveWorkspaceIconButton liveWorkspaceAudioButton ${audioState.active ? "isActive" : ""}`}
                    title={audioState.title}
                    aria-label={audioState.title}
                    aria-pressed={audioState.active ? "true" : "false"}
                    disabled={audioState.disabled}
                    data-live-audio-button="true"
                    data-audio-state={audioState.active ? "active" : audioState.disabled ? "unavailable" : "available"}
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={() => toggleTileAudio(tile.id)}
                  >
                    <svg className="liveWorkspaceAudioIcon" viewBox="0 0 24 24" aria-hidden="true">
                      <path
                        className="liveWorkspaceAudioSpeaker"
                        d="M4 9.5v5h4l5 4.5V5L8 9.5H4Z"
                      />
                      <path
                        className="liveWorkspaceAudioWave one"
                        d="M16 9c.9.9 1.4 1.9 1.4 3s-.5 2.1-1.4 3"
                      />
                      <path
                        className="liveWorkspaceAudioWave two"
                        d="M18.7 6.5c1.6 1.5 2.5 3.4 2.5 5.5s-.9 4-2.5 5.5"
                      />
                    </svg>
                  </button>
                  <button
                    type="button"
                    className="liveWorkspaceIconButton"
                    title={TEXT.close}
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={() => removeTile(tile.id)}
                  >
                    {"\u00d7"}
                  </button>
                </div>

                <div className="liveWorkspaceTileVideo">
                  {camera ? (
                    <TilePlayer
                      cameraId={camera.id}
                      stream={stream}
                      audioEnabled={audioState.active}
                      audioRequestId={audioRequestId}
                      onAudioStatusChange={(fact) => handleAudioStatusChange(tile.id, fact)}
                      onAudioPlaybackBlocked={() => {
                        setActiveAudioTileId((current) => (current === tile.id ? "" : current));
                        setError(TEXT.audioBlocked);
                      }}
                    />
                  ) : (
                    <div className="liveWorkspaceMissing">{TEXT.unavailable}</div>
                  )}
                </div>

                {["top-left", "top-right", "bottom-left", "bottom-right"].map((corner) => (
                  <button
                    key={corner}
                    type="button"
                    className={`workspaceResizeHandle liveWorkspaceResizeHandle ${corner}`}
                    title={TEXT.resize}
                    aria-label={TEXT.resize}
                    onPointerDown={(event) => startResize(event, tile, corner)}
                  />
                ))}
              </div>
            );
          })}
        </section>
      </div>
    </Layout>
  );
}
