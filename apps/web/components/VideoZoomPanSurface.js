"use client";

import { useEffect, useRef, useState } from "react";
import {
  DEFAULT_VIDEO_ZOOM_STATE,
  VIDEO_ZOOM_MAX,
  clampPan,
  containedMediaRect,
  consumeTouchDoubleTapSuppressionToken,
  createTouchDoubleTapSuppressionToken,
  createVideoTouchGestureState,
  distanceBetweenPoints,
  isTouchDoubleTap,
  midpointBetweenPoints,
  panBy,
  touchDoubleTapZone,
  transitionVideoTouchGesture,
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
  onDesktopDoubleClick,
  onTouchDoubleTap,
  onZoomActiveChange,
}) {
  const surfaceRef = useRef(null);
  const stateRef = useRef({ ...DEFAULT_VIDEO_ZOOM_STATE });
  const pointersRef = useRef(new Map());
  const gestureRef = useRef(null);
  const touchGestureRef = useRef(createVideoTouchGestureState());
  const lastTouchTapRef = useRef(null);
  const touchDoubleTapSuppressionRef = useRef(null);
  const ownedSecondTouchTapRef = useRef(null);
  const zoomActiveRef = useRef(false);
  const onZoomActiveChangeRef = useRef(onZoomActiveChange);
  const styledFullscreenMediaRef = useRef(null);
  const [fullscreenMedia, setFullscreenMedia] = useState(null);
  const [zoomState, setZoomState] = useState({ ...DEFAULT_VIDEO_ZOOM_STATE });

  onZoomActiveChangeRef.current = onZoomActiveChange;

  function commitState(next) {
    stateRef.current = next;
    const active = Number(next?.scale || 1) > 1.001;
    if (zoomActiveRef.current !== active) {
      zoomActiveRef.current = active;
      onZoomActiveChangeRef.current?.(active);
    }
    setZoomState((previous) => (
      previous.scale === next.scale
      && previous.panX === next.panX
      && previous.panY === next.panY
        ? previous
        : next
    ));
  }

  useEffect(() => {
    pointersRef.current.clear();
    gestureRef.current = null;
    touchGestureRef.current = createVideoTouchGestureState();
    lastTouchTapRef.current = null;
    touchDoubleTapSuppressionRef.current = null;
    ownedSecondTouchTapRef.current = null;
    commitState({ ...DEFAULT_VIDEO_ZOOM_STATE });
  }, [sourceKey]);

  useEffect(() => () => {
    if (zoomActiveRef.current) onZoomActiveChangeRef.current?.(false);
  }, []);

  function consumeTouchCompatibilityDoubleClick(event) {
    const nativeEvent = event?.nativeEvent || event;
    const suppression = consumeTouchDoubleTapSuppressionToken(touchDoubleTapSuppressionRef.current, {
      time: Date.now(),
      point: {
        x: Number(nativeEvent?.clientX || 0),
        y: Number(nativeEvent?.clientY || 0),
      },
      ownerKey: String(sourceKey || ""),
    });
    if (!suppression.consumed) return false;
    touchDoubleTapSuppressionRef.current = suppression.nextToken;
    stopHandledGesture(event);
    nativeEvent?.stopImmediatePropagation?.();
    return true;
  }

  useEffect(() => {
    const element = surfaceRef.current;
    if (!element) return undefined;
    const handleNativeDoubleClickCapture = (event) => {
      consumeTouchCompatibilityDoubleClick(event);
    };
    element.addEventListener("dblclick", handleNativeDoubleClickCapture, { capture: true });
    return () => element.removeEventListener("dblclick", handleNativeDoubleClickCapture, { capture: true });
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
      mode: "mousePan",
      pointerId: pointer.id,
      lastX: pointer.clientX,
      lastY: pointer.clientY,
    };
    event.currentTarget?.setPointerCapture?.(event.pointerId);
    stopHandledGesture(event);
  }

  function startPinch(event, pointerIds) {
    const element = gestureElement();
    const [firstId, secondId] = pointerIds;
    const first = pointersRef.current.get(firstId);
    const second = pointersRef.current.get(secondId);
    if (!element || !first || !second) return;
    const startMidpoint = midpointBetweenPoints(first, second);
    gestureRef.current = {
      mode: "touchPinch",
      pointerIds: [firstId, secondId],
      startDistance: distanceBetweenPoints(first, second),
      startMidpoint,
      startState: stateRef.current,
    };
    for (const pointerId of [firstId, secondId]) {
      try {
        event.currentTarget?.setPointerCapture?.(pointerId);
      } catch (_) {}
    }
    lastTouchTapRef.current = null;
    stopHandledGesture(event);
  }

  function visibleMediaRect(element) {
    const media = element?.matches?.("video")
      ? element
      : element?.querySelector?.("video");
    const mediaWidth = Number(media?.videoWidth || 0);
    const mediaHeight = Number(media?.videoHeight || 0);
    return containedMediaRect(rectFor(element), mediaWidth, mediaHeight);
  }

  function handlePointerDown(event) {
    if (event.target?.closest?.("button, select, input, textarea, a")) return;
    const element = gestureElement();
    if (!element) return;
    const pointer = pointerData(event, element);
    pointersRef.current.set(event.pointerId, pointer);

    if (pointer.pointerType === "mouse") {
      if (!event.nativeEvent?.sourceCapabilities?.firesTouchEvents) {
        touchDoubleTapSuppressionRef.current = null;
      }
      if (event.button === 0 && stateRef.current.scale > 1) startPan(event, pointer);
      return;
    }
    if (pointer.pointerType !== "touch") return;

    const now = Date.now();
    const transition = transitionVideoTouchGesture(touchGestureRef.current, {
      type: "down",
      pointerId: pointer.id,
      point: { x: pointer.clientX, y: pointer.clientY },
      time: now,
      panEnabled: stateRef.current.scale > 1.001,
    });
    touchGestureRef.current = transition.state;
    if (isTouchDoubleTap(lastTouchTapRef.current, {
      time: now,
      point: { x: pointer.clientX, y: pointer.clientY },
    })) {
      ownedSecondTouchTapRef.current = pointer.id;
      stopHandledGesture(event);
    }
    if (transition.invalidatedTap) lastTouchTapRef.current = null;
    if (transition.startedPinch) {
      startPinch(event, transition.state.activePointerIds.slice(0, 2));
    } else if (transition.consumeEvent) {
      stopHandledGesture(event);
    }
  }

  function handlePointerMove(event) {
    const element = gestureElement();
    if (!element || !pointersRef.current.has(event.pointerId)) return;
    const previousPointer = pointersRef.current.get(event.pointerId);
    const nextPointer = pointerData(event, element);
    pointersRef.current.set(event.pointerId, nextPointer);
    if (nextPointer.pointerType === "touch") {
      const transition = transitionVideoTouchGesture(touchGestureRef.current, {
        type: "move",
        pointerId: nextPointer.id,
        point: { x: nextPointer.clientX, y: nextPointer.clientY },
        time: Date.now(),
      });
      touchGestureRef.current = transition.state;
      if (transition.invalidatedTap) lastTouchTapRef.current = null;
      const touchGesture = gestureRef.current;
      if (touchGesture?.mode === "touchPinch" && transition.state.mode === "pinch") {
        const [firstId, secondId] = touchGesture.pointerIds;
        const first = pointersRef.current.get(firstId);
        const second = pointersRef.current.get(secondId);
        if (!first || !second) return;
        const midpoint = midpointBetweenPoints(first, second);
        const currentDistance = distanceBetweenPoints(first, second);
        commitState(
          zoomFromPinch(
            touchGesture.startState,
            rectFor(element),
            touchGesture.startDistance,
            currentDistance,
            touchGesture.startMidpoint,
            midpoint,
            maxZoom
          )
        );
        stopHandledGesture(event);
      } else if (transition.startedPan || (touchGesture?.mode === "touchPan" && transition.state.mode === "touchPan")) {
        const origin = transition.startedPan ? previousPointer : touchGesture;
        const deltaX = nextPointer.clientX - Number(origin?.lastX ?? origin?.clientX ?? nextPointer.clientX);
        const deltaY = nextPointer.clientY - Number(origin?.lastY ?? origin?.clientY ?? nextPointer.clientY);
        gestureRef.current = {
          mode: "touchPan",
          pointerId: nextPointer.id,
          lastX: nextPointer.clientX,
          lastY: nextPointer.clientY,
        };
        try {
          event.currentTarget?.setPointerCapture?.(event.pointerId);
        } catch (_) {}
        commitState(panBy(stateRef.current, rectFor(element), deltaX, deltaY));
        stopHandledGesture(event);
      } else if (transition.consumeEvent) {
        stopHandledGesture(event);
      }
      return;
    }
    const gesture = gestureRef.current;
    if (!gesture) return;

    if (gesture.mode === "mousePan") {
      if (gesture.pointerId !== event.pointerId) return;
      const deltaX = nextPointer.clientX - gesture.lastX;
      const deltaY = nextPointer.clientY - gesture.lastY;
      gestureRef.current = { ...gesture, lastX: nextPointer.clientX, lastY: nextPointer.clientY };
      commitState(panBy(stateRef.current, rectFor(element), deltaX, deltaY));
      stopHandledGesture(event);
      return;
    }

  }

  function handlePointerEnd(event) {
    const pointer = pointersRef.current.get(event.pointerId);
    if (pointer?.pointerType === "touch") {
      const finalPointer = pointerData(event, gestureElement());
      const transition = transitionVideoTouchGesture(touchGestureRef.current, {
        type: event.type === "pointercancel" ? "cancel" : "up",
        pointerId: pointer.id,
        point: { x: finalPointer.clientX, y: finalPointer.clientY },
        time: Date.now(),
      });
      touchGestureRef.current = transition.state;
      pointersRef.current.delete(event.pointerId);
      if (transition.invalidatedTap) lastTouchTapRef.current = null;
      if (transition.endedPinch || transition.endedPan) gestureRef.current = null;
      if (transition.completedTap) {
        const now = Date.now();
        const tap = {
          time: now,
          point: { x: finalPointer.clientX, y: finalPointer.clientY },
          localPoint: { x: finalPointer.x, y: finalPointer.y },
        };
        const previous = lastTouchTapRef.current;
        if (isTouchDoubleTap(previous, tap)) {
          ownedSecondTouchTapRef.current = null;
          lastTouchTapRef.current = null;
          touchDoubleTapSuppressionRef.current = createTouchDoubleTapSuppressionToken({
            time: now,
            point: tap.point,
            ownerKey: String(sourceKey || ""),
          });
          const element = gestureElement();
          onTouchDoubleTap?.({
            event,
            point: tap.localPoint,
            zone: touchDoubleTapZone(tap.localPoint, visibleMediaRect(element)),
          });
          stopHandledGesture(event);
        } else {
          lastTouchTapRef.current = tap;
        }
      } else if (transition.consumeEvent) {
        lastTouchTapRef.current = null;
        stopHandledGesture(event);
      }
      if (ownedSecondTouchTapRef.current === pointer.id) {
        ownedSecondTouchTapRef.current = null;
      }
      try {
        event.currentTarget?.releasePointerCapture?.(event.pointerId);
      } catch (_) {}
      return;
    }

    pointersRef.current.delete(event.pointerId);
    const gesture = gestureRef.current;
    if (!gesture) return;
    if (gesture.mode === "mousePan" && gesture.pointerId === event.pointerId) {
      gestureRef.current = null;
      return;
    }
  }

  function handleDoubleClick(event) {
    if (consumeTouchCompatibilityDoubleClick(event)) return;
    if (event.target?.closest?.("button, select, input, textarea, a")) return;
    stopHandledGesture(event);
    onDesktopDoubleClick?.(event);
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
      data-video-touch-owner="true"
      onPointerDown={handlePointerDown}
      onPointerMove={handlePointerMove}
      onPointerUp={handlePointerEnd}
      onPointerCancel={handlePointerEnd}
      onDoubleClick={handleDoubleClick}
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
