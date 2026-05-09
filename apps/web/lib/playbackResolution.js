export const HIGH_RESOLUTION_THRESHOLD = {
  width: 2560,
  height: 1440,
};

export const COMPACT_VIEWER_THRESHOLD = {
  minWidth: 960,
  minHeight: 540,
  naturalScale: 0.75,
  maxComfortWidth: 1280,
  maxComfortHeight: 720,
};

export function normalizeVideoDimensions(width, height) {
  const naturalWidth = Number(width || 0);
  const naturalHeight = Number(height || 0);
  if (!Number.isFinite(naturalWidth) || !Number.isFinite(naturalHeight)) {
    return { width: 0, height: 0 };
  }
  return {
    width: Math.max(0, Math.round(naturalWidth)),
    height: Math.max(0, Math.round(naturalHeight)),
  };
}

export function isHighResolutionVideo(dimensions) {
  const { width, height } = normalizeVideoDimensions(dimensions?.width, dimensions?.height);
  if (!width || !height) return false;
  return width >= HIGH_RESOLUTION_THRESHOLD.width || height >= HIGH_RESOLUTION_THRESHOLD.height;
}

export function isCompactPlaybackViewer(dimensions, rect) {
  const natural = normalizeVideoDimensions(dimensions?.width, dimensions?.height);
  const viewerWidth = Number(rect?.width || 0);
  const viewerHeight = Number(rect?.height || 0);
  if (!natural.width || !natural.height || !viewerWidth || !viewerHeight) return false;

  const scaledWidthLimit = Math.min(
    COMPACT_VIEWER_THRESHOLD.maxComfortWidth,
    natural.width * COMPACT_VIEWER_THRESHOLD.naturalScale
  );
  const scaledHeightLimit = Math.min(
    COMPACT_VIEWER_THRESHOLD.maxComfortHeight,
    natural.height * COMPACT_VIEWER_THRESHOLD.naturalScale
  );

  return (
    viewerWidth < COMPACT_VIEWER_THRESHOLD.minWidth ||
    viewerHeight < COMPACT_VIEWER_THRESHOLD.minHeight ||
    viewerWidth < scaledWidthLimit ||
    viewerHeight < scaledHeightLimit
  );
}

export function shouldUseAdaptiveHighResolutionPlayback(dimensions, rect, isFullscreen = false) {
  if (isFullscreen) return false;
  return isHighResolutionVideo(dimensions) && isCompactPlaybackViewer(dimensions, rect);
}
