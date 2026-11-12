"use client";
import { isArchiveRecoveryHealthy } from "../lib/archivePlaybackRecovery";
import { archivePositionMs, archiveSourceCovers, archiveTargetOffset, archiveTargetReady, confirmedArchiveEmpty } from "../lib/archiveGroupPlayback";

import { useCallback, useEffect, useRef, useState } from "react";
import CompactVideoCanvas from "./CompactVideoCanvas";
import VideoZoomPanSurface from "./VideoZoomPanSurface";
import { issueChronologyMediaToken } from "../lib/api";
import { useI18n } from "../lib/i18n";
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
  registerPlaybackTile,
  onDesktopDoubleClick,
  onTouchDoubleTap,
}) {
  const { text } = useI18n();
  const unavailableText = playback?.availabilityStatus === "root_unavailable"
    ? "Том архива сейчас недоступен"
    : playback?.availabilityStatus === "root_unresolved"
      ? "Не удалось определить расположение записи"
      : confirmedArchiveEmpty(playback) ? "Нет записи на это время" : "Запись недоступна";
  const wrapRef = useRef(null);
  const videoRef = useRef(null);
  const heldFrameRef = useRef(null);
  const [heldFrame, setHeldFrame] = useState(false);
  const playIntentRef = useRef(false);
  const playbackRateRef = useRef(1);
  const coordinationRef = useRef(coordination);
  const currentSourceRef = useRef({ playbackKey: "", relPath: "", cameraId: "" });
  const sourceRecoveryRef = useRef(null);
  const latestPlaybackRef = useRef(playback);
  latestPlaybackRef.current = playback;
  const prepareSequenceRef = useRef(0);
  const prepareCancelRef = useRef(null);
  const unsupportedDownloadBusyRef = useRef(false);
  const dimensionProbeTimerRef = useRef(null);
  const [status, setStatus] = useState("idle");
  const [naturalResolution, setNaturalResolution] = useState({ width: 0, height: 0, source: "missing" });
  const [viewerRect, setViewerRect] = useState({ width: 0, height: 0 });
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [zoomActive, setZoomActive] = useState(false);
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
  const compactCanvasRequested = !zoomActive && renderState.renderer === "canvas" && readyState >= 2;
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
    const nextFrame = {
      ready: Boolean(next?.ready),
      generation: String(next?.generation || ""),
      reason: next?.reason || "",
      error: next?.error || "",
    };
    setCanvasFrame((previous) => (
      previous.ready === nextFrame.ready
      && previous.generation === nextFrame.generation
      && previous.reason === nextFrame.reason
      && previous.error === nextFrame.error
        ? previous
        : nextFrame
    ));
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
        positionMs: archivePositionMs(latestPlaybackRef.current, videoRef.current?.currentTime),
        ...facts,
      });
    },
    [onTilePlaybackState, playback?.offsetSec, playback?.playbackKey, playback?.relPath, tileId]
  );

  function captureCurrentFrame() {
    const video = videoRef.current, canvas = heldFrameRef.current;
    if (!canvas || !video || video.readyState < 2 || !video.videoWidth || !video.videoHeight) return;
    try {
      canvas.width = Math.min(video.videoWidth, Math.max(1, Math.round((wrapRef.current?.clientWidth || video.videoWidth) * (globalThis.devicePixelRatio || 1))));
      canvas.height = Math.max(1, Math.round(canvas.width * video.videoHeight / video.videoWidth));
      canvas.getContext("2d", { alpha: false }).drawImage(video, 0, 0, canvas.width, canvas.height);
      setHeldFrame(true);
    } catch (_) { /* The native paused frame remains the safe fallback. */ }
  }

  useEffect(() => {
    if (!tileId || !registerPlaybackTile) return;
    return registerPlaybackTile(tileId, {
      pause() { captureCurrentFrame(); videoRef.current?.pause(); },
      sample() {
        if (currentSourceRef.current.playbackKey !== latestPlaybackRef.current?.playbackKey) return null;
        if (!videoRef.current?.currentSrc || videoRef.current.readyState < 2 || videoRef.current.seeking) return null;
        return { positionMs: archivePositionMs(latestPlaybackRef.current, videoRef.current?.currentTime) };
      },
    });
  }, [tileId, registerPlaybackTile]);

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

  async function buildMediaUrl(signal) {
    if (!playback?.cameraId || !playback?.relPath) return "";
    const mediaToken = await issueChronologyMediaToken(playback.cameraId, playback.relPath, playback, { signal });
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
      return Math.min(next, Math.max(0, duration - 0.01));
    }
    return next;
  }

  function prepareVideoAtTarget({
    targetTime,
    playIntent = playIntentRef.current,
    reason = "prepare",
    timeoutMs = 6000,
  } = {}) {
    prepareCancelRef.current?.();
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
      video.removeEventListener("canplaythrough", complete);
      video.removeEventListener("progress", complete);
      video.removeEventListener("error", fail);
      if (timer) clearTimeout(timer);
      timer = null;
      if (prepareCancelRef.current === cancel) prepareCancelRef.current = null;
    };

    const cancel = () => { done = true; cleanup(); };
    prepareCancelRef.current = cancel;

    const currentOperationMatches = () =>
      sequence === prepareSequenceRef.current &&
      (!operationId || coordinationRef.current?.operationId === operationId);

    const readyEnough = () => archiveTargetReady(video, target, {
      resume: Boolean(coordinationRef.current?.releaseState === "holding" ? coordinationRef.current.shouldResume : playIntentRef.current), speed: playbackRateRef.current,
    });

    function complete() {
      if (done) return;
      if (!currentOperationMatches()) { cancel(); return; }
      if (!readyEnough()) return;
      done = true;
      cleanup();
      sampleVideoDimensions(reason);
      setHeldFrame(false);
      try {
        video.playbackRate = Number(playbackRateRef.current || 1);
      } catch (_) {}
      if (playIntentRef.current && !["holding", "cancelled"].includes(coordinationRef.current?.releaseState)) {
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
      if (done) return;
      if (!currentOperationMatches()) { cancel(); return; }
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
    video.addEventListener("canplaythrough", complete);
    video.addEventListener("progress", complete);
    video.addEventListener("error", fail);

    try {
      // Seek inside the readiness tolerance too; otherwise a paused delta in
      // the old 0.20–0.35 second dead zone could never become ready.
      if (Number.isFinite(target) && Math.abs(Number(video.currentTime || 0) - target) > 0.05) {
        video.currentTime = target;
      }
    } catch (_) {}

    complete();
    if (done) return;
    timer = setTimeout(() => {
      if (done) return;
      if (!currentOperationMatches()) { cancel(); return; }
      if (readyEnough()) {
        complete();
        return;
      }
      setStatus("loading");
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
    let recoveryProgressStart = null;
    let stableReadyTimer = null;
    let loadSequence = 0;
    let pendingLoad = null;
    let refreshInFlight = false;
    let refreshWatchdog = null;
    let tokenController = null;
    let tokenTimeout = null;

    function clearRefreshWatchdog() {
      if (refreshWatchdog) {
        clearTimeout(refreshWatchdog);
        refreshWatchdog = null;
      }
    }

    function finishRefreshCycle() {
      clearTimeout(stableReadyTimer);
      refreshInFlight = false;
      clearRefreshWatchdog();
    }

    const hardReset = () => {
      captureCurrentFrame();
      prepareCancelRef.current?.();
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
        targetTime: coordinationRef.current?.releaseState === "holding"
          ? archiveTargetOffset(latestPlaybackRef.current, coordinationRef.current?.targetMs) : loadState.targetTime,
        playIntent: loadState.playIntent,
        reason: "source-ready",
      });
      recoveryProgressStart = loadState.targetTime;
      pendingLoad = null;
      if (refreshAttempts > 0) {
        stableReadyTimer = setTimeout(() => {
          if (loadState.sequence === loadSequence) handleHealthyProgress(2000);
        }, 2000);
      }
    }

    async function loadFreshMedia({ preserveTime = false, playIntent = playIntentRef.current, targetTime: requestedTime } = {}) {
      const sequence = loadSequence + 1;
      loadSequence = sequence;
      const targetTime = Number.isFinite(requestedTime) ? Math.max(0, requestedTime)
        : preserveTime ? Number(video.currentTime || 0) : archiveTargetOffset(latestPlaybackRef.current, coordinationRef.current?.releaseState === "holding" ? coordinationRef.current?.targetMs : undefined);
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

      tokenController?.abort();
      clearTimeout(tokenTimeout);
      const controller = new AbortController();
      tokenController = controller;
      const timeout = setTimeout(() => controller.abort(), 12000);
      tokenTimeout = timeout;
      try {
        const src = await buildMediaUrl(controller.signal);
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
      } finally {
        clearTimeout(timeout);
        if (tokenTimeout === timeout) tokenTimeout = null;
        if (tokenController === controller) tokenController = null;
      }
    }

    function retryWithFreshMedia() {
      if (refreshInFlight) return;
      if (cancelled || refreshAttempts >= MEDIA_REFRESH_RETRY_LIMIT) {
        pendingLoad = null;
        const failure = [3, 4].includes(Number(video.error?.code)) ? "unsupported" : "error";
        hardReset();
        setStatus(failure);
        reportTileState(failure, { reason: "refresh-exhausted" });
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
      if (confirmedArchiveEmpty(playback)) {
        setHeldFrame(false);
        setStatus("empty");
        reportTileState("empty", { reason: "no-archive" });
      } else {
        const next = playback?.lookupState === "error" ? "error" : "loading";
        setStatus(next);
        reportTileState(next, { reason: "archive-lookup" });
      }
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
    const handleHealthyProgress = (stableReadyMs = 0) => {
      if (cancelled || pendingLoad || refreshInFlight) return;
      if (isArchiveRecoveryHealthy(video, recoveryProgressStart, typeof stableReadyMs === "number" ? stableReadyMs : 0)) {
        clearTimeout(stableReadyTimer);
        refreshAttempts = 0;
        recoveryProgressStart = null;
      }
      reportTileState("progress");
    };

    hardReset();
    reportTileState("loading", { reason: "source-load", offsetSec });

    video.addEventListener("loadedmetadata", handleLoaded);
    video.addEventListener("loadeddata", handleLoaded);
    video.addEventListener("canplay", handleLoaded);
    video.addEventListener("playing", handleLoaded);
    video.addEventListener("resize", handleLoaded);
    video.addEventListener("error", handleError);
    video.addEventListener("timeupdate", handleHealthyProgress);

    const handleStalledMedia = () => {
      if (video.paused && coordinationRef.current?.releaseState !== "holding") return;
      if (!archiveTargetReady(video, Number(video.currentTime || 0), { resume: true, speed: playbackRateRef.current })) {
        captureCurrentFrame();
        reportTileState("buffering", { reason: "waiting-for-data" });
      }
    };
    const handleEnded = () => reportTileState("boundary", {
      targetMs: latestPlaybackRef.current?.sourceEndMs,
      reason: "next-recording",
    });

    video.addEventListener("stalled", handleStalledMedia);
    video.addEventListener("waiting", handleStalledMedia);
    video.addEventListener("ended", handleEnded);

    const recovery = {
      isLoading: () => Boolean(pendingLoad),
      ensureMedia({ targetTime, playIntent = false }) {
        if (cancelled) return;
        if (pendingLoad) {
          pendingLoad.targetTime = Math.max(0, Number(targetTime || 0));
          pendingLoad.playIntent = playIntent;
          return;
        }
        // A new user operation gets a fresh bounded attempt. It does not start
        // an automatic retry loop or remount a healthy source on every seek.
        refreshAttempts = 0;
        finishRefreshCycle();
        loadFreshMedia({ targetTime, playIntent });
      },
    };
    sourceRecoveryRef.current = recovery;

    loadFreshMedia();

    return () => {
      cancelled = true;
      tokenController?.abort();
      clearTimeout(tokenTimeout);
      if (sourceRecoveryRef.current === recovery) sourceRecoveryRef.current = null;
      prepareCancelRef.current?.();
      pendingLoad = null;
      loadSequence += 1;
      finishRefreshCycle();
      video.removeEventListener("loadedmetadata", handleLoaded);
      video.removeEventListener("loadeddata", handleLoaded);
      video.removeEventListener("canplay", handleLoaded);
      video.removeEventListener("playing", handleLoaded);
      video.removeEventListener("resize", handleLoaded);
      video.removeEventListener("error", handleError);
      video.removeEventListener("timeupdate", handleHealthyProgress);
      video.removeEventListener("stalled", handleStalledMedia);
      video.removeEventListener("waiting", handleStalledMedia);
      video.removeEventListener("ended", handleEnded);
      clearDimensionProbe();
    };
  }, [playback?.playbackKey]);

  useEffect(() => () => { prepareCancelRef.current?.(); }, []);

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
      setIsFullscreen(Boolean(fullscreenElement && (
        fullscreenElement === wrap ||
        fullscreenElement.contains?.(wrap) ||
        wrap?.contains(fullscreenElement)
      )));
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
      const next = confirmedArchiveEmpty(playback) ? "empty" : playback?.lookupState === "error" ? "error" : "loading";
      setStatus(next);
      reportTileState(next, { reason: next === "empty" ? "operation-empty" : "archive-lookup" });
      return;
    }
    if (Number.isFinite(coordination?.targetMs) && Number.isFinite(playback.sourceEndMs)
        && !archiveSourceCovers(playback, coordination.targetMs)) {
      setStatus("loading");
      reportTileState("loading", { reason: "waiting-for-next-recording" });
      return;
    }

    const currentSource = currentSourceRef.current;
    const sameSourceReady =
      currentSource.playbackKey === (playback?.playbackKey || "") &&
      currentSource.relPath === (playback?.relPath || "") &&
      currentSource.cameraId === String(playback?.cameraId || "") &&
      video.currentSrc;

    if (!sameSourceReady || sourceRecoveryRef.current?.isLoading()) {
      setStatus("loading");
      reportTileState("loading", { reason: "operation-waiting-for-source" });
      sourceRecoveryRef.current?.ensureMedia({ targetTime: archiveTargetOffset(playback, coordination?.targetMs), playIntent: false });
      return;
    }

    prepareVideoAtTarget({
      targetTime: archiveTargetOffset(playback, coordination?.targetMs),
      playIntent: false,
      reason: "same-source-seek",
    });
  }, [coordination?.operationId, coordination?.releaseState, coordination?.targetMs, playback?.cameraId, playback?.hasVideo, playback?.lookupState, playback?.offsetSec, playback?.playbackKey, playback?.relPath, reportTileState]);

  return (
    <div
      ref={wrapRef}
      className={`archiveVideoWrap ${compactCanvasRequested ? "compactVideoCanvasRequested" : ""} ${nativeVideoSuppressed ? "compactVideoCanvasActive" : ""}`}
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
        onDesktopDoubleClick={onDesktopDoubleClick}
        onTouchDoubleTap={onTouchDoubleTap}
        onZoomActiveChange={setZoomActive}
      >
        <video
          ref={videoRef}
          className={`archiveVideo ${nativeVideoSuppressed ? "nativeVideoSuppressed" : ""}`}
          muted
          autoPlay={false}
          playsInline
          controls={false}
        />
        <canvas ref={heldFrameRef} className={`archiveHeldFrame${heldFrame ? " is-visible" : ""}`} aria-hidden="true" />
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
        <div className="archiveCenterHint" role="status"><span className="archiveLoadingSpinner" aria-hidden="true" />{text("Загрузка записи...")}</div>
      ) : null}
      {coordinatorHolding && coordination?.shouldResume && ["ready", "playing"].includes(status) ? (
        <div className="archiveGroupPause" role="status">⏸ {text("Ожидание других записей")}</div>
      ) : null}

      {status === "empty" ? (
        <div className="archiveCenterHint">{text(unavailableText)}</div>
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
