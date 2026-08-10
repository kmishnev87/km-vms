export const VIDEO_ZOOM_MIN = 1;
export const VIDEO_ZOOM_MAX = 4;
export const VIDEO_TOUCH_MOVE_THRESHOLD_PX = 12;
export const VIDEO_TOUCH_TAP_MAX_MS = 500;
export const TOUCH_DOUBLE_TAP_SUPPRESSION_MAX_MS = 900;
export const TOUCH_DOUBLE_TAP_SUPPRESSION_DISTANCE_PX = 42;

export const DEFAULT_VIDEO_ZOOM_STATE = Object.freeze({
  scale: 1,
  panX: 0,
  panY: 0,
});

function finiteNumber(value, fallback = 0) {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
}

export function clamp(value, min, max) {
  return Math.min(Math.max(finiteNumber(value, min), min), max);
}

export function normalizeZoomState(state) {
  const scale = clamp(state?.scale, VIDEO_ZOOM_MIN, VIDEO_ZOOM_MAX);
  if (scale <= VIDEO_ZOOM_MIN) return { ...DEFAULT_VIDEO_ZOOM_STATE };
  return {
    scale,
    panX: finiteNumber(state?.panX, 0),
    panY: finiteNumber(state?.panY, 0),
  };
}

export function panBounds(rect, scale) {
  const width = Math.max(0, finiteNumber(rect?.width, 0));
  const height = Math.max(0, finiteNumber(rect?.height, 0));
  const nextScale = Math.max(VIDEO_ZOOM_MIN, finiteNumber(scale, VIDEO_ZOOM_MIN));
  return {
    maxX: (width * (nextScale - 1)) / 2,
    maxY: (height * (nextScale - 1)) / 2,
  };
}

export function clampPan(state, rect) {
  const next = normalizeZoomState(state);
  if (next.scale <= VIDEO_ZOOM_MIN) return { ...DEFAULT_VIDEO_ZOOM_STATE };
  const bounds = panBounds(rect, next.scale);
  return {
    scale: next.scale,
    panX: clamp(next.panX, -bounds.maxX, bounds.maxX),
    panY: clamp(next.panY, -bounds.maxY, bounds.maxY),
  };
}

export function zoomAtPoint(state, rect, focalPoint, scaleFactor, maxZoom = VIDEO_ZOOM_MAX) {
  const current = normalizeZoomState(state);
  const width = Math.max(1, finiteNumber(rect?.width, 1));
  const height = Math.max(1, finiteNumber(rect?.height, 1));
  const oldScale = current.scale;
  const nextScale = clamp(oldScale * finiteNumber(scaleFactor, 1), VIDEO_ZOOM_MIN, maxZoom);

  if (nextScale <= VIDEO_ZOOM_MIN) return { ...DEFAULT_VIDEO_ZOOM_STATE };

  const focalX = clamp(finiteNumber(focalPoint?.x, width / 2), 0, width) - width / 2;
  const focalY = clamp(finiteNumber(focalPoint?.y, height / 2), 0, height) - height / 2;
  const contentX = (focalX - current.panX) / oldScale;
  const contentY = (focalY - current.panY) / oldScale;

  return clampPan({
    scale: nextScale,
    panX: focalX - contentX * nextScale,
    panY: focalY - contentY * nextScale,
  }, { width, height });
}

export function panBy(state, rect, deltaX, deltaY) {
  const current = normalizeZoomState(state);
  if (current.scale <= VIDEO_ZOOM_MIN) return { ...DEFAULT_VIDEO_ZOOM_STATE };
  return clampPan({
    scale: current.scale,
    panX: current.panX + finiteNumber(deltaX, 0),
    panY: current.panY + finiteNumber(deltaY, 0),
  }, rect);
}

export function distanceBetweenPoints(a, b) {
  const dx = finiteNumber(a?.x, 0) - finiteNumber(b?.x, 0);
  const dy = finiteNumber(a?.y, 0) - finiteNumber(b?.y, 0);
  return Math.hypot(dx, dy);
}

export function midpointBetweenPoints(a, b) {
  return {
    x: (finiteNumber(a?.x, 0) + finiteNumber(b?.x, 0)) / 2,
    y: (finiteNumber(a?.y, 0) + finiteNumber(b?.y, 0)) / 2,
  };
}

export function zoomFromPinch(
  startState,
  rect,
  startDistance,
  currentDistance,
  startMidpoint,
  currentMidpoint,
  maxZoom = VIDEO_ZOOM_MAX
) {
  const current = normalizeZoomState(startState);
  const width = Math.max(1, finiteNumber(rect?.width, 1));
  const height = Math.max(1, finiteNumber(rect?.height, 1));
  const safeStart = Math.max(1, finiteNumber(startDistance, 1));
  const safeCurrent = Math.max(1, finiteNumber(currentDistance, safeStart));
  const nextScale = clamp(
    current.scale * (safeCurrent / safeStart),
    VIDEO_ZOOM_MIN,
    maxZoom
  );
  if (nextScale <= VIDEO_ZOOM_MIN) return { ...DEFAULT_VIDEO_ZOOM_STATE };

  const startX = clamp(finiteNumber(startMidpoint?.x, width / 2), 0, width) - width / 2;
  const startY = clamp(finiteNumber(startMidpoint?.y, height / 2), 0, height) - height / 2;
  const nextX = clamp(finiteNumber(currentMidpoint?.x, width / 2), 0, width) - width / 2;
  const nextY = clamp(finiteNumber(currentMidpoint?.y, height / 2), 0, height) - height / 2;
  const contentX = (startX - current.panX) / current.scale;
  const contentY = (startY - current.panY) / current.scale;

  return clampPan(
    {
      scale: nextScale,
      panX: nextX - contentX * nextScale,
      panY: nextY - contentY * nextScale,
    },
    { width, height }
  );
}

export function touchDoubleTapZone(point, rect) {
  const width = Math.max(1, finiteNumber(rect?.width, 1));
  const left = finiteNumber(rect?.left, 0);
  const ratio = clamp((finiteNumber(point?.x, left + width / 2) - left) / width, 0, 1);
  if (ratio < 0.35) return "left";
  if (ratio < 0.65) return "center";
  return "right";
}

export function containedMediaRect(containerRect, mediaWidth, mediaHeight) {
  const width = Math.max(1, finiteNumber(containerRect?.width, 1));
  const height = Math.max(1, finiteNumber(containerRect?.height, 1));
  const sourceWidth = Math.max(0, finiteNumber(mediaWidth, 0));
  const sourceHeight = Math.max(0, finiteNumber(mediaHeight, 0));
  if (!sourceWidth || !sourceHeight) {
    return { left: 0, top: 0, width, height };
  }
  const scale = Math.min(width / sourceWidth, height / sourceHeight);
  const renderedWidth = sourceWidth * scale;
  const renderedHeight = sourceHeight * scale;
  return {
    left: (width - renderedWidth) / 2,
    top: (height - renderedHeight) / 2,
    width: renderedWidth,
    height: renderedHeight,
  };
}

export function isTouchDoubleTap(
  previous,
  current,
  { maxDelayMs = 360, maxDistancePx = 28 } = {}
) {
  if (!previous || !current) return false;
  const delay = finiteNumber(current.time, 0) - finiteNumber(previous.time, 0);
  return Boolean(
    delay >= 0 &&
    delay <= maxDelayMs &&
    distanceBetweenPoints(previous.point, current.point) <= maxDistancePx
  );
}

export function createTouchDoubleTapSuppressionToken({ time, point, ownerKey = "" } = {}) {
  return {
    time: finiteNumber(time, 0),
    point: {
      x: finiteNumber(point?.x, 0),
      y: finiteNumber(point?.y, 0),
    },
    ownerKey: String(ownerKey || ""),
  };
}

export function matchesTouchDoubleTapSuppressionToken(
  token,
  candidate,
  {
    maxDelayMs = TOUCH_DOUBLE_TAP_SUPPRESSION_MAX_MS,
    maxDistancePx = TOUCH_DOUBLE_TAP_SUPPRESSION_DISTANCE_PX,
  } = {}
) {
  if (!token || !candidate) return false;
  const tokenOwner = String(token.ownerKey || "");
  const candidateOwner = String(candidate.ownerKey || "");
  if (tokenOwner !== candidateOwner) return false;
  const delay = finiteNumber(candidate.time, 0) - finiteNumber(token.time, 0);
  return Boolean(
    delay >= 0 &&
    delay <= maxDelayMs &&
    distanceBetweenPoints(token.point, candidate.point) <= maxDistancePx
  );
}

export function consumeTouchDoubleTapSuppressionToken(token, candidate, options) {
  const consumed = matchesTouchDoubleTapSuppressionToken(token, candidate, options);
  return {
    consumed,
    nextToken: consumed ? null : token,
  };
}

export function createVideoTouchGestureState() {
  return {
    mode: "idle",
    activePointerIds: [],
    primaryPointerId: null,
    startPoint: null,
    startedAt: 0,
    moved: false,
    hadMultiTouch: false,
    panEnabled: false,
  };
}

export function transitionVideoTouchGesture(
  state,
  action,
  {
    moveThresholdPx = VIDEO_TOUCH_MOVE_THRESHOLD_PX,
    tapMaxMs = VIDEO_TOUCH_TAP_MAX_MS,
  } = {}
) {
  const current = state?.mode ? state : createVideoTouchGestureState();
  const type = String(action?.type || "");
  const pointerId = action?.pointerId;
  const point = {
    x: finiteNumber(action?.point?.x, 0),
    y: finiteNumber(action?.point?.y, 0),
  };
  const time = finiteNumber(action?.time, 0);
  const active = current.activePointerIds.includes(pointerId)
    ? [...current.activePointerIds]
    : [...current.activePointerIds, pointerId];
  const unchanged = {
    state: current,
    completedTap: false,
    startedPinch: false,
    endedPinch: false,
    startedPan: false,
    endedPan: false,
    invalidatedTap: false,
    consumeEvent:
      current.mode === "touchPan" ||
      current.mode === "pinch" ||
      current.mode === "consumed",
  };

  if (pointerId == null) return unchanged;

  if (type === "down") {
    if (current.activePointerIds.includes(pointerId)) return unchanged;
    if (current.mode === "idle" && current.activePointerIds.length === 0) {
      return {
        ...unchanged,
        state: {
          mode: "pending",
          activePointerIds: active,
          primaryPointerId: pointerId,
          startPoint: point,
          startedAt: time,
          moved: false,
          hadMultiTouch: false,
          panEnabled: Boolean(action?.panEnabled),
        },
        consumeEvent: false,
      };
    }
    if (current.mode === "pending" || current.mode === "touchPan") {
      return {
        ...unchanged,
        state: {
          ...current,
          mode: "pinch",
          activePointerIds: active,
          moved: true,
          hadMultiTouch: true,
        },
        startedPinch: true,
        invalidatedTap: true,
        consumeEvent: true,
      };
    }
    return {
      ...unchanged,
      state: { ...current, activePointerIds: active, hadMultiTouch: true },
      consumeEvent: true,
    };
  }

  if (!current.activePointerIds.includes(pointerId)) return unchanged;

  if (type === "move") {
    if (current.mode === "touchPan" && current.primaryPointerId === pointerId) {
      return {
        ...unchanged,
        state: { ...current, moved: true },
        invalidatedTap: true,
        consumeEvent: true,
      };
    }
    if (current.mode !== "pending" || current.primaryPointerId !== pointerId) {
      return unchanged;
    }
    const moved =
      current.moved ||
      distanceBetweenPoints(current.startPoint, point) > moveThresholdPx;
    if (moved && current.panEnabled) {
      return {
        ...unchanged,
        state: {
          ...current,
          mode: "touchPan",
          moved: true,
        },
        startedPan: true,
        invalidatedTap: true,
        consumeEvent: true,
      };
    }
    return {
      ...unchanged,
      state: {
        ...current,
        moved,
      },
      invalidatedTap: moved,
      consumeEvent: false,
    };
  }

  if (type !== "up" && type !== "cancel") return unchanged;

  const remaining = current.activePointerIds.filter((id) => id !== pointerId);
  if (type === "cancel") {
    return {
      ...unchanged,
      state: remaining.length
        ? { ...current, mode: "consumed", activePointerIds: remaining, hadMultiTouch: true }
        : createVideoTouchGestureState(),
      endedPinch: current.mode === "pinch",
      endedPan: current.mode === "touchPan",
      invalidatedTap: true,
      consumeEvent:
        current.mode === "touchPan" ||
        current.mode === "pinch" ||
        current.mode === "consumed",
    };
  }
  if (current.mode === "pinch" || current.mode === "consumed" || current.hadMultiTouch) {
    return {
      ...unchanged,
      state: remaining.length
        ? { ...current, mode: "consumed", activePointerIds: remaining, hadMultiTouch: true }
        : createVideoTouchGestureState(),
      endedPinch: current.mode === "pinch",
      invalidatedTap: true,
      consumeEvent: true,
    };
  }

  if (current.mode === "touchPan") {
    return {
      ...unchanged,
      state: remaining.length
        ? { ...current, mode: "consumed", activePointerIds: remaining, hadMultiTouch: true }
        : createVideoTouchGestureState(),
      endedPan: true,
      invalidatedTap: true,
      consumeEvent: true,
    };
  }

  const completedTap = Boolean(
    type === "up" &&
    current.mode === "pending" &&
    current.primaryPointerId === pointerId &&
    !current.moved &&
    time - current.startedAt >= 0 &&
    time - current.startedAt <= tapMaxMs &&
    distanceBetweenPoints(current.startPoint, point) <= moveThresholdPx
  );
  return {
    ...unchanged,
    state: createVideoTouchGestureState(),
    completedTap,
    invalidatedTap: !completedTap,
    consumeEvent: false,
  };
}
