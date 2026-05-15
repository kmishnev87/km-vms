"use client";

import { useEffect, useRef } from "react";
import { planCompactVideoDownscale } from "../lib/playbackResolution";

function syncCanvasElementSize(canvas, width, height) {
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function setHighQualitySmoothing(ctx) {
  if (!ctx) return;
  ctx.imageSmoothingEnabled = true;
  try {
    ctx.imageSmoothingQuality = "high";
  } catch (_) {}
}

export default function CompactVideoCanvas({
  videoRef,
  active,
  mode,
  ratio,
  backingScale = 1,
  generation,
  className = "",
  onFrameState,
}) {
  const canvasRef = useRef(null);
  const scratchRef = useRef([]);
  const planRef = useRef(null);
  const readyRef = useRef(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    const video = videoRef?.current;
    readyRef.current = false;
    onFrameState?.({
      ready: false,
      generation,
      reason: active ? "activating" : "inactive",
      error: "",
    });

    if (!active || !canvas || !video) return undefined;

    const ctx = canvas.getContext("2d", { alpha: false }) || canvas.getContext("2d");
    if (!ctx) {
      onFrameState?.({ ready: false, generation, reason: "no-context", error: "no-context" });
      return undefined;
    }

    let cancelled = false;
    let rafId = 0;
    let videoFrameId = 0;
    let loopRunning = false;
    scratchRef.current = [document.createElement("canvas"), document.createElement("canvas")];

    function report(next) {
      if (cancelled) return;
      onFrameState?.({ generation, ...next });
    }

    function getScratch(index, width, height) {
      const scratch = scratchRef.current[index];
      if (!scratch) return null;
      syncCanvasElementSize(scratch, width, height);
      return scratch;
    }

    function setCanvasFacts(plan, state = {}) {
      planRef.current = plan;
      canvas.dataset.compactPassCount = String(plan.passCount || 0);
      canvas.dataset.compactBackingScale = String(plan.backingScale || 1);
      canvas.dataset.compactQualityPath = plan.qualityPath || "";
      canvas.dataset.compactCanvasSize = `${canvas.width}x${canvas.height}`;
      canvas.dataset.compactTargetSize = `${plan.target?.width || 0}x${plan.target?.height || 0}`;
      canvas.dataset.compactGeneration = String(generation || "");
      canvas.dataset.compactReady = state.ready ? "true" : "false";
      canvas.dataset.compactDrawError = state.error || "";
    }

    function drawWithPlan(plan) {
      if (!plan.passes.length) return false;

      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, plan.canvas.width, plan.canvas.height);
      setHighQualitySmoothing(ctx);

      let currentSource = video;
      let currentWidth = video.videoWidth || 0;
      let currentHeight = video.videoHeight || 0;

      for (let index = 0; index < plan.passes.length; index += 1) {
        const pass = plan.passes[index];
        const isFinal = index === plan.passes.length - 1;
        const targetCanvas = isFinal ? canvas : getScratch(index % 2, pass.width, pass.height);
        if (!targetCanvas) return false;

        const targetCtx = isFinal
          ? ctx
          : targetCanvas.getContext("2d", { alpha: false }) || targetCanvas.getContext("2d");
        if (!targetCtx) return false;
        setHighQualitySmoothing(targetCtx);
        targetCtx.clearRect(0, 0, targetCanvas.width, targetCanvas.height);

        if (isFinal) {
          targetCtx.drawImage(
            currentSource,
            0,
            0,
            currentWidth,
            currentHeight,
            plan.target.dx,
            plan.target.dy,
            plan.target.width,
            plan.target.height
          );
        } else {
          targetCtx.drawImage(currentSource, 0, 0, currentWidth, currentHeight, 0, 0, pass.width, pass.height);
          currentSource = targetCanvas;
          currentWidth = pass.width;
          currentHeight = pass.height;
        }
      }
      return true;
    }

    function drawFrame() {
      if (cancelled) return;

      const rect = canvas.getBoundingClientRect();
      const sourceWidth = video.videoWidth || 0;
      const sourceHeight = video.videoHeight || 0;
      const cssWidth = Math.round(rect.width || 0);
      const cssHeight = Math.round(rect.height || 0);
      const plan = planCompactVideoDownscale({
        sourceWidth,
        sourceHeight,
        cssWidth,
        cssHeight,
        mode,
        ratio,
        backingScale,
        devicePixelRatio: window.devicePixelRatio || 1,
      });

      syncCanvasElementSize(canvas, plan.canvas.width, plan.canvas.height);

      if (!sourceWidth || !sourceHeight || !cssWidth || !cssHeight || video.readyState < 2) {
        readyRef.current = false;
        setCanvasFacts(plan, { ready: false, error: "" });
        report({ ready: false, reason: "not-ready", error: "", plan });
        return;
      }

      try {
        const drawn = drawWithPlan(plan);
        readyRef.current = Boolean(drawn);
        setCanvasFacts(plan, { ready: drawn, error: drawn ? "" : "draw-failed" });
        report({
          ready: Boolean(drawn),
          reason: drawn ? "drawn" : "draw-failed",
          error: drawn ? "" : "draw-failed",
          plan,
        });
      } catch (error) {
        readyRef.current = false;
        setCanvasFacts(plan, { ready: false, error: "draw-exception" });
        report({
          ready: false,
          reason: "draw-exception",
          error: error?.name || "draw-exception",
          plan,
        });
      }
    }

    function scheduleFallback() {
      if (cancelled) return;
      drawFrame();
      if (!video.paused && !video.ended) {
        rafId = window.requestAnimationFrame(scheduleFallback);
      } else {
        loopRunning = false;
      }
    }

    function scheduleVideoFrame() {
      if (cancelled) return;
      drawFrame();
      if (!video.paused && !video.ended && typeof video.requestVideoFrameCallback === "function") {
        videoFrameId = video.requestVideoFrameCallback(scheduleVideoFrame);
      } else {
        loopRunning = false;
      }
    }

    function startLoop() {
      if (loopRunning) return;
      loopRunning = true;
      if (typeof video.requestVideoFrameCallback === "function") {
        videoFrameId = video.requestVideoFrameCallback(scheduleVideoFrame);
      } else {
        rafId = window.requestAnimationFrame(scheduleFallback);
      }
    }

    const handleStaticUpdate = () => drawFrame();
    const handlePlay = () => startLoop();

    video.addEventListener("play", handlePlay);
    video.addEventListener("playing", handlePlay);
    video.addEventListener("loadedmetadata", handleStaticUpdate);
    video.addEventListener("loadeddata", handleStaticUpdate);
    video.addEventListener("canplay", handleStaticUpdate);
    video.addEventListener("resize", handleStaticUpdate);
    video.addEventListener("pause", handleStaticUpdate);
    window.addEventListener("resize", handleStaticUpdate);

    drawFrame();
    if (!video.paused && !video.ended) startLoop();

    return () => {
      cancelled = true;
      readyRef.current = false;
      if (rafId) window.cancelAnimationFrame(rafId);
      if (videoFrameId && typeof video.cancelVideoFrameCallback === "function") {
        video.cancelVideoFrameCallback(videoFrameId);
      }
      scratchRef.current = [];
      video.removeEventListener("play", handlePlay);
      video.removeEventListener("playing", handlePlay);
      video.removeEventListener("loadedmetadata", handleStaticUpdate);
      video.removeEventListener("loadeddata", handleStaticUpdate);
      video.removeEventListener("canplay", handleStaticUpdate);
      video.removeEventListener("resize", handleStaticUpdate);
      video.removeEventListener("pause", handleStaticUpdate);
      window.removeEventListener("resize", handleStaticUpdate);
      onFrameState?.({
        ready: false,
        generation,
        reason: "cleanup",
        error: "",
      });
    };
  }, [active, mode, ratio, backingScale, generation, videoRef, onFrameState]);

  if (!active) return null;

  return (
    <canvas
      ref={canvasRef}
      className={`compactVideoCanvas ${className}`.trim()}
      aria-hidden="true"
      data-compact-canvas-mode={mode}
      data-compact-canvas-active={active ? "true" : "false"}
      data-compact-canvas-ready={readyRef.current ? "true" : "false"}
      data-compact-generation={String(generation || "")}
      data-compact-pass-count={planRef.current?.passCount || 0}
      data-compact-backing-scale={planRef.current?.backingScale || 1}
      data-compact-quality-path={planRef.current?.qualityPath || ""}
    />
  );
}
