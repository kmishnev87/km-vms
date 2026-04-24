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
      speed: SPEED_OPTIONS.some((item) => item.value === Number(parsed.speed))
        ? Number(parsed.speed)
        : 1,
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
  const validCameraIds = new Set(cameras.map((camera) => String(camera.id)));
  const base = Array.isArray(rawTiles) ? rawTiles.slice(0, gridCount) : [];

  while (base.length < gridCount) {
    base.push({
      slot: base.length + 1,
      cameraId: "",
    });
  }

  return base.map((tile, index) => ({
    slot: index + 1,
    cameraId: validCameraIds.has(String(tile?.cameraId || "")) ? String(tile.cameraId) : "",
  }));
}

function pad(value) {
  return String(value).padStart(2, "0");
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
  const [previewTs, setPreviewTs] = useState(null);
  const [isViewMode, setIsViewMode] = useState(false);
  const [viewControlsVisible, setViewControlsVisible] = useState(true);
  const [focusedTileSlot, setFocusedTileSlot] = useState(null);

  const hydratedRef = useRef(false);
  const initializedCurrentTsRef = useRef(false);
  const currentTsRef = useRef(null);
  const playbackMapRef = useRef({});
  const tilesRef = useRef([]);
  const seekInFlightRef = useRef(false);
  const playbackRequestIdRef = useRef(0);
  const rangesRequestIdRef = useRef(0);
  const isScrubbingRef = useRef(false);
  const seekActionIdRef = useRef(0);
  const activeSeekActionRef = useRef(null);
  const loadedRangesWindowRef = useRef(null);
  const viewOverlayRef = useRef(null);
  const browserFullscreenActiveRef = useRef(false);

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
      result[String(id)] = camera?.name || `Камера ${id}`;
    });
    return result;
  }, [selectedCameraIds, cameraMap]);

  const selectedCameraKey = selectedCameraIds.join(",");

  function normalizeTargetTs() {
    const normalizedDate = date || getNowDefaults().date;
    const normalizedTime = time && time.trim() ? time.trim() : "00:00:00";
    const finalTime = normalizedTime.length === 5 ? `${normalizedTime}:00` : normalizedTime;
    return `${normalizedDate}T${finalTime}`;
  }

  const timelineTs = previewTs || currentTs || new Date(normalizeTargetTs());

  useEffect(() => {
    if (initializedCurrentTsRef.current) return;
    if (!hydratedRef.current) return;

    initializedCurrentTsRef.current = true;
    const initialTs = new Date(normalizeTargetTs());
    setCurrentTs(initialTs);
    setPreviewTs(initialTs);
  }, [date, time]);

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

  function patchTile(slotIndex, patch) {
    setTiles((prev) => prev.map((tile, index) => (
      index === slotIndex ? { ...tile, ...patch } : tile
    )));
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

  async function fetchPlaybackForTile(tile, ts, forceReload = false) {
    const prev = playbackMapRef.current[tile.slot];

    if (!tile.cameraId) {
      return {
        hasVideo: false,
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey: forceReload || !prev
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
    const requestId = ++playbackRequestIdRef.current;
    const results = await Promise.all(
      tilesRef.current.map(async (tile) => [tile.slot, await fetchPlaybackForTile(tile, ts, forceReload)])
    );

    if (requestId !== playbackRequestIdRef.current) {
      return { applied: false };
    }

    const nextMap = {};
    results.forEach(([slot, value]) => {
      nextMap[slot] = value;
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
      const nextDate = new Date(normalizeTargetTs());
      commitCurrentTimestamp(nextDate);
    }
    setIsPlaying(true);
  }

  function handlePause() {
    invalidateSeekActions();
    setIsPlaying(false);
  }

  async function handleStepBack10() {
    const nextDate = currentTsRef.current
      ? new Date(currentTsRef.current.getTime() - 10000)
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
    if (!isPlaying || !currentTs || isScrubbingRef.current || seekInFlightRef.current) return;

    seekInFlightRef.current = true;
    resolvePlaybackForTimestamp(formatLocalNaiveTs(currentTs), false)
      .finally(() => {
        seekInFlightRef.current = false;
      });
  }, [currentTs, isPlaying]);

  async function handleEnterViewMode() {
    setFocusedTileSlot(null);
    setViewControlsVisible(true);
    setIsViewMode(true);
  }

  async function handleExitViewMode() {
    setFocusedTileSlot(null);
    setViewControlsVisible(true);
    setIsViewMode(false);

    if (document.fullscreenElement && viewOverlayRef.current && browserFullscreenActiveRef.current) {
      try {
        await document.exitFullscreen();
      } catch {}
    }
  }

  function toggleViewControls() {
    setViewControlsVisible((prev) => !prev);
  }

  function toggleFocusedTile(slot) {
    setFocusedTileSlot((prev) => (prev === slot ? null : slot));
  }

  useEffect(() => {
    if (!isViewMode) {
      browserFullscreenActiveRef.current = false;
      return;
    }

    setViewControlsVisible(true);

    const node = viewOverlayRef.current;
    if (node?.requestFullscreen) {
      node.requestFullscreen()
        .then(() => {
          browserFullscreenActiveRef.current = true;
        })
        .catch(() => {
          browserFullscreenActiveRef.current = false;
        });
    }
  }, [isViewMode]);

  useEffect(() => {
    function handleFullscreenChange() {
      const hasFullscreen = Boolean(document.fullscreenElement);
      if (!hasFullscreen && browserFullscreenActiveRef.current) {
        browserFullscreenActiveRef.current = false;
        setFocusedTileSlot(null);
        setIsViewMode(false);
        setViewControlsVisible(true);
      }
    }

    function handleKeyDown(event) {
      if (event.key !== "Escape") return;

      if (focusedTileSlot !== null) {
        setFocusedTileSlot(null);
        return;
      }

      if (isViewMode && !document.fullscreenElement) {
        setIsViewMode(false);
        setViewControlsVisible(true);
      }
    }

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("fullscreenchange", handleFullscreenChange);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [focusedTileSlot, isViewMode]);

  function renderControlBar({ compact = false, inViewMode = false } = {}) {
    return (
      <div className={`card chronologyTopCard ${compact ? "chronologyTopCardCompact" : ""}`}>
        <div className="chronologyControlsRow chronologyControlsRowSpread">
          <div className="chronologyControlsLeft">
            <input
              type="date"
              className={`input chronologyDateInput ${compact ? "chronologyInputCompact" : ""}`}
              value={date}
              onChange={(event) => setDate(event.target.value)}
            />

            <input
              type="time"
              step="1"
              className={`input chronologyTimeInput ${compact ? "chronologyInputCompact" : ""}`}
              value={time}
              onChange={(event) => setTime(event.target.value)}
              placeholder="00:00:00"
            />

            <button
              className={`button chronologyApplyButton ${compact ? "chronologyApplyButtonCompact" : ""}`}
              onClick={handleFind}
            >
              Найти
            </button>

            {!inViewMode ? (
              <select
                className={`select chronologyGridSelect ${compact ? "chronologyGridSelectCompact" : ""}`}
                value={gridCount}
                onChange={(event) => setGridCount(Number(event.target.value))}
              >
                {GRID_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : null}

            <button
              className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`}
              onClick={handlePlay}
              title="Play"
            >
              ▶
            </button>

            <button
              className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`}
              onClick={handlePause}
              title="Pause"
            >
              ❚❚
            </button>

            <button
              className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`}
              onClick={handleStepBack10}
              title="-10"
            >
              -10
            </button>

            <div className="chronologySpeedGroup">
              {SPEED_OPTIONS.map((item) => (
                <button
                  key={item.value}
                  className={`chronologySpeedButton ${speed === item.value ? "active" : ""} ${compact ? "chronologySpeedButtonCompact" : ""}`}
                  onClick={() => setSpeed(item.value)}
                >
                  {item.label}
                </button>
              ))}
            </div>

            {!inViewMode ? (
              <button
                className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`}
                onClick={handleEnterViewMode}
                title="Просмотр"
              >
                ⛶
              </button>
            ) : null}
          </div>

          <div className={`chronologyCurrentTimeBox ${compact ? "chronologyCurrentTimeBoxCompact" : ""}`}>
            {timelineTs ? formatPlaybackDateTime(timelineTs) : "—"}
          </div>

          {inViewMode ? (
            <div className="chronologyViewToolbarActions">
              <button
                className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`}
                onClick={toggleViewControls}
                title={viewControlsVisible ? "Скрыть панель" : "Показать панель"}
              >
                {viewControlsVisible ? "▴" : "▾"}
              </button>

              <button
                className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`}
                onClick={handleExitViewMode}
                title="Выйти"
              >
                ✕
              </button>
            </div>
          ) : null}
        </div>
      </div>
    );
  }

  function renderWorkspace({ fullscreen = false } = {}) {
    const visibleTiles = fullscreen && focusedTileSlot !== null
      ? tiles.filter((tile) => tile.slot === focusedTileSlot)
      : tiles;

    return (
      <div className={`card chronologyWorkspaceCard ${fullscreen ? "chronologyWorkspaceCardFullscreen" : ""}`}>
        <div
          className={[
            "chronologyGrid",
            `chronologyGrid${gridCount}`,
            fullscreen ? "chronologyGridFullscreen" : "",
            fullscreen && gridCount === 2 ? "chronologyGridFullscreen2" : "",
            fullscreen && focusedTileSlot !== null ? "chronologyGridFocused" : "",
          ].filter(Boolean).join(" ")}
        >
          {visibleTiles.map((tile) => {
            const camera = tile.cameraId ? cameraMap.get(String(tile.cameraId)) || null : null;
            const playback = playbackMap[tile.slot] || {
              hasVideo: false,
              cameraId: null,
              relPath: null,
              offsetSec: 0,
              playbackKey: `slot-${tile.slot}-initial`,
            };

            return (
              <div
                className={[
                  "chronologyTile",
                  fullscreen ? "chronologyTileFullscreen chronologyTileInteractive" : "",
                  fullscreen && focusedTileSlot !== null ? "chronologyTileFocused" : "",
                ].filter(Boolean).join(" ")}
                key={tile.slot}
                onDoubleClickCapture={fullscreen ? (event) => {
                  event.preventDefault();
                  event.stopPropagation();
                  toggleFocusedTile(tile.slot);
                } : undefined}
              >
                <div className={`chronologyTileBar ${fullscreen ? "chronologyTileBarCompact" : ""}`}>
                  <select
                    className={`select chronologyCameraSelect ${fullscreen ? "chronologyCameraSelectCompact" : ""}`}
                    value={tile.cameraId}
                    onChange={(event) => {
                      patchTile(tile.slot - 1, { cameraId: event.target.value });
                    }}
                  >
                    <option value="">Выбери камеру</option>
                    {cameras.map((camera) => (
                      <option key={camera.id} value={camera.id}>
                        {camera.name}
                      </option>
                    ))}
                  </select>
                </div>

                <div className={`chronologyTileBody ${fullscreen ? "chronologyTileBodyFullscreen" : ""}`}>
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
    );
  }

  return (
    <Layout>
      {error ? (
        <div className="badge err" style={{ marginBottom: 12 }}>
          {error}
        </div>
      ) : null}

      <div style={{ marginBottom: 14 }}>
        {renderControlBar()}
      </div>

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
        currentTimeLabel={timelineTs ? formatPlaybackDateTime(timelineTs) : "—"}
      />

      {renderWorkspace()}

      {isViewMode ? (
        <div ref={viewOverlayRef} className="chronologyViewOverlay">
          {viewControlsVisible ? (
            <div className="chronologyViewChrome">
              <div className="chronologyViewControlStack">
                {renderControlBar({ compact: true, inViewMode: true })}

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
                  currentTimeLabel={timelineTs ? formatPlaybackDateTime(timelineTs) : "—"}
                  compact
                />
              </div>
            </div>
          ) : (
            <button
              className="chronologyViewHandle"
              onClick={toggleViewControls}
              title="Показать панель"
            >
              Панель
            </button>
          )}

          <div className="chronologyViewWorkspace">
            {renderWorkspace({ fullscreen: true })}
          </div>
        </div>
      ) : null}
    </Layout>
  );
}
