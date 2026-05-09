"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import OperatorProblemBanners from "../../components/OperatorProblemBanners";
import TilePlayer from "../../components/TilePlayer";
import { apiFetch } from "../../lib/api";
import { visibleWorkspaceTiles, workspaceCameraIds } from "../../lib/workspaceLayoutCore";

const STORAGE_KEY = "vms_live_workspace_v1";
const WORKSPACE_KEY = "live";
const MIGRATION_MARKER_PREFIX = `${STORAGE_KEY}_backend_migrated`;
const LEGACY_CANVAS_W = 1600;
const LEGACY_CANVAS_H = 900;
const MIN_TILE_W = 220;
const MIN_TILE_H = 150;
const DEFAULT_W_PCT = 0.28;
const DEFAULT_H_PCT = 0.29;

const TEXT = {
  cameras: "\u041a\u0430\u043c\u0435\u0440\u044b",
  align: "\u0412\u044b\u0440\u043e\u0432\u043d\u044f\u0442\u044c",
  addAll: "\u0412\u0441\u0435",
  loadError: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043a\u0430\u043c\u0435\u0440\u044b",
  empty: "\u041f\u0435\u0440\u0435\u0442\u0430\u0449\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443 \u043d\u0430 canvas",
  camera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
  unavailable: "\u041a\u0430\u043c\u0435\u0440\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430",
  resize: "\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0440\u0430\u0437\u043c\u0435\u0440",
  duplicate: "\u041a\u0430\u043c\u0435\u0440\u0430 \u0443\u0436\u0435 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0430",
};

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

function backendPayload(tiles) {
  return {
    layout_version: 1,
    tiles: dedupeTiles(tiles.map(normalizeTile)),
  };
}

export default function LivePage() {
  const workspaceRef = useRef(null);
  const hydratedRef = useRef(false);
  const backendReadyRef = useRef(false);
  const saveTimerRef = useRef(null);
  const [cameras, setCameras] = useState([]);
  const [camerasLoaded, setCamerasLoaded] = useState(false);
  const [tiles, setTiles] = useState([]);
  const [error, setError] = useState("");
  const [dragState, setDragState] = useState(null);
  const [resizeState, setResizeState] = useState(null);

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
      const markerKey = migrationMarkerKey(user?.id);

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
        await apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(backendPayload(localTiles)),
        });
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
        await apiFetch(`/users/me/workspaces/${WORKSPACE_KEY}/layout`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(backendPayload(tiles)),
        });
      } catch (err) {
        setError((prev) => prev || err.message || TEXT.loadError);
      }
    }, 500);
  }, [tiles]);

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
    setTiles((prev) =>
      dedupeTiles(prev.map((tile) => (tile.id === tileId ? normalizeTile({ ...tile, ...patch }) : tile)))
    );
  }

  function removeTile(tileId) {
    setTiles((prev) => prev.filter((tile) => tile.id !== tileId));
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
    const cameraId = event.dataTransfer.getData("application/x-camera-id");
    const camera = cameraMap.get(String(cameraId || ""));
    const stream = event.dataTransfer.getData("application/x-camera-stream") || defaultStream(camera);
    if (cameraId) addTile(cameraId, stream, event.clientX, event.clientY);
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

  return (
    <Layout>
      <OperatorProblemBanners domains={["live"]} className="liveWorkspaceWarnings" limit={3} />
      <div className="liveWorkspaceShell">
        <aside className="liveWorkspaceCameraPanel">
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
              >
                {TEXT.align}
              </button>
            </div>
          </div>
          {error ? <div className="liveWorkspaceError">{error}</div> : null}

          <div className="liveWorkspaceCameraList">
            {cameras.map((camera) => {
              const streams = detectStreams(camera);
              const initialStream = defaultStream(camera);

              return (
                <div
                  key={camera.id}
                  className="liveWorkspaceCameraItem"
                  draggable
                  onDragStart={(event) => {
                    event.dataTransfer.setData("application/x-camera-id", String(camera.id));
                    event.dataTransfer.setData("application/x-camera-stream", initialStream);
                    event.dataTransfer.effectAllowed = "copy";
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
                          event.dataTransfer.setData("application/x-camera-id", String(camera.id));
                          event.dataTransfer.setData("application/x-camera-stream", stream.key);
                          event.dataTransfer.effectAllowed = "copy";
                        }}
                      >
                        {stream.label}
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </aside>

        <section
          ref={workspaceRef}
          className={`liveWorkspaceCanvas ${dragState || resizeState ? "isEditing" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
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

            return (
              <div
                key={tile.id}
                className="liveWorkspaceTile"
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
                    <TilePlayer cameraId={camera.id} stream={stream} />
                  ) : (
                    <div className="liveWorkspaceMissing">{TEXT.unavailable}</div>
                  )}
                </div>

                <button
                  type="button"
                  className="liveWorkspaceResizeHandle"
                  title={TEXT.resize}
                  onPointerDown={(event) => startResize(event, tile)}
                />
              </div>
            );
          })}
        </section>
      </div>
    </Layout>
  );
}
