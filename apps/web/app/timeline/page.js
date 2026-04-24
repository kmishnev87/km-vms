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
const VIEW_CONTROLS_IDLE_MS = 4000;

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
  const [previewTs, setPreviewTs] = useState(null);
  const [isViewMode, setIsViewMode] = useState(false);
  const [viewControlsVisible, setViewControlsVisible] = useState(true);

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
  const viewHideTimerRef = useRef(null);
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

      if (!needsReload) {
        return;
      }

      const from = new Date(fromMs);
      const to = new Date(toMs);

      try {
        const response = await apiFetch(
          `/chronology/ranges?camera_ids=${selectedCameraKey}&from=${encodeURIComponent(formatLocalNaiveTs(from))}&to=${encodeURIComponent(formatLocalNaiveTs(to))}`
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
    setTiles((prev) =>
      prev.map((tile, idx) => (idx === slotIndex ? { ...tile, ...patch } : tile))
    );
  }

  function syncFormDateTime(nextDate) {
    const { date: nextDateStr, time: nextTimeStr } = dateTimeFromDate(nextDate);
    setDate(nextDateStr);
    setTime(nextTimeStr);
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
    const requestId = ++playbackRequestIdRef.current;
    const results = await Promise.all(
      tilesRef.current.map(async (tile) => {
        const value = await fetchPlaybackForTile(tile, ts, forceReload);
        return [tile.slot, value];
      })
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
    const ts = normalizeTargetTs();
    const targetDate = new Date(ts);

    setIsPlaying(false);
    invalidateLoadedRangesWindow();
    commitCurrentTimestamp(targetDate);
    await resolvePlaybackForTimestamp(ts, true);
  }

  function handlePlay() {
    invalidateSeekActions();
    if (!currentTsRef.current) {
      const ts = normalizeTargetTs();
      const nextDate = new Date(ts);
      commitCurrentTimestamp(nextDate);
    }
    setIsPlaying(true);
  }

  function handlePause() {
    invalidateSeekActions();
    setIsPlaying(false);
  }

  async function handleStepBack10() {
    let nextDate = currentTsRef.current;
    if (!nextDate) {
      nextDate = new Date(normalizeTargetTs());
    } else {
      nextDate = new Date(nextDate.getTime() - 10000);
    }

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
    if (
      action &&
      result.applied &&
      action.shouldResume &&
      seekActionIdRef.current === action.id
    ) {
      setIsPlaying(true);
    }
  }

  useEffect(() => {
    if (!isPlaying || !currentTs) return;
    if (isScrubbingRef.current) return;
    if (seekInFlightRef.current) return;

    seekInFlightRef.current = true;
    const ts = formatLocalNaiveTs(currentTs);

    resolvePlaybackForTimestamp(ts, false)
      .finally(() => {
        seekInFlightRef.current = false;
      });
  }, [currentTs, isPlaying]);

  function clearViewHideTimer() {
    if (viewHideTimerRef.current) {
      clearTimeout(viewHideTimerRef.current);
      viewHideTimerRef.current = null;
    }
  }

  function scheduleViewControlsHide() {
    clearViewHideTimer();
    viewHideTimerRef.current = setTimeout(() => {
      setViewControlsVisible(false);
    }, VIEW_CONTROLS_IDLE_MS);
  }

  function revealViewControls() {
    setViewControlsVisible(true);
    if (isViewMode) {
      scheduleViewControlsHide();
    }
  }

  async function handleEnterViewMode() {
    setIsViewMode(true);
    setViewControlsVisible(true);
  }

  async function handleExitViewMode() {
    clearViewHideTimer();
    setViewControlsVisible(true);
    setIsViewMode(false);

    if (document.fullscreenElement && viewOverlayRef.current && browserFullscreenActiveRef.current) {
      try {
        await document.exitFullscreen();
      } catch {}
    }
  }

  useEffect(() => {
    if (!isViewMode) {
      clearViewHideTimer();
      browserFullscreenActiveRef.current = false;
      return;
    }

    setViewControlsVisible(true);
    scheduleViewControlsHide();

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

    return () => {
      clearViewHideTimer();
    };
  }, [isViewMode]);

  useEffect(() => {
    function handleFullscreenChange() {
      const hasFullscreen = Boolean(document.fullscreenElement);
      if (!hasFullscreen && browserFullscreenActiveRef.current) {
        browserFullscreenActiveRef.current = false;
        setIsViewMode(false);
        setViewControlsVisible(true);
        clearViewHideTimer();
      }
    }

    function handleKeyDown(event) {
      if (event.key === "Escape" && isViewMode && !document.fullscreenElement) {
        setIsViewMode(false);
        setViewControlsVisible(true);
        clearViewHideTimer();
      }
    }

    document.addEventListener("fullscreenchange", handleFullscreenChange);
    window.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("fullscreenchange", handleFullscreenChange);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [isViewMode]);

  function renderControlBar({ compact = false, inViewMode = false } = {}) {
    return (
      <div className={`card chronologyTopCard ${compact ? "chronologyTopCardCompact" : ""}`} style={{ marginBottom: inViewMode ? 10 : 14 }}>
        <div className="chronologyControlsRow chronologyControlsRowSpread">
          <div className="chronologyControlsLeft">
            <input
              type="date"
              className={`input chronologyDateInput ${compact ? "chronologyInputCompact" : ""}`}
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />

            <input
              type="time"
              step="1"
              className={`input chronologyTimeInput ${compact ? "chronologyInputCompact" : ""}`}
              value={time}
              onChange={(e) => setTime(e.target.value)}
              placeholder="00:00:00"
            />

            <button className={`button chronologyApplyButton ${compact ? "chronologyApplyButtonCompact" : ""}`} onClick={handleFind}>
              Найти
            </button>

            {!inViewMode ? (
              <select
                className={`select chronologyGridSelect ${compact ? "chronologyGridSelectCompact" : ""}`}
                value={gridCount}
                onChange={(e) => setGridCount(Number(e.target.value))}
              >
                {GRID_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : null}

            <button className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`} onClick={handlePlay} title="Play">
              ▶
            </button>

            <button className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`} onClick={handlePause} title="Pause">
              ❚❚
            </button>

            <button className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`} onClick={handleStepBack10} title="-10">
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
              <button className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`} onClick={handleEnterViewMode} title="Просмотр">
                ⛶
              </button>
            ) : null}
          </div>

          <div className={`chronologyCurrentTimeBox ${compact ? "chronologyCurrentTimeBoxCompact" : ""}`}>
            {timelineTs ? formatPlaybackDateTime(timelineTs) : "—"}
          </div>

          {inViewMode ? (
            <button className={`chronologyIconButton ${compact ? "chronologyIconButtonCompact" : ""}`} onClick={handleExitViewMode} title="Выйти">
              ✕
            </button>
          ) : null}
        </div>
      </div>
    );
  }

  function renderWorkspace({ compact = false, fullscreen = false } = {}) {
    return (
      <div className={`card chronologyWorkspaceCard ${fullscreen ? "chronologyWorkspaceCardFullscreen" : ""}`}>
        <div className={`chronologyGrid chronologyGrid${gridCount} ${fullscreen ? "chronologyGridFullscreen" : ""} ${fullscreen && gridCount === 2 ? "chronologyGridFullscreen2" : ""}`}>
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
              <div className={`chronologyTile ${fullscreen ? "chronologyTileFullscreen" : ""}`} key={tile.slot}>
                <div className={`chronologyTileBar ${fullscreen ? "chronologyTileBarCompact" : ""}`}>
                  <select
                    className={`select chronologyCameraSelect ${fullscreen ? "chronologyCameraSelectCompact" : ""}`}
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

      {renderControlBar()}

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
        <div
          ref={viewOverlayRef}
          className="chronologyViewOverlay"
          onMouseMove={revealViewControls}
        >
          <div className={`chronologyViewChrome ${viewControlsVisible ? "visible" : "hidden"}`}>
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

          <div className="chronologyViewWorkspace">
            {renderWorkspace({ fullscreen: true })}
          </div>
        </div>
      ) : null}
    </Layout>
  );
}
