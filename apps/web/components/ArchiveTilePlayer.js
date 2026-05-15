"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import CompactVideoCanvas from "./CompactVideoCanvas";
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
}) {
  const wrapRef = useRef(null);
  const videoRef = useRef(null);
  const playIntentRef = useRef(false);
  const playbackRateRef = useRef(1);
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

  playIntentRef.current = Boolean(isPlaying);
  playbackRateRef.current = Number(speed || 1);

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
    const mediaToken = await issueChronologyMediaToken(playback.cameraId, playback.relPath);
    return (
      `/api/chronology/file?camera_id=${playback.cameraId}` +
      `&rel_path=${encodeURIComponent(playback.relPath)}` +
      `&media_token=${encodeURIComponent(mediaToken)}`
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
      try {
        video.load();
      } catch (_) {}
    };

    function safeTargetTime(value) {
      const next = Math.max(0, Number(value || 0));
      const duration = Number(video.duration || 0);
      if (Number.isFinite(duration) && duration > 0) {
        return Math.min(next, Math.max(0, duration - 0.25));
      }
      return next;
    }

    function updateNaturalResolution() {
      sampleVideoDimensions("video-event");
    }

    function restorePlaybackState(loadState) {
      if (cancelled || !loadState || loadState.sequence !== loadSequence) return;
      finishRefreshCycle();
      updateNaturalResolution();

      try {
        video.currentTime = safeTargetTime(loadState.targetTime);
      } catch (_) {}

      try {
        video.playbackRate = Number(loadState.playbackRate || 1);
      } catch (_) {}

      if (loadState.playIntent) {
        video.play().catch(() => {});
        setStatus("playing");
      } else {
        try {
          video.pause();
        } catch (_) {}
        setStatus("ready");
      }
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
        video.src = src;
        video.load();
      } catch (_) {
        if (!cancelled && sequence === loadSequence) {
          pendingLoad = null;
          finishRefreshCycle();
          hardReset();
          setStatus("error");
        }
      }
    }

    function retryWithFreshMedia() {
      if (refreshInFlight) return;
      if (cancelled || refreshAttempts >= MEDIA_REFRESH_RETRY_LIMIT) {
        pendingLoad = null;
        hardReset();
        setStatus("unsupported");
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
      }, 20000);
      loadFreshMedia({
        preserveTime: true,
        playIntent: playIntentRef.current || !video.paused,
      });
    }

    if (!hasVideo || !cameraId || !relPath) {
      hardReset();
      setStatus("empty");
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
      if (isPlaying) {
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
  }, [isPlaying, speed, status]);

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

      {status === "loading" ? (
        <div className="archiveCenterHint">Загрузка записи...</div>
      ) : null}

      {status === "empty" ? (
        <div className="archiveCenterHint">Запись недоступна</div>
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
