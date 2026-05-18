"use client";

import { useEffect, useRef, useState } from "react";
import {
  DEFAULT_VIDEO_ZOOM_STATE,
  VIDEO_ZOOM_MAX,
  clampPan,
  distanceBetweenPoints,
  midpointBetweenPoints,
  panBy,
  zoomAtPoint,
  zoomFromPinch,
} from "../lib/videoZoomPanCore";

function pointFromPointer(event, element) {
  const rect = element?.getBoundingClientRect?.();
  return {
    x: Number(event.clientX || 0) - Number(rect?.left || 0),
    y: Number(event.clientY || 0) - Number(rect?.top || 0),
  };
}

function rectFor(element) {
  const rect = element?.getBoundingClientRect?.();
  return {
    width: Math.max(1, Number(rect?.width || 1)),
    height: Math.max(1, Number(rect?.height || 1)),
  };
}

function stopHandledGesture(event) {
  event.preventDefault();
  event.stopPropagation();
}

export default function VideoZoomPanSurface({
  children,
  className = "",
  sourceKey = "",
  context = "",
  maxZoom = VIDEO_ZOOM_MAX,
}) {
  const surfaceRef = useRef(null);
  const stateRef = useRef({ ...DEFAULT_VIDEO_ZOOM_STATE });
  const pointersRef = useRef(new Map());
  const gestureRef = useRef(null);
  const styledFullscreenMediaRef = useRef(null);
  const [fullscreenMedia, setFullscreenMedia] = useState(null);
  const [zoomState, setZoomState] = useState({ ...DEFAULT_VIDEO_ZOOM_STATE });

  function commitState(next) {
    stateRef.current = next;
    setZoomState(next);
  }

  useEffect(() => {
    pointersRef.current.clear();
    gestureRef.current = null;
    commitState({ ...DEFAULT_VIDEO_ZOOM_STATE });
  }, [sourceKey]);

  useEffect(() => {
    const handleResize = () => {
      const element = surfaceRef.current;
      if (!element) return;
      commitState(clampPan(stateRef.current, rectFor(element)));
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  useEffect(() => {
    const element = surfaceRef.current;
    if (!element) return undefined;
    function handleWheel(event) {
      stopHandledGesture(event);
      const factor = Math.exp(-Number(event.deltaY || 0) * 0.0015);
      const targetElement = gestureElement();
      commitState(zoomAtPoint(stateRef.current, rectFor(targetElement), pointFromPointer(event, targetElement), factor, maxZoom));
    }
    const targets = new Set([element]);
    if (fullscreenMedia) targets.add(fullscreenMedia);
    targets.forEach((target) => target?.addEventListener?.("wheel", handleWheel, { passive: false, capture: true }));
    return () => targets.forEach((target) => target?.removeEventListener?.("wheel", handleWheel, { capture: true }));
  }, [fullscreenMedia, maxZoom]);

  useEffect(() => {
    function handleFullscreenChange() {
      const surface = surfaceRef.current;
      const fullscreenElement = document.fullscreenElement;
      if (!surface || !fullscreenElement || !surface.contains(fullscreenElement)) {
        setFullscreenMedia(null);
        return;
      }
      if (fullscreenElement.matches?.("video, canvas")) {
        setFullscreenMedia(fullscreenElement);
        return;
      }
      setFullscreenMedia(null);
    }
    document.addEventListener("fullscreenchange", handleFullscreenChange);
    handleFullscreenChange();
    return () => document.removeEventListener("fullscreenchange", handleFullscreenChange);
  }, []);

  useEffect(() => {
    if (!fullscreenMedia) return undefined;
    fullscreenMedia.addEventListener("pointerdown", handlePointerDown, { capture: true });
    fullscreenMedia.addEventListener("pointermove", handlePointerMove, { capture: true });
    fullscreenMedia.addEventListener("pointerup", handlePointerEnd, { capture: true });
    fullscreenMedia.addEventListener("pointercancel", handlePointerEnd, { capture: true });
    return () => {
      fullscreenMedia.removeEventListener("pointerdown", handlePointerDown, { capture: true });
      fullscreenMedia.removeEventListener("pointermove", handlePointerMove, { capture: true });
      fullscreenMedia.removeEventListener("pointerup", handlePointerEnd, { capture: true });
      fullscreenMedia.removeEventListener("pointercancel", handlePointerEnd, { capture: true });
    };
  }, [fullscreenMedia]);

  function resetFullscreenMediaStyle(element) {
    if (!element) return;
    element.style.transform = "";
    element.style.transformOrigin = "";
    element.style.willChange = "";
  }

  function gestureElement() {
    return fullscreenMedia || surfaceRef.current;
  }

  function pointerData(event, element = gestureElement()) {
    const point = pointFromPointer(event, element);
    return {
      id: event.pointerId,
      pointerType: event.pointerType || "mouse",
      clientX: Number(event.clientX || 0),
      clientY: Number(event.clientY || 0),
      x: point.x,
      y: point.y,
    };
  }

  function startPan(event, pointer) {
    gestureRef.current = {
      mode: pointer.pointerType === "touch" ? "touchPan" : "mousePan",
      pointerId: pointer.id,
      lastX: pointer.clientX,
      lastY: pointer.clientY,
    };
    event.currentTarget?.setPointerCapture?.(event.pointerId);
    stopHandledGesture(event);
  }

  function startPinch(event) {
    const element = gestureElement();
    const points = Array.from(pointersRef.current.values()).filter((item) => item.pointerType === "touch");
    if (!element || points.length < 2) return;
    const [first, second] = points;
    gestureRef.current = {
      mode: "touchPinch",
      pointerIds: [first.id, second.id],
      startDistance: distanceBetweenPoints(first, second),
      startState: stateRef.current,
    };
    event.currentTarget?.setPointerCapture?.(event.pointerId);
    stopHandledGesture(event);
  }

  function handlePointerDown(event) {
    if (event.target?.closest?.("button, select, input, textarea, a")) return;
    const element = gestureElement();
    if (!element) return;
    const pointer = pointerData(event, element);
    pointersRef.current.set(event.pointerId, pointer);

    if (pointer.pointerType === "mouse") {
      if (event.button === 0 && stateRef.current.scale > 1) startPan(event, pointer);
      return;
    }

    const touchCount = Array.from(pointersRef.current.values()).filter((item) => item.pointerType === "touch").length;
    if (touchCount >= 2) {
      startPinch(event);
      return;
    }
    if (stateRef.current.scale > 1) {
      startPan(event, pointer);
    }
  }

  function handlePointerMove(event) {
    const element = gestureElement();
    if (!element || !pointersRef.current.has(event.pointerId)) return;
    const nextPointer = pointerData(event, element);
    pointersRef.current.set(event.pointerId, nextPointer);
    const gesture = gestureRef.current;
    if (!gesture) return;

    if (gesture.mode === "mousePan" || gesture.mode === "touchPan") {
      if (gesture.pointerId !== event.pointerId) return;
      const deltaX = nextPointer.clientX - gesture.lastX;
      const deltaY = nextPointer.clientY - gesture.lastY;
      gestureRef.current = { ...gesture, lastX: nextPointer.clientX, lastY: nextPointer.clientY };
      commitState(panBy(stateRef.current, rectFor(element), deltaX, deltaY));
      stopHandledGesture(event);
      return;
    }

    if (gesture.mode === "touchPinch") {
      const [firstId, secondId] = gesture.pointerIds;
      const first = pointersRef.current.get(firstId);
      const second = pointersRef.current.get(secondId);
      if (!first || !second) return;
      const midpoint = midpointBetweenPoints(first, second);
      const currentDistance = distanceBetweenPoints(first, second);
      commitState(zoomFromPinch(gesture.startState, rectFor(element), gesture.startDistance, currentDistance, midpoint, maxZoom));
      stopHandledGesture(event);
    }
  }

  function handlePointerEnd(event) {
    pointersRef.current.delete(event.pointerId);
    const gesture = gestureRef.current;
    if (!gesture) return;
    if (gesture.mode === "mousePan" && gesture.pointerId === event.pointerId) {
      gestureRef.current = null;
      return;
    }
    if (gesture.mode === "touchPan" && gesture.pointerId === event.pointerId) {
      gestureRef.current = null;
      return;
    }
    if (gesture.mode === "touchPinch" && gesture.pointerIds.includes(event.pointerId)) {
      gestureRef.current = null;
      const remainingTouch = Array.from(pointersRef.current.values()).find((item) => item.pointerType === "touch");
      if (remainingTouch && stateRef.current.scale > 1) {
        gestureRef.current = {
          mode: "touchPan",
          pointerId: remainingTouch.id,
          lastX: remainingTouch.clientX,
          lastY: remainingTouch.clientY,
        };
      }
    }
  }

  const zoomed = zoomState.scale > 1.001;
  const transform = `translate3d(${zoomState.panX}px, ${zoomState.panY}px, 0) scale(${zoomState.scale})`;

  useEffect(() => {
    const previous = styledFullscreenMediaRef.current;
    if (previous && previous !== fullscreenMedia) resetFullscreenMediaStyle(previous);
    if (!fullscreenMedia) {
      styledFullscreenMediaRef.current = null;
      return;
    }
    fullscreenMedia.style.transform = transform;
    fullscreenMedia.style.transformOrigin = "center center";
    fullscreenMedia.style.willChange = zoomed ? "transform" : "";
    styledFullscreenMediaRef.current = fullscreenMedia;
    return () => {
      if (styledFullscreenMediaRef.current === fullscreenMedia) resetFullscreenMediaStyle(fullscreenMedia);
    };
  }, [fullscreenMedia, transform, zoomed]);

  return (
    <div
      ref={surfaceRef}
      className={`videoZoomPanSurface ${zoomed ? "isZoomed" : ""} ${className}`.trim()}
      data-video-zoom-surface="true"
      data-video-zoom-context={context}
      data-video-zoom-scale={zoomState.scale.toFixed(3)}
      data-video-zoom-pan={`${Math.round(zoomState.panX)},${Math.round(zoomState.panY)}`}
      data-video-zoom-max={String(maxZoom)}
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerEnd}
      onPointerCancel={handlePointerEnd}
    >
      <div
        className="videoZoomPanContent"
        data-video-zoom-content="true"
        style={{ transform }}
      >
        {children}
      </div>
      {zoomed ? <div className="videoZoomPanIndicator" aria-hidden="true">{zoomState.scale.toFixed(1)}x</div> : null}
    </div>
  );
}
