"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import TilePlayer from "../../components/TilePlayer";
import { apiFetch } from "../../lib/api";

const STORAGE_KEY = "vms_live2_workspace_v1";
const MIN_TILE_W = 260;
const MIN_TILE_H = 170;
const DEFAULT_TILE_W = 420;
const DEFAULT_TILE_H = 260;
const TEXT = {
  cameras: "\u041a\u0430\u043c\u0435\u0440\u044b",
  loadError: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043a\u0430\u043c\u0435\u0440\u044b",
  empty: "\u041f\u0435\u0440\u0435\u0442\u0430\u0449\u0438\u0442\u0435 \u043a\u0430\u043c\u0435\u0440\u0443 \u043d\u0430 canvas",
  camera: "\u041a\u0430\u043c\u0435\u0440\u0430",
  close: "\u0417\u0430\u043a\u0440\u044b\u0442\u044c",
  unavailable: "\u041a\u0430\u043c\u0435\u0440\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430",
  resize: "\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0440\u0430\u0437\u043c\u0435\u0440",
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
  if (streams.some((item) => item.key === "sub")) return "sub";
  return streams[0]?.key || "main";
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function readSavedTiles() {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveTiles(tiles) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tiles));
  } catch {}
}

function nextZIndex(tiles) {
  return tiles.reduce((max, tile) => Math.max(max, Number(tile.z || 2)), 2) + 1;
}

export default function Live2Page() {
  const workspaceRef = useRef(null);
  const hydratedRef = useRef(false);
  const [cameras, setCameras] = useState([]);
  const [tiles, setTiles] = useState([]);
  const [error, setError] = useState("");
  const [dragState, setDragState] = useState(null);
  const [resizeState, setResizeState] = useState(null);

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
    hydratedRef.current = true;
    loadCameras();
    const timer = setInterval(loadCameras, 8000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!hydratedRef.current) return;
    saveTiles(tiles);
  }, [tiles]);

  const cameraMap = useMemo(() => {
    const map = new Map();
    cameras.forEach((camera) => map.set(String(camera.id), camera));
    return map;
  }, [cameras]);

  function workspaceBounds() {
    return workspaceRef.current?.getBoundingClientRect() || null;
  }

  function addTile(cameraId, stream, clientX, clientY) {
    const bounds = workspaceBounds();
    if (!bounds) return;

    const tileW = Math.min(DEFAULT_TILE_W, Math.max(MIN_TILE_W, bounds.width - 16));
    const tileH = Math.min(DEFAULT_TILE_H, Math.max(MIN_TILE_H, bounds.height - 16));
    const x = clamp(clientX - bounds.left - tileW / 2, 8, Math.max(8, bounds.width - tileW - 8));
    const y = clamp(clientY - bounds.top - 28, 8, Math.max(8, bounds.height - tileH - 8));

    setTiles((prev) => {
      const z = nextZIndex(prev);
      return [
        ...prev,
        {
          id: `${cameraId}-${stream}-${Date.now()}`,
          cameraId: String(cameraId),
          stream,
          x,
          y,
          w: tileW,
          h: tileH,
          z,
        },
      ];
    });
  }

  function updateTile(tileId, patch) {
    setTiles((prev) =>
      prev.map((tile) => (tile.id === tileId ? { ...tile, ...patch } : tile))
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

  function handleDrop(event) {
    event.preventDefault();
    const cameraId = event.dataTransfer.getData("application/x-camera-id");
    const stream = event.dataTransfer.getData("application/x-camera-stream") || "sub";
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
      tileX: tile.x,
      tileY: tile.y,
      maxX: Math.max(8, bounds.width - tile.w - 8),
      maxY: Math.max(8, bounds.height - tile.h - 8),
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
      tileW: tile.w,
      tileH: tile.h,
      maxW: Math.max(MIN_TILE_W, bounds.width - tile.x - 8),
      maxH: Math.max(MIN_TILE_H, bounds.height - tile.y - 8),
    });
  }

  useEffect(() => {
    if (!dragState) return undefined;

    function onMove(event) {
      const nextX = clamp(dragState.tileX + event.clientX - dragState.startX, 8, dragState.maxX);
      const nextY = clamp(dragState.tileY + event.clientY - dragState.startY, 8, dragState.maxY);
      updateTile(dragState.id, { x: nextX, y: nextY });
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
      const nextW = clamp(resizeState.tileW + event.clientX - resizeState.startX, MIN_TILE_W, resizeState.maxW);
      const nextH = clamp(resizeState.tileH + event.clientY - resizeState.startY, MIN_TILE_H, resizeState.maxH);
      updateTile(resizeState.id, { w: nextW, h: nextH });
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
      <div className="live2Shell">
        <aside className="live2CameraPanel">
          <div className="live2PanelTitle">{TEXT.cameras}</div>
          {error ? <div className="live2Error">{error}</div> : null}

          <div className="live2CameraList">
            {cameras.map((camera) => {
              const streams = detectStreams(camera);
              const initialStream = defaultStream(camera);

              return (
                <div
                  key={camera.id}
                  className="live2CameraItem"
                  draggable
                  onDragStart={(event) => {
                    event.dataTransfer.setData("application/x-camera-id", String(camera.id));
                    event.dataTransfer.setData("application/x-camera-stream", initialStream);
                    event.dataTransfer.effectAllowed = "copy";
                  }}
                >
                  <div className="live2CameraName">{camera.name}</div>
                  <div className="live2CameraMeta">{camera.host}:{camera.port}</div>
                  <div className="live2StreamButtons">
                    {streams.map((stream) => (
                      <button
                        key={stream.key}
                        type="button"
                        className="live2StreamButton"
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
          className={`live2Workspace ${dragState || resizeState ? "isEditing" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
          }}
          onDrop={handleDrop}
        >
          {!tiles.length ? (
            <div className="live2Empty">
              <div className="live2EmptyTitle">{TEXT.empty}</div>
            </div>
          ) : null}

          {tiles.map((tile) => {
            const camera = cameraMap.get(String(tile.cameraId));
            const streams = detectStreams(camera);
            const stream = streams.some((item) => item.key === tile.stream)
              ? tile.stream
              : defaultStream(camera);

            return (
              <div
                key={tile.id}
                className="live2Tile"
                style={{
                  left: tile.x,
                  top: tile.y,
                  width: tile.w,
                  height: tile.h,
                  zIndex: tile.z || 2,
                }}
                onPointerDown={() => bringToFront(tile.id)}
              >
                <div className="live2TileBar" onPointerDown={(event) => startMove(event, tile)}>
                  <div className="live2TileTitle">{camera?.name || TEXT.camera}</div>
                  <select
                    className="live2TileSelect"
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
                    className="live2IconButton"
                    title={TEXT.close}
                    onPointerDown={(event) => event.stopPropagation()}
                    onClick={() => removeTile(tile.id)}
                  >
                    ×
                  </button>
                </div>

                <div className="live2TileVideo">
                  {camera ? (
                    <TilePlayer cameraId={camera.id} stream={stream} />
                  ) : (
                    <div className="live2Missing">{TEXT.unavailable}</div>
                  )}
                </div>

                <button
                  type="button"
                  className="live2ResizeHandle"
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
