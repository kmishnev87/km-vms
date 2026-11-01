"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import CompactVideoCanvas from "./CompactVideoCanvas";
import VideoZoomPanSurface from "./VideoZoomPanSurface";
import { apiFetch, issueLiveMediaTokenInfo } from "../lib/api";
import { useI18n } from "../lib/i18n";
import { liveFailureMessage, liveRecoveryDelay, notifyLiveRuntimeChanged } from "../lib/livePlaybackRecovery";
import {
  isCompactSmoothingSourceVideo,
  normalizeVideoDimensions,
  selectCompactVideoRenderMode,
} from "../lib/playbackResolution";

const READY_POLL_INTERVAL_MS = 1500;
const VIEWER_TOUCH_INTERVAL_MS = 15000;
const MEDIA_TOKEN_REFRESH_SAFETY_MS = 90000;
const MEDIA_TOKEN_MIN_REFRESH_MS = 30000;

const TEXT = {
  noToken: "\u041d\u0435\u0442 \u0442\u043e\u043a\u0435\u043d\u0430 \u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0430\u0446\u0438\u0438",
  loading: "\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0430\u0435\u043c \u043f\u043e\u0442\u043e\u043a...",
  waiting: "\u0416\u0434\u0451\u043c \u0433\u043e\u0442\u043e\u0432\u043d\u043e\u0441\u0442\u044c \u043f\u043e\u0442\u043e\u043a\u0430...",
  failedPlay: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0432\u043e\u0441\u043f\u0440\u043e\u0438\u0437\u0432\u0435\u0441\u0442\u0438 \u043f\u043e\u0442\u043e\u043a",
  failedStart: "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c \u043f\u043e\u0442\u043e\u043a",
  browserUnsupported: "\u0411\u0440\u0430\u0443\u0437\u0435\u0440 \u043d\u0435 \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0435\u0442 HLS",
  doubleClick: "\u0414\u0432\u043e\u0439\u043d\u043e\u0439 \u043a\u043b\u0438\u043a \u2014 \u043d\u0430 \u0432\u0435\u0441\u044c \u044d\u043a\u0440\u0430\u043d",
};

function extractErrorMessage(err, fallback) {
  const raw = err?.message || "";
  if (!raw) return fallback;

  try {
    const parsed = JSON.parse(raw);
    const detail = parsed?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message;
    if (detail?.code) return detail.code;
  } catch (_) {}

  return raw || fallback;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default function TilePlayer({
  cameraId,
  stream,
  audioEnabled = false,
  audioRequestId = 0,
  onAudioStatusChange,
  onAudioPlaybackBlocked,
  onDesktopDoubleClick,
  onTouchDoubleTap,
}) {
  const { text } = useI18n();
  const videoRef = useRef(null);
  const wrapRef = useRef(null);
  const hlsRef = useRef(null);
  const retryTimerRef = useRef(null);
  const mediaTokenRefreshTimerRef = useRef(null);
  const mediaTokenRefreshInFlightRef = useRef(null);
  const nativeHlsCleanupRef = useRef(null);
  const dimensionProbeTimerRef = useRef(null);
  const audioStatusChangeRef = useRef(onAudioStatusChange);
  const audioPlaybackBlockedRef = useRef(onAudioPlaybackBlocked);

  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [naturalResolution, setNaturalResolution] = useState({ width: 0, height: 0, source: "missing" });
  const [viewerRect, setViewerRect] = useState({ width: 0, height: 0 });
  const [readyState, setReadyState] = useState(0);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [zoomActive, setZoomActive] = useState(false);
  const [canvasFrame, setCanvasFrame] = useState({
    ready: false,
    generation: "",
    reason: "inactive",
    error: "",
  });

  const sourceEligibleForCompactSmoothing =
    stream === "main" || isCompactSmoothingSourceVideo(naturalResolution);
  const renderState = selectCompactVideoRenderMode({
    dimensions: naturalResolution,
    rect: viewerRect,
    isFullscreen,
    sourceHighResolution: sourceEligibleForCompactSmoothing,
  });

  function reportAudioStatus(item, source = "live-status") {
    if (typeof audioStatusChangeRef.current !== "function") return;
    const audioMode = String(item?.audio_mode || "unknown").toLowerCase();
    const audioEnabledByConfig = item?.audio_enabled !== false && !["none", "off", "disable", "disabled"].includes(audioMode);
    const inputHasAudio = item?.audio_available === true || Boolean(item?.input_audio_codec);
    audioStatusChangeRef.current({
      source,
      audioMode,
      audioEnabled: audioEnabledByConfig,
      audioAvailable: Boolean(audioEnabledByConfig && inputHasAudio),
      audioDisabledByConfig: !audioEnabledByConfig || audioMode === "none",
      audioReason: item?.audio_reason || (audioEnabledByConfig ? "unknown" : "disabled"),
      inputAudioCodec: item?.input_audio_codec || "",
      inputAudioChannels: item?.input_audio_channels || null,
      inputAudioSampleRate: item?.input_audio_sample_rate || null,
    });
  }
  const compactCanvasRequested = !zoomActive && renderState.renderer === "canvas" && readyState >= 2;
  const canvasGeneration = [
    cameraId || "",
    stream || "",
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

  function destroyPlayer() {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (mediaTokenRefreshTimerRef.current) {
      clearTimeout(mediaTokenRefreshTimerRef.current);
      mediaTokenRefreshTimerRef.current = null;
    }
    mediaTokenRefreshInFlightRef.current = null;

    if (nativeHlsCleanupRef.current) {
      try {
        nativeHlsCleanupRef.current();
      } catch (_) {}
      nativeHlsCleanupRef.current = null;
    }

    if (hlsRef.current) {
      try {
        hlsRef.current.destroy();
      } catch (_) {}
      hlsRef.current = null;
    }
    clearDimensionProbe();
    setReadyState(0);
    setNaturalResolution({ width: 0, height: 0, source: "missing" });
    setCanvasFrame({ ready: false, generation: "", reason: "destroy", error: "" });

    const video = videoRef.current;
    if (video) {
      try {
        video.pause();
        video.removeAttribute("src");
        video.load();
      } catch (_) {}
    }
  }

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
  }, [cameraId, stream]);

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
    const updateMetadata = () => sampleVideoDimensions("video-event");
    const resetReadiness = () => {
      setReadyState(Number(video.readyState || 0));
      setCanvasFrame((prev) => ({ ...prev, ready: false, reason: "video-state", error: "" }));
    };
    video.addEventListener("loadedmetadata", updateMetadata);
    video.addEventListener("loadeddata", updateMetadata);
    video.addEventListener("canplay", updateMetadata);
    video.addEventListener("playing", updateMetadata);
    video.addEventListener("resize", updateMetadata);
    video.addEventListener("emptied", resetReadiness);
    video.addEventListener("waiting", resetReadiness);
    video.addEventListener("stalled", resetReadiness);
    return () => {
      video.removeEventListener("loadedmetadata", updateMetadata);
      video.removeEventListener("loadeddata", updateMetadata);
      video.removeEventListener("canplay", updateMetadata);
      video.removeEventListener("playing", updateMetadata);
      video.removeEventListener("resize", updateMetadata);
      video.removeEventListener("emptied", resetReadiness);
      video.removeEventListener("waiting", resetReadiness);
      video.removeEventListener("stalled", resetReadiness);
    };
  }, [cameraId, stream]);

  useEffect(() => {
    let cancelled = false;
    let recoveryAttempt = 0;
    let recoveryScheduled = false;
    let starting = false;
    let generation = 0;
    let everPlayed = false;
    let healthySince = 0;
    let lastPlaybackTime = 0;
    let lastPlaybackProgressAt = Date.now();
    let lastTokenError = null;
    let lastRuntimeState = "";
    let currentViewerId = null;
    let viewerTouchTimer = null;
    const sourceKey = {
      cameraId: cameraId ? Number(cameraId) : null,
      stream: stream || null,
    };
    setCanvasFrame({ ready: false, generation: "", reason: "source-change", error: "" });

    async function closeViewer() {
      if (viewerTouchTimer) {
        clearInterval(viewerTouchTimer);
        viewerTouchTimer = null;
      }

      const viewerId = currentViewerId;
      currentViewerId = null;
      if (!viewerId) return;

      try {
        await apiFetch(`/live/viewers/${encodeURIComponent(viewerId)}`, {
          method: "DELETE",
        });
      } catch (_) {}
      notifyLiveRuntimeChanged();
    }

    function startViewerHeartbeat() {
      if (viewerTouchTimer) {
        clearInterval(viewerTouchTimer);
        viewerTouchTimer = null;
      }

      viewerTouchTimer = setInterval(() => {
        const viewerId = currentViewerId;
        if (!viewerId || cancelled) return;

        apiFetch(`/live/viewers/${encodeURIComponent(viewerId)}/touch`, {
          method: "POST",
        }).then((result) => {
          if (!cancelled && viewerId === currentViewerId && result?.touched === false) failWithMessage(TEXT.failedPlay);
        }).catch((err) => {
          if (!cancelled && [401, 403].includes(err?.status)) failWithMessage(TEXT.failedPlay, err);
        });
      }, VIEWER_TOUCH_INTERVAL_MS);
    }

    async function waitForReady(expectedGeneration) {
      const startedWaitingAt = Date.now();
      let lastItem = null;
      while (!cancelled && generation === expectedGeneration) {
        try {
          const response = await apiFetch(
            `/live/status?camera_id=${encodeURIComponent(sourceKey.cameraId)}&stream=${encodeURIComponent(sourceKey.stream)}`
          );
          if (cancelled || generation !== expectedGeneration) break;
          const item = response?.items?.[0];
          if (item) lastItem = item;
          const runtimeState = `${item?.status || "missing"}|${item?.safe_failure_reason || item?.failure_reason || ""}|${item?.retry_count || 0}`;
          if (runtimeState !== lastRuntimeState) {
            lastRuntimeState = runtimeState;
            notifyLiveRuntimeChanged();
          }
          if (item?.source_resolution?.width && item?.source_resolution?.height) {
            applyNaturalResolution(item.source_resolution.width, item.source_resolution.height, "runtime-source");
          }
          reportAudioStatus(item, "live-status");
          if (item?.running && item?.ready) {
            return { ready: true, item };
          }
          if (item?.status === "failed" && !item?.next_retry_at) {
            return {
              ready: false,
              failed: true,
              message: liveFailureMessage(item?.safe_failure_reason || item?.failure_reason, everPlayed),
              item,
            };
          }
          if (item?.safe_failure_reason || item?.failure_reason || item?.status === "restarting") {
            setError(liveFailureMessage(item?.safe_failure_reason || item?.failure_reason, everPlayed));
          }
        } catch (err) {
          if ([401, 403, 404].includes(err?.status)) throw err;
          setError(liveFailureMessage("camera_unreachable", everPlayed));
        }

        setStatus("waiting");
        await sleep(Date.now() - startedWaitingAt > 10000 ? 5000 : READY_POLL_INTERVAL_MS);
      }

      return { ready: false, failed: false, message: TEXT.failedStart, item: lastItem };
    }

    function failWithMessage(message, cause = null) {
      if (cancelled || recoveryScheduled) return;
      generation += 1;
      destroyPlayer();
      void closeViewer();
      healthySince = 0;
      if ([401, 403, 404].includes(cause?.status)) {
        setStatus("error");
        setError(extractErrorMessage(cause, message || TEXT.failedStart));
        return;
      }
      recoveryScheduled = true;
      setStatus("waiting");
      setError(message || liveFailureMessage(null, everPlayed));
      const delay = liveRecoveryDelay(recoveryAttempt++);
      const retry = () => {
        if (!cancelled && starting) {
          retryTimerRef.current = setTimeout(retry, 250);
          return;
        }
        retryTimerRef.current = null;
        recoveryScheduled = false;
        if (!cancelled) void startPlayback();
      };
      retryTimerRef.current = setTimeout(retry, delay);
    }

    async function buildPlaylistUrl() {
      const tokenInfo = await issueLiveMediaTokenInfo(sourceKey.cameraId, sourceKey.stream);
      if (!tokenInfo.mediaToken) throw new Error(TEXT.failedPlay);
      return {
        url: `/api/live/${sourceKey.cameraId}/${sourceKey.stream}/index.m3u8`,
        expiresIn: tokenInfo.expiresIn,
      };
    }

    async function refreshPlaylistSource() {
      if (cancelled) return false;
      if (mediaTokenRefreshInFlightRef.current) {
        try {
          await mediaTokenRefreshInFlightRef.current;
          return true;
        } catch (_) {
          return false;
        }
      }

      const pendingRefresh = (async () => {
        const next = await buildPlaylistUrl();
        if (cancelled) return;
        // The response renews a scoped HttpOnly cookie. Neither native HLS nor
        // hls.js needs a source change, buffer reset or media reattachment.
        scheduleMediaTokenRefresh(next.expiresIn);
      })();
      mediaTokenRefreshInFlightRef.current = pendingRefresh;

      try {
        await pendingRefresh;
        lastTokenError = null;
        return true;
      } catch (err) {
        lastTokenError = err;
        return false;
      } finally {
        if (mediaTokenRefreshInFlightRef.current === pendingRefresh) {
          mediaTokenRefreshInFlightRef.current = null;
        }
      }
    }

    function scheduleMediaTokenRefresh(expiresIn) {
      if (mediaTokenRefreshTimerRef.current) {
        clearTimeout(mediaTokenRefreshTimerRef.current);
        mediaTokenRefreshTimerRef.current = null;
      }
      const ttlMs = Math.max(0, Number(expiresIn || 0) * 1000);
      const delay = Math.max(MEDIA_TOKEN_MIN_REFRESH_MS, ttlMs - MEDIA_TOKEN_REFRESH_SAFETY_MS);
      mediaTokenRefreshTimerRef.current = setTimeout(async () => {
        const refreshed = await refreshPlaylistSource();
        if (!cancelled && !refreshed) {
          failWithMessage(liveFailureMessage(null, everPlayed), lastTokenError);
        }
      }, delay);
    }

    async function attachPlayer(playlist) {
      const video = videoRef.current;
      if (!video) return;
      let authRefreshFailures = 0;

      destroyPlayer();
      scheduleMediaTokenRefresh(playlist.expiresIn);
      lastPlaybackProgressAt = Date.now();
      lastPlaybackTime = 0;

      if (Hls.isSupported()) {
        const hls = new Hls({
          lowLatencyMode: true,
          backBufferLength: 12,
          maxBufferLength: 6,
          liveSyncDurationCount: 2,
          liveMaxLatencyDurationCount: 4,
          manifestLoadingTimeOut: 8000,
          levelLoadingTimeOut: 8000,
          fragLoadingTimeOut: 12000,
          manifestLoadingMaxRetry: 2,
          levelLoadingMaxRetry: 2,
          fragLoadingMaxRetry: 2,
        });

        hlsRef.current = hls;

        hls.once(Hls.Events.MEDIA_ATTACHED, () => {
          if (!cancelled) {
            hls.loadSource(playlist.url);
          }
        });

        hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
          if (cancelled) return;
          const level = Array.isArray(data?.levels)
            ? data.levels.find((item) => item?.width && item?.height)
            : null;
          if (level?.width && level?.height) {
            applyNaturalResolution(level.width, level.height, "hls-manifest");
          }
          authRefreshFailures = 0;
          if ((Array.isArray(data?.audioTracks) && data.audioTracks.length) || (Array.isArray(hls.audioTracks) && hls.audioTracks.length)) {
            reportAudioStatus({ audio_mode: "manifest", audio_enabled: true, audio_available: true, audio_reason: "input_has_audio" }, "hls-manifest");
          }
          setStatus("playing");
          setError("");
          everPlayed = true;
          notifyLiveRuntimeChanged();
          video.play().catch(() => {});
          startDimensionProbe("hls-manifest-probe");
        });

        hls.on(Hls.Events.LEVEL_LOADED, (_event, data) => {
          const level = hls.levels?.[data?.level];
          if (level?.width && level?.height) {
            applyNaturalResolution(level.width, level.height, "hls-level");
          }
          sampleVideoDimensions("hls-level");
        });

        hls.on(Hls.Events.ERROR, async (_event, data) => {
          if (cancelled) return;
          const statusCode = Number(data?.response?.code || data?.networkDetails?.status || 0);
          if (data?.type === Hls.ErrorTypes.NETWORK_ERROR && (statusCode === 401 || statusCode === 403)) {
            if (authRefreshFailures >= 3) {
              failWithMessage(TEXT.failedPlay);
              return;
            }
            authRefreshFailures += 1;
            const refreshed = await refreshPlaylistSource();
            if (!cancelled && !refreshed) {
              failWithMessage(liveFailureMessage(null, everPlayed), lastTokenError);
            } else if (!cancelled && hls === hlsRef.current) {
              hls.startLoad();
            }
            return;
          }
          if (cancelled || !data?.fatal) return;
          failWithMessage(liveFailureMessage(null, everPlayed));
        });

        hls.attachMedia(video);
        return;
      }

      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        let nativeRefreshFailures = 0;
        video.src = playlist.url;

        const onLoaded = () => {
          if (cancelled) return;
          sampleVideoDimensions("native-hls");
          setStatus("playing");
          setError("");
          everPlayed = true;
          notifyLiveRuntimeChanged();
          nativeRefreshFailures = 0;
          video.play().catch(() => {});
          startDimensionProbe("native-hls-probe");
        };

        const onError = async () => {
          if (cancelled) return;
          if (nativeRefreshFailures >= 3) {
            failWithMessage(TEXT.failedPlay);
            return;
          }
          nativeRefreshFailures += 1;
          failWithMessage(liveFailureMessage(null, everPlayed));
        };

        video.addEventListener("loadedmetadata", onLoaded);
        video.addEventListener("error", onError);
        nativeHlsCleanupRef.current = () => {
          video.removeEventListener("loadedmetadata", onLoaded);
          video.removeEventListener("error", onError);
        };
        video.load();
        return;
      }

      setStatus("error");
      setError(TEXT.browserUnsupported);
      void closeViewer();
    }

    async function startPlayback() {
      if (cancelled || starting || !sourceKey.cameraId || !sourceKey.stream) return;
      starting = true;
      const expectedGeneration = ++generation;

      setStatus("loading");
      setError("");

      try {
        await closeViewer();
        if (cancelled || generation !== expectedGeneration) return;

        const viewer = await apiFetch("/live/viewers", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            camera_id: sourceKey.cameraId,
            stream: sourceKey.stream,
          }),
        });
        currentViewerId = viewer?.viewer_id || null;
        startViewerHeartbeat();

        if (cancelled) {
          await closeViewer();
          return;
        }

        const readyState = await waitForReady(expectedGeneration);
        if (cancelled || generation !== expectedGeneration) return;
        if (!readyState.ready) {
          if (readyState.failed) {
            failWithMessage(readyState.message || TEXT.failedStart);
            return;
          }
          failWithMessage(TEXT.failedStart);
          return;
        }

        const src = await buildPlaylistUrl();
        if (!cancelled && generation === expectedGeneration) {
          await attachPlayer(src);
        }
      } catch (err) {
        if (!cancelled) {
          const message = extractErrorMessage(err, TEXT.failedStart);
          if (String(message).includes("Live stream is not ready") || String(message).includes("process_exit")) {
            failWithMessage(message);
            return;
          }
          failWithMessage(message, err);
        }
      } finally {
        starting = false;
      }
    }

    const watchTimer = setInterval(() => {
      const video = videoRef.current;
      if (cancelled || !video || !hlsRef.current && !nativeHlsCleanupRef.current) return;
      const now = Date.now();
      if (video.currentTime > lastPlaybackTime + 0.01) {
        lastPlaybackTime = video.currentTime;
        lastPlaybackProgressAt = now;
        healthySince ||= now;
        if (now - healthySince >= 30000) recoveryAttempt = 0;
      } else if (!video.paused && now - lastPlaybackProgressAt > 30000) {
        failWithMessage(liveFailureMessage("stream_stalled", everPlayed));
      }
    }, 10000);
    const onResume = () => {
      if (!cancelled && !document.hidden && (hlsRef.current || nativeHlsCleanupRef.current)) {
        void refreshPlaylistSource().then((ok) => { if (!ok) failWithMessage(TEXT.failedPlay, lastTokenError); });
      }
    };
    window.addEventListener("online", onResume);
    document.addEventListener("visibilitychange", onResume);
    startPlayback();

    return () => {
      cancelled = true;
      generation += 1;
      clearInterval(watchTimer);
      window.removeEventListener("online", onResume);
      document.removeEventListener("visibilitychange", onResume);
      destroyPlayer();
      closeViewer();
      clearDimensionProbe();
    };
  }, [cameraId, stream]);

  useEffect(() => {
    audioStatusChangeRef.current = onAudioStatusChange;
    audioPlaybackBlockedRef.current = onAudioPlaybackBlocked;
  }, [onAudioStatusChange, onAudioPlaybackBlocked]);

  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !audioEnabled;
    if (!audioEnabled) {
      return;
    }
    video.volume = 1;
    video.play().catch(() => {
      video.muted = true;
      if (typeof audioPlaybackBlockedRef.current === "function") {
        audioPlaybackBlockedRef.current();
      }
    });
  }, [audioEnabled, audioRequestId]);

  return (
    <div
      ref={wrapRef}
      className={`liveVideoWrap ${compactCanvasRequested ? "compactVideoCanvasRequested" : ""} ${nativeVideoSuppressed ? "compactVideoCanvasActive" : ""}`}
      title={text(TEXT.doubleClick)}
      data-render-context="live"
      data-playback-status={status}
      data-renderer={nativeVideoSuppressed ? "canvas" : "native"}
      data-render-mode={renderState.mode}
      data-quality-tier={renderState.qualityTier}
      data-downscale-ratio={renderState.ratio == null ? "" : renderState.ratio.toFixed(4)}
      data-rendered-rect={`${viewerRect.width}x${viewerRect.height}`}
      data-decoded-resolution={`${naturalResolution.width}x${naturalResolution.height}`}
      data-source-resolution={`${naturalResolution.width}x${naturalResolution.height}`}
      data-source-compact-smoothing={sourceEligibleForCompactSmoothing ? "true" : "false"}
      data-ready-state={readyState}
      data-dimension-source={naturalResolution.source || "missing"}
      data-canvas-ready={nativeVideoSuppressed ? "true" : "false"}
      data-first-frame-drawn={canvasFrame.ready && canvasFrame.generation === canvasGeneration ? "true" : "false"}
      data-canvas-draw-error={canvasFrame.error || ""}
      data-canvas-generation={canvasGeneration}
      data-fullscreen={isFullscreen ? "true" : "false"}
      data-audio-enabled={audioEnabled ? "true" : "false"}
    >
      <VideoZoomPanSurface
        className="liveVideoZoomSurface"
        context="live"
        sourceKey={`${cameraId || ""}:${stream || ""}`}
        onDesktopDoubleClick={onDesktopDoubleClick}
        onTouchDoubleTap={onTouchDoubleTap}
        onZoomActiveChange={setZoomActive}
      >
        <video
          ref={videoRef}
          className={`liveVideo ${nativeVideoSuppressed ? "nativeVideoSuppressed" : ""}`}
          muted={!audioEnabled}
          autoPlay
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
          className="liveCompactVideoCanvas"
        />
      </VideoZoomPanSurface>

      {status === "loading" || status === "waiting" ? (
        <div className="liveCenterHint">
          {text(status === "waiting" ? error || TEXT.waiting : TEXT.loading)}
        </div>
      ) : null}

      {status === "error" ? (
        <div className="liveCenterHint liveCenterHintError">{text(error)}</div>
      ) : null}
    </div>
  );
}
