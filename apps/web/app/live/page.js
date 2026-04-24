"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import TilePlayer from "../../components/TilePlayer";
import { apiFetch } from "../../lib/api";

const GRID_OPTIONS = [
  { value: 1, label: "1 окно" },
  { value: 4, label: "4 окна" },
  { value: 6, label: "6 окон" },
  { value: 9, label: "9 окон" },
];

const LIVE_GRID_KEY = "vms_live_grid_count";
const LIVE_TILES_KEY = "vms_live_tiles_v3";

function getStatusBadge(camera) {
  if (!camera?.enabled) return { text: "Отключена", cls: "warn" };
  if (camera.status === "recording") return { text: "Идёт запись", cls: "ok" };
  if (camera.status === "error") return { text: "Ошибка", cls: "err" };
  if (camera.status === "enabled" || camera.status === "created") {
    return { text: "Включена", cls: "ok" };
  }
  return { text: "Включена", cls: "ok" };
}

function detectAvailableStreams(camera) {
  const result = [];
  if (camera?.rtsp_main_url) result.push({ key: "main", label: "Main" });
  if (camera?.rtsp_sub_url) result.push({ key: "sub", label: "Sub" });
  if (!result.length) result.push({ key: "main", label: "Main" });
  return result;
}

function getDefaultStream(camera) {
  const available = detectAvailableStreams(camera);
  const preferred = (camera?.default_live_stream || "").toLowerCase();

  if (available.some((item) => item.key === "sub")) {
    return "sub";
  }

  if (preferred && available.some((x) => x.key === preferred)) {
    return preferred;
  }

  return available[0]?.key || "main";
}

function buildDefaultTiles(count, cameras) {
  const tiles = [];
  for (let i = 0; i < count; i += 1) {
    const cam = cameras[i] || null;
    tiles.push({
      slot: i + 1,
      cameraId: cam?.id ? String(cam.id) : "",
      stream: cam ? getDefaultStream(cam) : "main",
    });
  }
  return tiles;
}

function readSavedGrid() {
  if (typeof window === "undefined") return 4;
  try {
    const raw = localStorage.getItem(LIVE_GRID_KEY);
    const parsed = Number(raw);
    return [1, 4, 6, 9].includes(parsed) ? parsed : 4;
  } catch {
    return 4;
  }
}

function readSavedTiles() {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(LIVE_TILES_KEY);
    const parsed = JSON.parse(raw || "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveGrid(value) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(LIVE_GRID_KEY, String(value));
  } catch {}
}

function saveTiles(value) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(LIVE_TILES_KEY, JSON.stringify(value));
  } catch {}
}

function reconcileTilesWithCameras(rawTiles, gridCount, cameras) {
  const validIds = new Set(cameras.map((cam) => String(cam.id)));
  const base = Array.isArray(rawTiles) && rawTiles.length
    ? rawTiles.slice(0, gridCount)
    : buildDefaultTiles(gridCount, cameras);

  while (base.length < gridCount) {
    base.push({
      slot: base.length + 1,
      cameraId: "",
      stream: "main",
    });
  }

  return base.map((tile, idx) => {
    const savedCameraId = String(tile?.cameraId || "");
    const savedStream = String(tile?.stream || "").toLowerCase();

    if (!savedCameraId || !validIds.has(savedCameraId)) {
      return {
        slot: idx + 1,
        cameraId: "",
        stream: "main",
      };
    }

    const camera = cameras.find((cam) => String(cam.id) === savedCameraId) || null;
    if (!camera) {
      return {
        slot: idx + 1,
        cameraId: "",
        stream: "main",
      };
    }

    const streams = detectAvailableStreams(camera);
    const finalStream = streams.some((s) => s.key === savedStream)
      ? savedStream
      : getDefaultStream(camera);

    return {
      slot: idx + 1,
      cameraId: savedCameraId,
      stream: finalStream,
    };
  });
}

export default function LivePage() {
  const [cameras, setCameras] = useState([]);
  const [gridCount, setGridCount] = useState(4);
  const [tiles, setTiles] = useState([]);
  const [error, setError] = useState("");

  const hydratedRef = useRef(false);

  async function load() {
    try {
      setError("");
      const cams = await apiFetch("/cameras");
      setCameras(Array.isArray(cams) ? cams : []);
    } catch (err) {
      setError(err.message || "Ошибка загрузки камер");
    }
  }

  useEffect(() => {
    const savedGrid = readSavedGrid();
    const savedTiles = readSavedTiles();

    setGridCount(savedGrid);
    setTiles(savedTiles);
    hydratedRef.current = true;

    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!hydratedRef.current) return;
    saveGrid(gridCount);
  }, [gridCount]);

  useEffect(() => {
    if (!hydratedRef.current) return;
    saveTiles(tiles);
  }, [tiles]);

  useEffect(() => {
    if (!hydratedRef.current) return;
    if (!cameras.length) return;

    setTiles((prev) => {
      const next = reconcileTilesWithCameras(prev, gridCount, cameras);
      saveTiles(next);
      return next;
    });
  }, [cameras, gridCount]);

  const cameraMap = useMemo(() => {
    const map = new Map();
    cameras.forEach((camera) => map.set(String(camera.id), camera));
    return map;
  }, [cameras]);

  function patchTiles(nextTiles) {
    setTiles(nextTiles);
    saveTiles(nextTiles);
  }

  function patchTile(slotIndex, patch) {
    const nextTiles = tiles.map((tile, idx) =>
      idx === slotIndex ? { ...tile, ...patch } : tile
    );
    patchTiles(nextTiles);
  }

  function getTileCamera(tile) {
    if (!tile?.cameraId) return null;
    return cameraMap.get(String(tile.cameraId)) || null;
  }

  return (
    <Layout>
      {error ? (
        <div className="badge err" style={{ marginBottom: 14 }}>
          {error}
        </div>
      ) : null}

      <div className="card liveTopPanelCardCompact" style={{ marginBottom: 14 }}>
        <div className="liveTopRowCompact">
          <select
            className="select liveGridSelectMini"
            value={gridCount}
            onChange={(e) => {
              const nextGrid = Number(e.target.value);
              setGridCount(nextGrid);
              saveGrid(nextGrid);

              const nextTiles = reconcileTilesWithCameras(tiles, nextGrid, cameras);
              patchTiles(nextTiles);
            }}
          >
            {GRID_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div className="liveCameraChipsWrapCompact">
          {cameras.length ? (
            cameras.map((camera) => {
              const badge = getStatusBadge(camera);
              const streams = detectAvailableStreams(camera);

              return (
                <div className="liveCameraChipCompact" key={camera.id}>
                  <div className="liveCameraChipCompactTitle">{camera.name}</div>

                  <div className="liveCameraChipCompactMetaRow">
                    <div className="liveCameraChipCompactAddr">
                      {camera.host}:{camera.port}
                    </div>
                    <span className={`badge ${badge.cls}`}>{badge.text}</span>
                  </div>

                  <div className="liveCameraChipCompactBadges">
                    <span className="badge">{(camera.protocol || "").toUpperCase()}</span>
                    {streams.map((stream) => (
                      <span className="badge" key={stream.key}>
                        {stream.label}
                      </span>
                    ))}
                  </div>
                </div>
              );
            })
          ) : (
            <div className="liveCameraStripEmpty">Камеры не добавлены.</div>
          )}
        </div>
      </div>

      <div className="card liveWorkspaceCardCompact">
        <div className={`liveGrid liveGrid${gridCount}`}>
          {tiles.map((tile, idx) => {
            const camera = getTileCamera(tile);
            const streams = detectAvailableStreams(camera);
            const currentStream = streams.some((s) => s.key === tile.stream)
              ? tile.stream
              : (camera ? getDefaultStream(camera) : "main");

            return (
              <div className="liveTile" key={tile.slot}>
                <div className="liveTileCompactBar">
                  <select
                    className="select liveSelectCompact liveCameraSelect"
                    value={tile.cameraId}
                    onChange={(e) => {
                      const selectedId = e.target.value;
                      const selectedCamera =
                        cameraMap.get(String(selectedId)) || null;

                      patchTile(idx, {
                        cameraId: selectedId,
                        stream: selectedCamera
                          ? getDefaultStream(selectedCamera)
                          : "main",
                      });
                    }}
                  >
                    <option value="">Выбери камеру</option>
                    {cameras.map((cam) => (
                      <option key={cam.id} value={cam.id}>
                        {cam.name}
                      </option>
                    ))}
                  </select>

                  <select
                    className="select liveSelectCompact liveStreamSelect"
                    value={currentStream}
                    onChange={(e) => {
                      patchTile(idx, { stream: e.target.value });
                    }}
                    disabled={!camera}
                  >
                    {streams.map((stream) => (
                      <option key={stream.key} value={stream.key}>
                        {stream.label}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="liveTileBody liveTileVideoBody">
                  {camera ? (
                    <TilePlayer
                      cameraId={camera.id}
                      stream={currentStream}
                    />
                  ) : (
                    <div className="liveTilePlaceholder">
                      <div className="liveTileHint">Выбери камеру</div>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </Layout>
  );
}
