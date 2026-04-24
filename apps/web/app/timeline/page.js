"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import ArchiveTilePlayer from "../../components/ArchiveTilePlayer";
import ChronologyTimeline from "../../components/ChronologyTimeline";
import { apiFetch } from "../../lib/api";

const GRID_OPTIONS = [
  { value: 2, label: "2 окна" },
  { value: 4, label: "4 окна" },
];

const SPEED_OPTIONS = [
  { value: 0.25, label: "0,25x" },
  { value: 0.5, label: "0,5x" },
  { value: 0.75, label: "0,75x" },
  { value: 1, label: "1,0x" },
  { value: 1.5, label: "1,5x" },
  { value: 2, label: "2,0x" },
  { value: 4, label: "4,0x" },
];

const ZOOM_KEYS = ["24h", "3d", "7d"];
const ZOOM_HOURS = {
  "24h": 24,
  "3d": 72,
  "7d": 168,
};

const CHRONOLOGY_PREFS_KEY = "vms_chronology_prefs_v3";

function getNowDefaults() {
  const now = new Date();
  return {
    date: now.toISOString().slice(0, 10),
    time: "",
  };
}

function readPrefs() {
  const defaults = getNowDefaults();

  if (typeof window === "undefined") {
    return {
      gridCount: 2,
      speed: 1,
      date: defaults.date,
      time: defaults.time,
      zoomKey: "24h",
      tiles: [],
    };
  }

  try {
    const raw = localStorage.getItem(CHRONOLOGY_PREFS_KEY);
    if (!raw) {
      return {
        gridCount: 2,
        speed: 1,
        date: defaults.date,
        time: defaults.time,
        zoomKey: "24h",
        tiles: [],
      };
    }

    const parsed = JSON.parse(raw);
    return {
      gridCount: [2, 4].includes(Number(parsed.gridCount)) ? Number(parsed.gridCount) : 2,
      speed: SPEED_OPTIONS.some((x) => x.value === Number(parsed.speed)) ? Number(parsed.speed) : 1,
      date: parsed.date || defaults.date,
      time: typeof parsed.time === "string" ? parsed.time : defaults.time,
      zoomKey: ZOOM_KEYS.includes(parsed.zoomKey) ? parsed.zoomKey : "24h",
      tiles: Array.isArray(parsed.tiles) ? parsed.tiles : [],
    };
  } catch {
    return {
      gridCount: 2,
      speed: 1,
      date: defaults.date,
      time: defaults.time,
      zoomKey: "24h",
      tiles: [],
    };
  }
}

function savePrefs(prefs) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(CHRONOLOGY_PREFS_KEY, JSON.stringify(prefs));
  } catch {}
}

function reconcileTiles(rawTiles, gridCount, cameras) {
  const validCameraIds = new Set(cameras.map((cam) => String(cam.id)));
  const base = Array.isArray(rawTiles) ? rawTiles.slice(0, gridCount) : [];

  while (base.length < gridCount) {
    base.push({
      slot: base.length + 1,
      cameraId: "",
    });
  }

  return base.map((tile, idx) => {
    const cameraId = validCameraIds.has(String(tile?.cameraId || ""))
      ? String(tile.cameraId)
      : "";

    return {
      slot: idx + 1,
      cameraId,
    };
  });
}

function pad(v) {
  return String(v).padStart(2, "0");
}

function formatPlaybackDateTime(dt) {
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

export default function TimelinePage() {
  const initial = readPrefs();

  const [cameras, setCameras] = useState([]);
  const [gridCount, setGridCount] = useState(initial.gridCount);
  const [speed, setSpeed] = useState(initial.speed);
  const [date, setDate] = useState(initial.date);
  const [time, setTime] = useState(initial.time);
  const [zoomKey, setZoomKey] = useState(initial.zoomKey);
  const [tiles, setTiles] = useState(initial.tiles || []);
  const [playbackMap, setPlaybackMap] = useState({});
  const [rangesData, setRangesData] = useState({});
  const [error, setError] = useState("");
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTs, setCurrentTs] = useState(null);

  const hydratedRef = useRef(false);
  const currentTsRef = useRef(null);
  const playbackMapRef = useRef({});
  const tilesRef = useRef([]);
  const seekInFlightRef = useRef(false);

  async function loadCameras() {
    try {
      setError("");
      const cams = await apiFetch("/cameras");
      setCameras(Array.isArray(cams) ? cams : []);
    } catch (err) {
      setError(err.message || "Ошибка загрузки камер");
    }
  }

  useEffect(() => {
    hydratedRef.current = true;
    loadCameras();
  }, []);

  useEffect(() => {
    if (!cameras.length) return;
    setTiles((prev) => reconcileTiles(prev, gridCount, cameras));
  }, [cameras, gridCount]);

  useEffect(() => {
    if (!hydratedRef.current) return;
    savePrefs({
      gridCount,
      speed,
      date,
      time,
      zoomKey,
      tiles,
    });
  }, [gridCount, speed, date, time, zoomKey, tiles]);

  useEffect(() => {
    currentTsRef.current = currentTs;
  }, [currentTs]);

  useEffect(() => {
    playbackMapRef.current = playbackMap;
  }, [playbackMap]);

  useEffect(() => {
    tilesRef.current = tiles;
  }, [tiles]);

  useEffect(() => {
    if (!isPlaying || !currentTsRef.current) return;

    const timer = setInterval(() => {
      setCurrentTs((prev) => {
        if (!prev) return prev;
        return new Date(prev.getTime() + 1000 * Number(speed || 1));
      });
    }, 1000);

    return () => clearInterval(timer);
  }, [isPlaying, speed]);

  const cameraMap = useMemo(() => {
    const map = new Map();
    cameras.forEach((camera) => map.set(String(camera.id), camera));
    return map;
  }, [cameras]);

  const selectedCameraIds = useMemo(() => {
    return tiles
      .map((tile) => String(tile.cameraId || ""))
      .filter(Boolean)
      .filter((value, index, arr) => arr.indexOf(value) === index);
  }, [tiles]);

  const selectedCameraNames = useMemo(() => {
    const result = {};
    selectedCameraIds.forEach((id) => {
      const camera = cameraMap.get(String(id));
      result[String(id)] = camera?.name || `Камера ${id}`;
    });
    return result;
  }, [selectedCameraIds, cameraMap]);

  useEffect(() => {
    async function loadRanges() {
      if (!currentTs || !selectedCameraIds.length) {
        setRangesData({});
        return;
      }

      const hours = ZOOM_HOURS[zoomKey] || 24;
      const centerMs = currentTs.getTime();
      const halfMs = (hours * 3600 * 1000) / 2;
      const from = new Date(centerMs - halfMs);
      const to = new Date(centerMs + halfMs);

      try {
        const response = await apiFetch(
          `/chronology/ranges?camera_ids=${selectedCameraIds.join(",")}&from=${encodeURIComponent(formatLocalNaiveTs(from))}&to=${encodeURIComponent(formatLocalNaiveTs(to))}`
        );
        setRangesData(response?.items || {});
      } catch (_) {
        setRangesData({});
      }
    }

    loadRanges();
  }, [currentTs, zoomKey, selectedCameraIds.join(",")]);

  function patchTile(slotIndex, patch) {
    setTiles((prev) =>
      prev.map((tile, idx) => (idx === slotIndex ? { ...tile, ...patch } : tile))
    );
  }

  function normalizeTargetTs() {
    const normalizedDate = date || getNowDefaults().date;
    const normalizedTime = time && time.trim() ? time.trim() : "00:00:00";
    const finalTime = normalizedTime.length === 5 ? `${normalizedTime}:00` : normalizedTime;
    return `${normalizedDate}T${finalTime}`;
  }

  async function fetchPlaybackForTile(tile, ts, forceReload = false) {
    const prev = playbackMapRef.current[tile.slot];

    if (!tile.cameraId) {
      return {
        hasVideo: false,
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey:
          forceReload || !prev
            ? `slot-${tile.slot}-empty-${Date.now()}`
            : prev.playbackKey,
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
          : `slot-${tile.slot}-${cameraId || "empty"}-${relPath || "empty"}-${offsetSec}-${Date.now()}`,
      };
    } catch (_) {
      return {
        hasVideo: false,
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey: `slot-${tile.slot}-error-${Date.now()}`,
      };
    }
  }

  async function resolvePlaybackForTimestamp(ts, forceReload = false) {
    const results = await Promise.all(
      tilesRef.current.map(async (tile) => {
        const value = await fetchPlaybackForTile(tile, ts, forceReload);
        return [tile.slot, value];
      })
    );

    const nextMap = {};
    results.forEach(([slot, value]) => {
      nextMap[slot] = value;
    });

    setPlaybackMap(nextMap);
  }

  async function handleFind() {
    const ts = normalizeTargetTs();
    const targetDate = new Date(ts);

    setIsPlaying(false);
    setCurrentTs(targetDate);

    await resolvePlaybackForTimestamp(ts, true);
  }

  function handlePlay() {
    if (!currentTsRef.current) {
      const ts = normalizeTargetTs();
      setCurrentTs(new Date(ts));
    }
    setIsPlaying(true);
  }

  function handlePause() {
    setIsPlaying(false);
  }

  async function handleStepBack10() {
    let nextDate = currentTsRef.current;
    if (!nextDate) {
      nextDate = new Date(normalizeTargetTs());
    } else {
      nextDate = new Date(nextDate.getTime() - 10000);
    }

    setIsPlaying(false);
    setCurrentTs(nextDate);

    const { date: nextDateStr, time: nextTimeStr } = dateTimeFromDate(nextDate);
    setDate(nextDateStr);
    setTime(nextTimeStr);

    await resolvePlaybackForTimestamp(formatLocalNaiveTs(nextDate), true);
  }

  async function handleTimelineSelect(nextDate) {
    setIsPlaying(false);
    setCurrentTs(nextDate);

    const { date: nextDateStr, time: nextTimeStr } = dateTimeFromDate(nextDate);
    setDate(nextDateStr);
    setTime(nextTimeStr);

    await resolvePlaybackForTimestamp(formatLocalNaiveTs(nextDate), true);
  }

  function handleZoomOut() {
    const currentIndex = ZOOM_KEYS.indexOf(zoomKey);
    setZoomKey(ZOOM_KEYS[Math.min(currentIndex + 1, ZOOM_KEYS.length - 1)]);
  }

  function handleZoomIn() {
    const currentIndex = ZOOM_KEYS.indexOf(zoomKey);
    setZoomKey(ZOOM_KEYS[Math.max(currentIndex - 1, 0)]);
  }

  useEffect(() => {
    if (!isPlaying || !currentTs) return;
    if (seekInFlightRef.current) return;

    seekInFlightRef.current = true;
    const ts = formatLocalNaiveTs(currentTs);

    resolvePlaybackForTimestamp(ts, false)
      .finally(() => {
        seekInFlightRef.current = false;
      });
  }, [currentTs, isPlaying]);

  return (
    <Layout>
      {error ? (
        <div className="badge err" style={{ marginBottom: 12 }}>
          {error}
        </div>
      ) : null}

      <div className="card chronologyTopCard" style={{ marginBottom: 14 }}>
        <div className="chronologyControlsRow chronologyControlsRowSpread">
          <div className="chronologyControlsLeft">
            <input
              type="date"
              className="input chronologyDateInput"
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />

            <input
              type="time"
              step="1"
              className="input chronologyTimeInput"
              value={time}
              onChange={(e) => setTime(e.target.value)}
              placeholder="00:00:00"
            />

            <button className="button chronologyApplyButton" onClick={handleFind}>
              Найти
            </button>

            <select
              className="select chronologyGridSelect"
              value={gridCount}
              onChange={(e) => setGridCount(Number(e.target.value))}
            >
              {GRID_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>

            <button className="chronologyIconButton" onClick={handlePlay} title="Play">
              ▶
            </button>

            <button className="chronologyIconButton" onClick={handlePause} title="Pause">
              ❚❚
            </button>

            <button className="chronologyIconButton" onClick={handleStepBack10} title="-10">
              -10
            </button>

            <div className="chronologySpeedGroup">
              {SPEED_OPTIONS.map((item) => (
                <button
                  key={item.value}
                  className={`chronologySpeedButton ${speed === item.value ? "active" : ""}`}
                  onClick={() => setSpeed(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>
          </div>

          <div className="chronologyCurrentTimeBox">
            {currentTs ? formatPlaybackDateTime(currentTs) : "—"}
          </div>
        </div>
      </div>

      <ChronologyTimeline
        currentTs={currentTs || new Date(normalizeTargetTs())}
        zoomKey={zoomKey}
        onZoomOut={handleZoomOut}
        onZoomIn={handleZoomIn}
        onSelectTime={handleTimelineSelect}
        rangesByCamera={rangesData}
        selectedCameraIds={selectedCameraIds}
        cameraNames={selectedCameraNames}
      />

      <div className="card chronologyWorkspaceCard">
        <div className={`chronologyGrid chronologyGrid${gridCount}`}>
          {tiles.map((tile, idx) => {
            const camera = tile.cameraId ? cameraMap.get(String(tile.cameraId)) || null : null;
            const playback = playbackMap[tile.slot] || {
              hasVideo: false,
              cameraId: null,
              relPath: null,
              offsetSec: 0,
              playbackKey: `slot-${tile.slot}-initial`,
            };

            return (
              <div className="chronologyTile" key={tile.slot}>
                <div className="chronologyTileBar">
                  <select
                    className="select chronologyCameraSelect"
                    value={tile.cameraId}
                    onChange={(e) => {
                      patchTile(idx, {
                        cameraId: e.target.value,
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
                </div>

                <div className="chronologyTileBody">
                  {camera ? (
                    <ArchiveTilePlayer
                      playback={playback}
                      speed={speed}
                      isPlaying={isPlaying}
                    />
                  ) : (
                    <div className="chronologyPlaceholder">
                      Выбери камеру
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
