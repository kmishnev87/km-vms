"use client";

import { useEffect, useRef, useState } from "react";

export default function ArchiveTilePlayer({
  playback,
  speed,
  isPlaying,
}) {
  const wrapRef = useRef(null);
  const videoRef = useRef(null);
  const [status, setStatus] = useState("idle");

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
    const video = videoRef.current;
    if (!video) return;

    const hasVideo = Boolean(playback?.hasVideo);
    const cameraId = playback?.cameraId;
    const relPath = playback?.relPath;
    const offsetSec = Number(playback?.offsetSec || 0);

    const hardReset = () => {
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

    if (!hasVideo || !cameraId || !relPath) {
      hardReset();
      setStatus("empty");
      return;
    }

    const token =
      typeof window !== "undefined" ? localStorage.getItem("token") : null;

    if (!token) {
      hardReset();
      setStatus("error");
      return;
    }

    const src =
      `/api/chronology/file?camera_id=${cameraId}` +
      `&rel_path=${encodeURIComponent(relPath)}` +
      `&token=${encodeURIComponent(token)}`;

    setStatus("loading");

    const handleLoaded = () => {
      try {
        video.currentTime = Math.max(0, offsetSec);
      } catch (_) {}

      try {
        video.playbackRate = Number(speed || 1);
      } catch (_) {}

      if (isPlaying) {
        video.play().catch(() => {});
        setStatus("playing");
      } else {
        try {
          video.pause();
        } catch (_) {}
        setStatus("ready");
      }
    };

    const handleError = () => {
      hardReset();
      setStatus("error");
    };

    hardReset();

    video.addEventListener("loadedmetadata", handleLoaded, { once: true });
    video.addEventListener("error", handleError, { once: true });

    try {
      video.src = src;
      video.load();
    } catch (_) {
      hardReset();
      setStatus("error");
    }

    return () => {
      video.removeEventListener("loadedmetadata", handleLoaded);
      video.removeEventListener("error", handleError);
    };
  }, [playback?.playbackKey]);

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
      className="archiveVideoWrap"
      onDoubleClick={toggleFullscreen}
      title="Двойной клик — на весь экран"
    >
      <video
        key={playback?.playbackKey || "empty"}
        ref={videoRef}
        className="archiveVideo"
        muted
        autoPlay={false}
        playsInline
        controls={false}
      />

      {status === "loading" ? (
        <div className="archiveCenterHint">Ищем и подгружаем архив...</div>
      ) : null}

      {status === "empty" ? (
        <div className="archiveCenterHint">Видео отсутствует</div>
      ) : null}

      {status === "error" ? (
        <div className="archiveCenterHint archiveCenterHintError">
          Не удалось загрузить архив
        </div>
      ) : null}
    </div>
  );
}
