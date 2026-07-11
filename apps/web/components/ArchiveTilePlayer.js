"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import CompactVideoCanvas from "./CompactVideoCanvas";
import VideoZoomPanSurface from "./VideoZoomPanSurface";
import { issueChronologyMediaToken } from "../lib/api";
import {
  normalizeVideoDimensions,
  selectCompactVideoRenderMode,
} from "../lib/playbackResolution";

const MEDIA_REFRESH_RETRY_LIMIT = 1;

export default function ArchiveTilePlayer({
  playback,
  speed,
  isPlaying,
  allowFullscreen = true,
  coordination = null,
  tileId = "",
  onTilePlaybackState,
}) {
  const unavailableText = playback?.availabilityStatus === "root_unavailable"
    ? "Том архива сейчас недоступен"
    : playback?.availabilityStatus === "root_unresolved"
      ? "Не удалось определить расположение записи"
      : "Запись недоступна";
  const wrapRef = useRef(null);
  const videoRef = useRef(null);
  const playIntentRef = useRef(false);
  const playbackRateRef = useRef(1);
  const coordinationRef = useRef(coordination);
  const currentSourceRef = useRef({ playbackKey: "", relPath: "", cameraId: "" });
  const prepareSequenceRef = useRef(0);
  const unsupportedDownloadBusyRef = useRef(false);
  const dimensionProbeTimerRef = useRef(null);
  const [status, setStatus] = useState("idle");
  const [naturalResolution, setNaturalResolution] = useState({ width: 0, height: 0, source: "missing" });
  const [viewerRect, setViewerRect] = useState({ width: 0, height: 0 });
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [readyState, setReadyState] = useState(0);
  const [canvasFrame, setCanvasFrame] = useState({
    ready: false,
    generation: "",
    reason: "inactive",
    error: "",
  });

  const coordinatorHolding = coordination?.releaseState === "holding";
  playIntentRef.current = Boolean(isPlaying && !coordinatorHolding);
  playbackRateRef.current = Number(speed || 1);
  coordinationRef.current = coordination;

  const renderState = selectCompactVideoRenderMode({
    dimensions: naturalResolution,
    rect: viewerRect,
    isFullscreen,
    sourceHighResolution: true,
  });
  const compactCanvasRequested = renderState.renderer === "canvas" && readyState >= 2;
  const canvasGeneration = [
    playback?.playbackKey || "empty",
    naturalResolution.width,
    naturalResolution.height,
    viewerRect.width,
    viewerRect.height,
    isFullscreen ? "fullscreen" : "inline",
  ].join(":");
  const nativeVideoSuppressed =
    compactCanvasRequested &&
    canvasFrame.ready &&
    canvasFrame.generation === canvasGeneration &&
    !canvasFrame.error;

  const handleCanvasFrameState = useCallback((next) => {
    setCanvasFrame({
      ready: Boolean(next?.ready),
      generation: String(next?.generation || ""),
      reason: next?.reason || "",
      error: next?.error || "",
    });
  }, []);

  const reportTileState = useCallback(
    (state, facts = {}) => {
      const operationId = coordinationRef.current?.operationId || "";
      if (!operationId || !tileId || typeof onTilePlaybackState !== "function") return;
      onTilePlaybackState(tileId, operationId, state, {
        playbackKey: playback?.playbackKey || "",
        relPath: playback?.relPath || "",
        offsetSec: Number(playback?.offsetSec || 0),
        readyState: videoRef.current?.readyState || 0,
        currentTime: Number(videoRef.current?.currentTime || 0),
        ...facts,
      });
    },
    [onTilePlaybackState, playback?.offsetSec, playback?.playbackKey, playback?.relPath, tileId]
  );

  function applyNaturalResolution(width, height, source) {
    const normalized = normalizeVideoDimensions(width, height);
    setNaturalResolution((prev) => {
      if (
        prev.width === normalized.width &&
        prev.height === normalized.height &&
        prev.source === source
      ) {
        return prev;
      }
      return { ...normalized, source };
    });
  }

  function clearDimensionProbe() {
    if (dimensionProbeTimerRef.current) {
      clearTimeout(dimensionProbeTimerRef.current);
      dimensionProbeTimerRef.current = null;
    }
  }

  function sampleVideoDimensions(source = "video") {
    const video = videoRef.current;
    if (!video) return;
    setReadyState(Number(video.readyState || 0));
    if (video.videoWidth && video.videoHeight) {
      applyNaturalResolution(video.videoWidth, video.videoHeight, source);
    }
  }

  function startDimensionProbe(source = "probe") {
    clearDimensionProbe();
    let attempts = 0;
    const tick = () => {
      attempts += 1;
      sampleVideoDimensions(source);
      const video = videoRef.current;
      if (video?.videoWidth && video?.videoHeight) return;
      if (attempts < 20) {
        dimensionProbeTimerRef.current = setTimeout(tick, 250);
      }
    };
    tick();
  }

  async function buildMediaUrl() {
    if (!playback?.cameraId || !playback?.relPath) return "";
    const mediaToken = await issueChronologyMediaToken(playback.cameraId, playback.relPath, playback);
    const params = new URLSearchParams();
    if (playback?.segmentId) params.set("segment_id", String(playback.segmentId));
    if (playback?.archiveRootId) params.set("archive_root_id", playback.archiveRootId);
    if (playback?.playbackRef) params.set("playback_ref", playback.playbackRef);
    if (!params.has("segment_id") && !params.has("playback_ref")) {
      params.set("camera_id", String(playback.cameraId));
      params.set("rel_path", playback.relPath);
    }
    params.set("media_token", mediaToken);
    return (
      `/api/chronology/file?${params.toString()}`
    );
  }

  function toggleFullscreen() {
    if (!allowFullscreen) return;

    const el = wrapRef.current;
    if (!el) return;

    if (!document.fullscreenElement) {
      el.requestFullscreen?.().catch(() => {});
    } else {
      document.exitFullscreen?.().catch(() => {});
    }
  }

  async function handleUnsupportedDownload() {
    if (unsupportedDownloadBusyRef.current) return;

    unsupportedDownloadBusyRef.current = true;
    try {
      const url = await buildMediaUrl();
      const a = document.createElement("a");
      a.href = url;
      a.download = playback?.filename || playback?.relPath?.split(/[\\/]/).pop() || "archive-video";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (_) {
      setStatus("error");
    } finally {
      unsupportedDownloadBusyRef.current = false;
    }
  }

  function safeTargetTimeForVideo(video, value) {
    const next = Math.max(0, Number(value || 0));
    const duration = Number(video?.duration || 0);
    if (Number.isFinite(duration) && duration > 0) {
      return Math.min(next, Math.max(0, duration - 0.25));
    }
    return next;
  }

  function prepareVideoAtTarget({
    targetTime,
    playIntent = playIntentRef.current,
    reason = "prepare",
    timeoutMs = 6000,
  } = {}) {
    const video = videoRef.current;
    if (!video) return;
    const sequence = ++prepareSequenceRef.current;
    const operationId = coordinationRef.current?.operationId || "";
    const target = safeTargetTimeForVideo(video, targetTime);
    let done = false;
    let timer = null;

    const cleanup = () => {
      video.removeEventListener("seeked", complete);
      video.removeEventListener("loadeddata", complete);
      video.removeEventListener("canplay", complete);
      video.removeEventListener("error", fail);
      if (timer) clearTimeout(timer);
    };

    const currentOperationMatches = () =>
      sequence === prepareSequenceRef.current &&
      (!operationId || coordinationRef.current?.operationId === operationId);

    const readyEnough = () => {
      const ready = Number(video.readyState || 0) >= 2;
      const current = Number(video.currentTime || 0);
      return ready && Math.abs(current - target) <= 1;
    };

    function complete() {
      if (done || !currentOperationMatches() || !readyEnough()) return;
      done = true;
      cleanup();
      sampleVideoDimensions(reason);
      try {
        video.playbackRate = Number(playbackRateRef.current || 1);
      } catch (_) {}
      if (playIntent && coordinationRef.current?.releaseState !== "holding") {
        video.play().catch(() => {});
        setStatus("playing");
      } else {
        try {
          video.pause();
        } catch (_) {}
        setStatus("ready");
      }
      reportTileState("ready", { reason, targetTime: target });
    }

    function fail() {
      if (done || !currentOperationMatches()) return;
      done = true;
      cleanup();
      setStatus("error");
      reportTileState("error", { reason, targetTime: target });
    }

    try {
      video.pause();
    } catch (_) {}

    setStatus("loading");
    reportTileState("loading", { reason, targetTime: target });

    video.addEventListener("seeked", complete);
    video.addEventListener("loadeddata", complete);
    video.addEventListener("canplay", complete);
    video.addEventListener("error", fail);

    try {
      if (Number.isFinite(target) && Math.abs(Number(video.currentTime || 0) - target) > 0.35) {
        video.currentTime = target;
      }
    } catch (_) {}

    complete();
    timer = setTimeout(() => {
      if (done || !currentOperationMatches()) return;
      if (readyEnough()) {
        complete();
        return;
      }
      done = true;
      cleanup();
      setStatus("ready");
      reportTileState("timeout", { reason, targetTime: target });
    }, timeoutMs);
  }

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const hasVideo = Boolean(playback?.hasVideo);
    const cameraId = playback?.cameraId;
    const relPath = playback?.relPath;
    const offsetSec = Number(playback?.offsetSec || 0);
    let cancelled = false;
    let refreshAttempts = 0;
    let loadSequence = 0;
    let pendingLoad = null;
    let refreshInFlight = false;
    let refreshWatchdog = null;

    function clearRefreshWatchdog() {
      if (refreshWatchdog) {
        clearTimeout(refreshWatchdog);
        refreshWatchdog = null;
      }
    }

    function finishRefreshCycle() {
      refreshInFlight = false;
      clearRefreshWatchdog();
    }

    const hardReset = () => {
      finishRefreshCycle();
      clearDimensionProbe();
      setNaturalResolution({ width: 0, height: 0, source: "missing" });
      setReadyState(0);
      setCanvasFrame({ ready: false, generation: "", reason: "hard-reset", error: "" });
      try {
        video.pause();
      } catch (_) {}
      try {
        video.removeAttribute("src");
      } catch (_) {}
      currentSourceRef.current = { playbackKey: "", relPath: "", cameraId: "" };
      try {
        video.load();
      } catch (_) {}
    };

    function updateNaturalResolution() {
      sampleVideoDimensions("video-event");
    }

    function restorePlaybackState(loadState) {
      if (cancelled || !loadState || loadState.sequence !== loadSequence) return;
      finishRefreshCycle();
      updateNaturalResolution();

      prepareVideoAtTarget({
        targetTime: loadState.targetTime,
        playIntent: loadState.playIntent,
        reason: "source-ready",
      });
      pendingLoad = null;
    }

    async function loadFreshMedia({ preserveTime = false, playIntent = playIntentRef.current } = {}) {
      const sequence = loadSequence + 1;
      loadSequence = sequence;
      const targetTime = preserveTime ? Number(video.currentTime || 0) : Math.max(0, offsetSec);
      pendingLoad = {
        sequence,
        targetTime,
        playIntent,
        playbackRate: playbackRateRef.current,
      };
      setNaturalResolution({ width: 0, height: 0, source: "missing" });
      setReadyState(0);
      setCanvasFrame({ ready: false, generation: "", reason: "load-media", error: "" });
      setStatus("loading");

      try {
        const src = await buildMediaUrl();
        if (cancelled || sequence !== loadSequence) return;
        currentSourceRef.current = {
          playbackKey: playback?.playbackKey || "",
          relPath: relPath || "",
          cameraId: cameraId || "",
        };
        video.src = src;
        video.load();
      } catch (_) {
        if (!cancelled && sequence === loadSequence) {
          pendingLoad = null;
          finishRefreshCycle();
          hardReset();
          setStatus("error");
          reportTileState("error", { reason: "media-token" });
        }
      }
    }

    function retryWithFreshMedia() {
      if (refreshInFlight) return;
      if (cancelled || refreshAttempts >= MEDIA_REFRESH_RETRY_LIMIT) {
        pendingLoad = null;
        hardReset();
        setStatus("unsupported");
        reportTileState("unsupported", { reason: "refresh-exhausted" });
        return;
      }
      refreshAttempts += 1;
      refreshInFlight = true;
      clearRefreshWatchdog();
      refreshWatchdog = setTimeout(() => {
        if (cancelled || !refreshInFlight) return;
        pendingLoad = null;
        hardReset();
        setStatus("error");
        reportTileState("error", { reason: "refresh-timeout" });
      }, 20000);
      loadFreshMedia({
        preserveTime: true,
        playIntent: playIntentRef.current || !video.paused,
      });
    }

    if (!hasVideo || !cameraId || !relPath) {
      hardReset();
      setStatus("empty");
      reportTileState("empty", { reason: "no-archive" });
      return;
    }

    const handleLoaded = () => {
      updateNaturalResolution();
      startDimensionProbe("loaded-media-probe");
      restorePlaybackState(pendingLoad);
    };

    const handleError = () => {
      retryWithFreshMedia();
    };

    hardReset();
    reportTileState("loading", { reason: "source-load", offsetSec });

    video.addEventListener("loadedmetadata", handleLoaded);
    video.addEventListener("loadeddata", handleLoaded);
    video.addEventListener("canplay", handleLoaded);
    video.addEventListener("playing", handleLoaded);
    video.addEventListener("resize", handleLoaded);
    video.addEventListener("error", handleError);

    const handleStalledMedia = () => {
      retryWithFreshMedia();
    };

    video.addEventListener("stalled", handleStalledMedia);

    loadFreshMedia();

    return () => {
      cancelled = true;
      pendingLoad = null;
      loadSequence += 1;
      finishRefreshCycle();
      video.removeEventListener("loadedmetadata", handleLoaded);
      video.removeEventListener("loadeddata", handleLoaded);
      video.removeEventListener("canplay", handleLoaded);
      video.removeEventListener("playing", handleLoaded);
      video.removeEventListener("resize", handleLoaded);
      video.removeEventListener("error", handleError);
      video.removeEventListener("stalled", handleStalledMedia);
      clearDimensionProbe();
    };
  }, [playback?.playbackKey]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    const updateRect = () => {
      const rect = el.getBoundingClientRect();
      setViewerRect({ width: Math.round(rect.width || 0), height: Math.round(rect.height || 0) });
      setCanvasFrame((prev) => ({ ...prev, ready: false, reason: "resize", error: "" }));
    };
    updateRect();
    const observer = new ResizeObserver(updateRect);
    observer.observe(el);
    return () => observer.disconnect();
  }, [playback?.playbackKey]);

  useEffect(() => {
    function handleFullscreenChange() {
      const fullscreenElement = document.fullscreenElement;
      const wrap = wrapRef.current;
      setIsFullscreen(Boolean(fullscreenElement && (fullscreenElement === wrap || wrap?.contains(fullscreenElement))));
      setCanvasFrame((prev) => ({ ...prev, ready: false, reason: "fullscreen-change", error: "" }));
    }
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    handleFullscreenChange();
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    try {
      video.playbackRate = Number(speed || 1);
    } catch (_) {}
  }, [speed]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    if (status === "ready" || status === "playing") {
      if (isPlaying && !coordinatorHolding) {
        try {
          video.playbackRate = Number(speed || 1);
        } catch (_) {}
        video.play().catch(() => {});
        setStatus("playing");
      } else {
        try {
          video.pause();
        } catch (_) {}
        setStatus("ready");
      }
    }
  }, [coordinatorHolding, isPlaying, speed, status]);

  useEffect(() => {
    const operationId = coordination?.operationId || "";
    if (!operationId || coordination?.releaseState !== "holding") return;

    const video = videoRef.current;
    if (!video) return;

    if (!playback?.hasVideo || !playback?.cameraId || !playback?.relPath) {
      setStatus("empty");
      reportTileState("empty", { reason: "operation-empty" });
      return;
    }

    const currentSource = currentSourceRef.current;
    const sameSourceReady =
      currentSource.playbackKey === (playback?.playbackKey || "") &&
      currentSource.relPath === (playback?.relPath || "") &&
      currentSource.cameraId === String(playback?.cameraId || "") &&
      video.currentSrc;

    if (!sameSourceReady) {
      setStatus("loading");
      reportTileState("loading", { reason: "operation-waiting-for-source" });
      return;
    }

    prepareVideoAtTarget({
      targetTime: Number(playback?.offsetSec || 0),
      playIntent: false,
      reason: "same-source-seek",
    });
  }, [coordination?.operationId, coordination?.releaseState, playback?.cameraId, playback?.hasVideo, playback?.offsetSec, playback?.playbackKey, playback?.relPath, reportTileState]);

  return (
    <div
      ref={wrapRef}
      className={`archiveVideoWrap ${compactCanvasRequested ? "compactVideoCanvasRequested" : ""} ${nativeVideoSuppressed ? "compactVideoCanvasActive" : ""}`}
      onDoubleClick={allowFullscreen ? toggleFullscreen : undefined}
      title={allowFullscreen ? "Двойной клик для полноэкранного режима" : undefined}
      data-highres-adaptive={nativeVideoSuppressed ? "true" : "false"}
      data-natural-resolution={`${naturalResolution.width}x${naturalResolution.height}`}
      data-render-context="chronology"
      data-renderer={nativeVideoSuppressed ? "canvas" : "native"}
      data-render-mode={renderState.mode}
      data-quality-tier={renderState.qualityTier}
      data-downscale-ratio={renderState.ratio == null ? "" : renderState.ratio.toFixed(4)}
      data-rendered-rect={`${viewerRect.width}x${viewerRect.height}`}
      data-decoded-resolution={`${naturalResolution.width}x${naturalResolution.height}`}
      data-source-resolution={`${naturalResolution.width}x${naturalResolution.height}`}
      data-ready-state={readyState}
      data-dimension-source={naturalResolution.source || "missing"}
      data-canvas-ready={nativeVideoSuppressed ? "true" : "false"}
      data-first-frame-drawn={canvasFrame.ready && canvasFrame.generation === canvasGeneration ? "true" : "false"}
      data-canvas-draw-error={canvasFrame.error || ""}
      data-canvas-generation={canvasGeneration}
      data-fullscreen={isFullscreen ? "true" : "false"}
      data-playback-status={status}
      data-playback-operation={coordination?.operationId || ""}
      data-playback-release-state={coordination?.releaseState || "released"}
      data-playback-offset-sec={String(Number(playback?.offsetSec || 0))}
      data-playback-key={playback?.playbackKey || ""}
      data-playback-rel-path={playback?.relPath || ""}
    >
      <VideoZoomPanSurface
        className="archiveVideoZoomSurface"
        context="chronology"
        sourceKey={playback?.playbackKey || "empty"}
      >
        <video
          key={playback?.playbackKey || "empty"}
          ref={videoRef}
          className={`archiveVideo ${nativeVideoSuppressed ? "nativeVideoSuppressed" : ""}`}
          muted
          autoPlay={false}
          playsInline
          controls={false}
        />
        <CompactVideoCanvas
          videoRef={videoRef}
          active={compactCanvasRequested}
          mode={renderState.mode}
          ratio={renderState.ratio}
          backingScale={renderState.backingScale}
          generation={canvasGeneration}
          onFrameState={handleCanvasFrameState}
          className="archiveCompactVideoCanvas"
        />
      </VideoZoomPanSurface>

      {status === "loading" ? (
        <div className="archiveCenterHint">Загрузка записи...</div>
      ) : null}

      {status === "empty" ? (
        <div className="archiveCenterHint">{unavailableText}</div>
      ) : null}

      {status === "error" ? (
        <div className="archiveCenterHint archiveCenterHintError">
          Не удалось воспроизвести запись
        </div>
      ) : null}

      {status === "unsupported" ? (
        <div className="archiveCenterHint archiveCenterHintError archiveCenterHintInteractive">
          <div>
            <div>Браузер не смог воспроизвести запись онлайн.</div>
            <a
              className="archiveDownloadLink"
              href="#download"
              onClick={(event) => {
                event.preventDefault();
                handleUnsupportedDownload();
              }}
            >
              Скачать запись
            </a>
          </div>
        </div>
      ) : null}
    </div>
  );
}
