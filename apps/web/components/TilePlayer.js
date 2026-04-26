"use client";

import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { apiFetch } from "../lib/api";

const MAX_RETRIES = 4;
const READY_POLL_INTERVAL_MS = 700;
const READY_TIMEOUT_MS = 10000;
const VIEWER_TOUCH_INTERVAL_MS = 15000;

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
    if (detail?.debug?.failure_reason) return detail.debug.failure_reason;
    if (detail?.debug?.last_error) return detail.debug.last_error;
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
  const touchTimerRef = useRef(null);
  const attemptRef = useRef(0);
  const viewerIdRef = useRef(null);
  const fallbackRequestedRef = useRef(false);

  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");

  function destroyPlayer() {
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
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
      while (!cancelled && Date.now() < deadline) {
        try {
          const response = await apiFetch(
            `/live/status?camera_id=${encodeURIComponent(sourceKey.cameraId)}&stream=${encodeURIComponent(sourceKey.stream)}`
          );
          const item = response?.items?.[0];
          if (item?.running && item?.ready) {
            return true;
          }
        } catch (_) {}

        setStatus("waiting");
        await sleep(READY_POLL_INTERVAL_MS);
      }

      return false;
    }

    function scheduleRetry(message) {
      if (cancelled) return;

      if (attemptRef.current >= MAX_RETRIES) {
        setStatus("error");
        setError(message);
        return;
      }

      const delay = 1200 + attemptRef.current * 1000;
      attemptRef.current += 1;
      setStatus("loading");
      setError("");
      destroyPlayer();
      retryTimerRef.current = setTimeout(() => {
        startPlayback();
      }, delay);
    }

    function failWithMessage(message) {
      destroyPlayer();
      setStatus("error");
      setError(message || TEXT.failedStart);
    }

    async function requestFallbackAndRetry(message) {
      if (cancelled) return;

      if (fallbackRequestedRef.current) {
        scheduleRetry(message);
        return;
      }

      fallbackRequestedRef.current = true;
      setStatus("waiting");
      setError("");

      try {
        await apiFetch("/live/fallback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            camera_id: sourceKey.cameraId,
            stream: sourceKey.stream,
            reason: "hls_fatal_error",
          }),
        });

        if (cancelled) return;

        const ready = await waitForReady();
        if (!ready) {
          scheduleRetry(message);
          return;
        }

        const token =
          typeof window !== "undefined" ? localStorage.getItem("token") : null;
        if (!token) {
          setStatus("error");
          setError(TEXT.noToken);
          return;
        }

        const src = `/api/live/${sourceKey.cameraId}/${sourceKey.stream}/index.m3u8?token=${encodeURIComponent(token)}&fallback=${Date.now()}`;
        await attachPlayer(src);
      } catch (_) {
        scheduleRetry(message);
      }
    }

    async function attachPlayer(src) {
      const video = videoRef.current;
      if (!video) return;

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
            hls.loadSource(src);
          }
        });

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          if (cancelled) return;
          setStatus("playing");
          setError("");
          attemptRef.current = 0;
          video.play().catch(() => {});
        });

        hls.on(Hls.Events.ERROR, (_event, data) => {
          if (cancelled || !data?.fatal) return;
          requestFallbackAndRetry(TEXT.failedPlay);
        });

        hls.attachMedia(video);
        return;
      }

      if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = src;

        const onLoaded = () => {
          if (cancelled) return;
          setStatus("playing");
          setError("");
          attemptRef.current = 0;
          video.play().catch(() => {});
        };

        const onError = () => {
          if (!cancelled) {
            requestFallbackAndRetry(TEXT.failedPlay);
          }
        };

        video.addEventListener("loadedmetadata", onLoaded, { once: true });
        video.addEventListener("error", onError, { once: true });
        video.load();
        return;
      }

      setStatus("error");
      setError(TEXT.browserUnsupported);
    }

    async function startPlayback() {
      if (!sourceKey.cameraId || !sourceKey.stream) return;

      const token =
        typeof window !== "undefined" ? localStorage.getItem("token") : null;

      if (!token) {
        setStatus("error");
        setError(TEXT.noToken);
        return;
      }

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

        const ready = await waitForReady();
        if (!ready) {
          scheduleRetry(TEXT.failedStart);
          return;
        }

        const src = `/api/live/${sourceKey.cameraId}/${sourceKey.stream}/index.m3u8?token=${encodeURIComponent(token)}`;
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
          scheduleRetry(message);
        }
      }
    }

    attemptRef.current = 0;
    fallbackRequestedRef.current = false;
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
