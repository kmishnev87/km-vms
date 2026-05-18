export const VIDEO_ZOOM_MIN = 1;
export const VIDEO_ZOOM_MAX = 4;

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

export function zoomFromPinch(startState, rect, startDistance, currentDistance, midpoint, maxZoom = VIDEO_ZOOM_MAX) {
  const safeStart = Math.max(1, finiteNumber(startDistance, 1));
  const safeCurrent = Math.max(1, finiteNumber(currentDistance, safeStart));
  return zoomAtPoint(startState, rect, midpoint, safeCurrent / safeStart, maxZoom);
}
