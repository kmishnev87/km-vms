"use client";

import { useEffect, useRef, useState } from "react";
import { LIVE_VIEW_WIDTH, LIVE_VIEW_HEIGHT, liveGeometryMatches, readViewReference, viewMatchScore, viewStability } from "../lib/aiLiveView";

export default function LiveAiOverlay({ overlay, videoRef, onStatus }) {
  const [frame, setFrame] = useState(null);
  const statusRef = useRef(onStatus);
  statusRef.current = onStatus;
  const ratio = overlay.view?.ratio, edges = overlay.view?.edges, mode = overlay.view?.mode;
  useEffect(() => {
    const reference = readViewReference({ ratio, edges, mode }), video = videoRef.current;
    let state = { good: 0, bad: 0, visible: false }, lastTime = -1, freshAt = 0, status = null;
    function publish(next) { if (status !== next) { status = next; statusRef.current?.(next); } }
    function hide(next = "waiting") {
      setFrame(null);
      state = { good: 0, bad: 0, visible: false };
      publish(next);
    }
    hide(!reference ? "uncalibrated" : reference.mode === "unknown" ? "profile" : "waiting");
    if (!reference || !video || reference.mode === "unknown") return;
    const fixed = reference.mode === "fixed";
    const scale = 4;
    const tiny = fixed ? null : document.createElement("canvas");
    let ctx, luma;
    if (tiny) {
      tiny.width = LIVE_VIEW_WIDTH * scale; tiny.height = LIVE_VIEW_HEIGHT * scale;
      ctx = tiny.getContext("2d", { alpha: false, willReadFrequently: true });
      if (!ctx) return;
      ctx.imageSmoothingQuality = "high";
      luma = new Uint8Array(LIVE_VIEW_WIDTH * LIVE_VIEW_HEIGHT);
    }
    function sample() {
      if (document.hidden || video.readyState < 2 || video.paused || !video.videoWidth || !video.videoHeight) {
        hide(); return;
      }
      if (video.currentTime === lastTime) {
        if (performance.now() - freshAt > 1700) hide();
        return; // Do not count repeated copies of a frozen frame.
      }
      lastTime = video.currentTime;
      freshAt = performance.now();
      try {
        if (fixed) {
          if (!liveGeometryMatches(reference, video.videoWidth, video.videoHeight)) {
            hide("mismatch"); return;
          }
        } else {
          ctx.drawImage(video, 0, 0, tiny.width, tiny.height);
          const rgba = ctx.getImageData(0, 0, tiny.width, tiny.height).data;
          for (let y = 0; y < LIVE_VIEW_HEIGHT; y++) for (let x = 0; x < LIVE_VIEW_WIDTH; x++) {
            let sum = 0;
            for (let dy = 0; dy < scale; dy++) for (let dx = 0; dx < scale; dx++) {
              const i = ((scale*y+dy)*tiny.width + scale*x+dx)*4;
              sum += 77*rgba[i] + 150*rgba[i+1] + 29*rgba[i+2];
            }
            luma[y*LIVE_VIEW_WIDTH+x] = (sum + 2048) >> 12;
          }
          state = viewStability(state, viewMatchScore(reference, luma, video.videoWidth, video.videoHeight));
          if (!state.visible) {
            setFrame(null);
            publish(state.bad >= 2 ? "mismatch" : status === "mismatch" ? "mismatch" : "waiting");
            return;
          }
        }
        publish("visible");
        const w = video.videoWidth, h = video.videoHeight;
        const crop = getComputedStyle(video).objectFit === "cover";
        setFrame(old => old?.w === w && old?.h === h && old?.crop === crop ? old : { w, h, crop });
      } catch { hide(); }
    }
    sample();
    const timer = window.setInterval(sample, 420);
    const reset = () => { lastTime = -1; hide(); };
    const resetEvents = ["emptied", "pause", "resize"];
    resetEvents.forEach(event => video.addEventListener(event, reset));
    return () => {
      window.clearInterval(timer);
      resetEvents.forEach(event => video.removeEventListener(event, reset));
    };
  }, [ratio, edges, mode, videoRef]);
  if (!frame) return null;
  return <svg className="liveAiContours" aria-hidden="true" data-live-ai-contours="true"
    viewBox={`0 0 ${frame.w} ${frame.h}`} preserveAspectRatio={frame.crop ? "xMidYMid slice" : "xMidYMid meet"}>
    {overlay.shapes.map((shape, i) => {
      const Shape = shape.line ? "polyline" : "polygon";
      const points = shape.points.map(([x, y]) => `${x * frame.w / 1000},${y * frame.h / 1000}`).join(" ");
      return <Shape key={i} points={points} stroke={shape.mask ? "#edab76" : "#60dfef"}
        strokeWidth="1.6" vectorEffect="non-scaling-stroke" strokeLinejoin="round"
        strokeDasharray={shape.mask ? "5 3" : undefined}
        fill={shape.line ? "none" : shape.mask ? "#edab7630" : "#60dfef22"} />;
    })}
  </svg>;
}
