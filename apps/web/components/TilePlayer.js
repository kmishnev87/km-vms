"use client";

import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { apiFetch, issueLiveMediaTokenInfo } from "../lib/api";

const READY_POLL_INTERVAL_MS = 700;
const READY_TIMEOUT_MS = 210000;
const VIEWER_TOUCH_INTERVAL_MS = 15000;
const MEDIA_TOKEN_REFRESH_SAFETY_MS = 45000;
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

export default function TilePlayer({ cameraId, stream }) {
  const videoRef = useRef(null);
  const wrapRef = useRef(null);
  const hlsRef = useRef(null);
  const retryTimerRef = useRef(null);
  const mediaTokenRefreshTimerRef = useRef(null);
  const mediaTokenRefreshInFlightRef = useRef(null);
  const nativeHlsCleanupRef = useRef(null);
  const touchTimerRef = useRef(null);
  const viewerIdRef = useRef(null);

  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");

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

    const video = videoRef.current;
    if (video) {
      try {
        video.pause();
        video.removeAttribute("src");
        video.load();
      } catch (_) {}
    }
  }

  function toggleFullscreen() {
    const el = wrapRef.current;
    if (!el) return;

    if (!document.fullscreenElement) {
      el.requestFullscreen?.().catch(() => {});
    } else {
      document.exitFullscreen?.().catch(() => {});
    }
  }

  useEffect(() => {
    let cancelled = false;
    const sourceKey = {
      cameraId: cameraId ? Number(cameraId) : null,
      stream: stream || null,
    };

    async function closeViewer() {
      if (touchTimerRef.current) {
        clearInterval(touchTimerRef.current);
        touchTimerRef.current = null;
      }

      const viewerId = viewerIdRef.current;
      viewerIdRef.current = null;
      if (!viewerId) return;

      try {
        await apiFetch(`/live/viewers/${encodeURIComponent(viewerId)}`, {
          method: "DELETE",
        });
      } catch (_) {}
    }

    function startViewerHeartbeat() {
      if (touchTimerRef.current) {
        clearInterval(touchTimerRef.current);
        touchTimerRef.current = null;
      }

      touchTimerRef.current = setInterval(() => {
        const viewerId = viewerIdRef.current;
        if (!viewerId || cancelled) return;

        apiFetch(`/live/viewers/${encodeURIComponent(viewerId)}/touch`, {
          method: "POST",
        }).catch(() => {});
      }, VIEWER_TOUCH_INTERVAL_MS);
    }

    async function waitForReady() {
      const deadline = Date.now() + READY_TIMEOUT_MS;
      let lastItem = null;
      while (!cancelled && Date.now() < deadline) {
        try {
          const response = await apiFetch(
            `/live/status?camera_id=${encodeURIComponent(sourceKey.cameraId)}&stream=${encodeURIComponent(sourceKey.stream)}`
          );
          const item = response?.items?.[0];
          if (item) lastItem = item;
          if (item?.running && item?.ready) {
            return { ready: true, item };
          }
          if (
            item?.status === "failed" ||
            item?.failure_reason ||
            item?.safe_failure_reason
          ) {
            return {
              ready: false,
              failed: true,
              message: item?.safe_failure_reason || item?.failure_reason || TEXT.failedStart,
              item,
            };
          }
        } catch (_) {}

        setStatus("waiting");
        await sleep(READY_POLL_INTERVAL_MS);
      }

      return { ready: false, failed: false, message: TEXT.failedStart, item: lastItem };
    }

    function failWithMessage(message) {
      destroyPlayer();
      setStatus("error");
      setError(message || TEXT.failedStart);
    }

    function isStillStarting(readyState) {
      return Boolean(
        readyState?.item?.running &&
          (readyState.item.status === "starting" || readyState.item.status === "restarting")
      );
    }

    async function buildPlaylistUrl() {
      const tokenInfo = await issueLiveMediaTokenInfo(sourceKey.cameraId, sourceKey.stream);
      return {
        url: `/api/live/${sourceKey.cameraId}/${sourceKey.stream}/index.m3u8?media_token=${encodeURIComponent(tokenInfo.mediaToken)}`,
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

      mediaTokenRefreshInFlightRef.current = (async () => {
        const next = await buildPlaylistUrl();
        if (cancelled) return;
        if (hlsRef.current) {
          hlsRef.current.loadSource(next.url);
        } else if (videoRef.current) {
          const video = videoRef.current;
          const wasPaused = video.paused;
          video.src = next.url;
          video.load();
          if (!wasPaused) video.play().catch(() => {});
        }
        scheduleMediaTokenRefresh(next.expiresIn);
      })();

      try {
        await mediaTokenRefreshInFlightRef.current;
        return true;
      } catch (_) {
        return false;
      } finally {
        if (mediaTokenRefreshInFlightRef.current) {
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
          failWithMessage(TEXT.failedPlay);
        }
      }, delay);
    }

    function continueWaitingForReady(message) {
      if (cancelled) return;

      setStatus("waiting");
      setError("");
      retryTimerRef.current = setTimeout(async () => {
        if (cancelled) return;

        const readyState = await waitForReady();
        if (readyState.ready) {
          await attachPlayer(await buildPlaylistUrl());
          return;
        }

        if (readyState.failed) {
          failWithMessage(readyState.message || message);
          return;
        }

        if (isStillStarting(readyState)) {
          continueWaitingForReady(message);
          return;
        }

        failWithMessage(message);
      }, READY_POLL_INTERVAL_MS);
    }

    async function attachPlayer(playlist) {
      const video = videoRef.current;
      if (!video) return;
      let authRefreshFailures = 0;

      destroyPlayer();

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

        hls.on(Hls.Events.MEDIA_ATTACHED, () => {
          if (!cancelled) {
            hls.loadSource(playlist.url);
            scheduleMediaTokenRefresh(playlist.expiresIn);
          }
        });

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          if (cancelled) return;
          authRefreshFailures = 0;
          setStatus("playing");
          setError("");
          video.play().catch(() => {});
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
              failWithMessage(TEXT.failedPlay);
            }
            return;
          }
          if (cancelled || !data?.fatal) return;
          failWithMessage(TEXT.failedPlay);
        });

        hls.attachMedia(video);
        return;
      }

      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        let nativeRefreshFailures = 0;
        video.src = playlist.url;
        scheduleMediaTokenRefresh(playlist.expiresIn);

        const onLoaded = () => {
          if (cancelled) return;
          setStatus("playing");
          setError("");
          nativeRefreshFailures = 0;
          video.play().catch(() => {});
        };

        const onError = async () => {
          if (cancelled) return;
          if (nativeRefreshFailures >= 3) {
            failWithMessage(TEXT.failedPlay);
            return;
          }
          nativeRefreshFailures += 1;
          const refreshed = await refreshPlaylistSource();
          if (!cancelled && !refreshed) {
            failWithMessage(TEXT.failedPlay);
          }
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
    }

    async function startPlayback() {
      if (!sourceKey.cameraId || !sourceKey.stream) return;

      setStatus("loading");
      setError("");

      try {
        await closeViewer();

        const viewer = await apiFetch("/live/viewers", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            camera_id: sourceKey.cameraId,
            stream: sourceKey.stream,
          }),
        });
        viewerIdRef.current = viewer?.viewer_id || null;
        startViewerHeartbeat();

        if (cancelled) {
          await closeViewer();
          return;
        }

        const readyState = await waitForReady();
        if (!readyState.ready) {
          if (readyState.failed) {
            failWithMessage(readyState.message || TEXT.failedStart);
            return;
          }
          if (isStillStarting(readyState)) {
            continueWaitingForReady(TEXT.failedStart);
            return;
          }
          failWithMessage(TEXT.failedStart);
          return;
        }

        const src = await buildPlaylistUrl();
        if (!cancelled) {
          await attachPlayer(src);
        }
      } catch (err) {
        if (!cancelled) {
          const message = extractErrorMessage(err, TEXT.failedStart);
          if (String(message).includes("Live stream is not ready") || String(message).includes("process_exit")) {
            failWithMessage(message);
            return;
          }
          failWithMessage(message);
        }
      }
    }

    startPlayback();

    return () => {
      cancelled = true;
      destroyPlayer();
      closeViewer();
    };
  }, [cameraId, stream]);

  return (
    <div
      ref={wrapRef}
      className="liveVideoWrap"
      onDoubleClick={toggleFullscreen}
      title={TEXT.doubleClick}
    >
      <video
        ref={videoRef}
        className="liveVideo"
        muted
        autoPlay
        playsInline
        controls={false}
      />

      {status === "loading" || status === "waiting" ? (
        <div className="liveCenterHint">
          {status === "waiting" ? TEXT.waiting : TEXT.loading}
        </div>
      ) : null}

      {status === "error" ? (
        <div className="liveCenterHint liveCenterHintError">{error}</div>
      ) : null}
    </div>
  );
}
