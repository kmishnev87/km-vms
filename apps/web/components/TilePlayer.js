"use client";

import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { apiFetch } from "../lib/api";

const MAX_RETRIES = 6;

export default function TilePlayer({ cameraId, stream }) {
  const videoRef = useRef(null);
  const wrapRef = useRef(null);
  const hlsRef = useRef(null);
  const retryTimerRef = useRef(null);
  const attemptRef = useRef(0);

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

    async function startPlayback() {
      if (!cameraId || !stream) return;

      const token =
        typeof window !== "undefined" ? localStorage.getItem("token") : null;

      if (!token) {
        setStatus("error");
        setError("Нет токена авторизации");
        return;
      }

      setStatus("loading");
      setError("");

      try {
        await apiFetch("/live/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            camera_id: Number(cameraId),
            stream,
          }),
        });

        if (cancelled) return;

        const src = `/api/live/${cameraId}/${stream}/index.m3u8?token=${encodeURIComponent(token)}`;
        const video = videoRef.current;
        if (!video) return;

        destroyPlayer();

        if (Hls.isSupported()) {
          const hls = new Hls({
            lowLatencyMode: true,
            backBufferLength: 20,
            maxBufferLength: 8,
            liveSyncDurationCount: 2,
            liveMaxLatencyDurationCount: 4,
            manifestLoadingTimeOut: 10000,
            levelLoadingTimeOut: 10000,
            fragLoadingTimeOut: 15000,
          });

          hlsRef.current = hls;

          hls.on(Hls.Events.MEDIA_ATTACHED, () => {
            hls.loadSource(src);
          });

          hls.on(Hls.Events.MANIFEST_PARSED, () => {
            if (cancelled) return;
            setStatus("playing");
            setError("");
            attemptRef.current = 0;
            video.play().catch(() => {});
          });

          hls.on(Hls.Events.ERROR, (_event, data) => {
            if (cancelled) return;

            if (data?.fatal) {
              if (attemptRef.current < MAX_RETRIES) {
                const delay = 1200 + attemptRef.current * 800;
                attemptRef.current += 1;
                setStatus("loading");
                setError("");
                destroyPlayer();
                retryTimerRef.current = setTimeout(() => {
                  startPlayback();
                }, delay);
              } else {
                setStatus("error");
                setError("Не удалось воспроизвести поток");
              }
            }
          });

          hls.attachMedia(video);
        } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = src;

          const onLoaded = () => {
            if (cancelled) return;
            setStatus("playing");
            setError("");
            attemptRef.current = 0;
            video.play().catch(() => {});
          };

          const onError = () => {
            if (cancelled) return;

            if (attemptRef.current < MAX_RETRIES) {
              const delay = 1200 + attemptRef.current * 800;
              attemptRef.current += 1;
              setStatus("loading");
              setError("");
              destroyPlayer();
              retryTimerRef.current = setTimeout(() => {
                startPlayback();
              }, delay);
            } else {
              setStatus("error");
              setError("Не удалось воспроизвести поток");
            }
          };

          video.addEventListener("loadedmetadata", onLoaded, { once: true });
          video.addEventListener("error", onError, { once: true });
        } else {
          setStatus("error");
          setError("Браузер не поддерживает HLS");
        }
      } catch (_) {
        if (cancelled) return;

        if (attemptRef.current < MAX_RETRIES) {
          const delay = 1200 + attemptRef.current * 800;
          attemptRef.current += 1;
          setStatus("loading");
          setError("");
          destroyPlayer();
          retryTimerRef.current = setTimeout(() => {
            startPlayback();
          }, delay);
        } else {
          setStatus("error");
          setError("Не удалось запустить поток");
        }
      }
    }

    attemptRef.current = 0;
    startPlayback();

    return () => {
      cancelled = true;
      destroyPlayer();
    };
  }, [cameraId, stream]);

  return (
    <div
      ref={wrapRef}
      className="liveVideoWrap"
      onDoubleClick={toggleFullscreen}
      title="Двойной клик — на весь экран"
    >
      <video
        ref={videoRef}
        className="liveVideo"
        muted
        autoPlay
        playsInline
        controls={false}
      />

      {status === "loading" ? (
        <div className="liveCenterHint">Подключаем поток...</div>
      ) : null}

      {status === "error" ? (
        <div className="liveCenterHint liveCenterHintError">{error}</div>
      ) : null}
    </div>
  );
}
