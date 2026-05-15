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

export const COMPACT_RENDER_TIERS = [
  { name: "normal", minRatio: 0.75, renderer: "native", backingScale: 1 },
  { name: "ultra-soft", minRatio: 0.65, renderer: "canvas", backingScale: 1.35 },
  { name: "soft", minRatio: 0.55, renderer: "canvas", backingScale: 1.25 },
  { name: "medium-soft", minRatio: 0.45, renderer: "canvas", backingScale: 1.15 },
  { name: "medium", minRatio: 0.35, renderer: "canvas", backingScale: 1.05 },
  { name: "strong", minRatio: 0.25, renderer: "canvas", backingScale: 1 },
  { name: "stronger", minRatio: 0.15, renderer: "canvas", backingScale: 1 },
  { name: "strongest", minRatio: 0, renderer: "canvas", backingScale: 1 },
];

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

export function downscaleRatio(dimensions, rect) {
  const natural = normalizeVideoDimensions(dimensions?.width, dimensions?.height);
  const rendered = normalizeVideoDimensions(rect?.width, rect?.height);
  if (!natural.width || !natural.height || !rendered.width || !rendered.height) return null;
  return Math.min(rendered.width / natural.width, rendered.height / natural.height);
}

export function compactRenderTierForRatio(ratio) {
  const value = Number(ratio);
  if (!Number.isFinite(value)) return COMPACT_RENDER_TIERS[0];
  return COMPACT_RENDER_TIERS.find((tier) => value >= tier.minRatio) || COMPACT_RENDER_TIERS[COMPACT_RENDER_TIERS.length - 1];
}

export function planContainDrawRect(sourceWidth, sourceHeight, targetWidth, targetHeight) {
  const source = normalizeVideoDimensions(sourceWidth, sourceHeight);
  const target = normalizeVideoDimensions(targetWidth, targetHeight);
  if (!source.width || !source.height || !target.width || !target.height) {
    return { dx: 0, dy: 0, width: target.width, height: target.height };
  }

  const scale = Math.min(target.width / source.width, target.height / source.height);
  const width = Math.max(1, Math.round(source.width * scale));
  const height = Math.max(1, Math.round(source.height * scale));
  return {
    dx: Math.round((target.width - width) / 2),
    dy: Math.round((target.height - height) / 2),
    width,
    height,
  };
}

export function planCompactVideoDownscale({
  sourceWidth,
  sourceHeight,
  cssWidth,
  cssHeight,
  mode,
  ratio,
  backingScale = 1,
  devicePixelRatio = 1,
} = {}) {
  const source = normalizeVideoDimensions(sourceWidth, sourceHeight);
  const css = normalizeVideoDimensions(cssWidth, cssHeight);
  const requestedScale = Math.max(1, Number(backingScale || 1));
  const scale = Math.max(1, Math.min(requestedScale, Number(devicePixelRatio || 1), 1.5));
  const canvas = {
    width: Math.max(1, Math.round(css.width * scale)),
    height: Math.max(1, Math.round(css.height * scale)),
  };
  const target = planContainDrawRect(source.width, source.height, canvas.width, canvas.height);

  if (!source.width || !source.height || !css.width || !css.height || mode === "normal") {
    return {
      renderer: "native",
      qualityPath: "native",
      backingScale: scale,
      canvas,
      target,
      passes: [],
      passCount: 0,
    };
  }

  const passes = [];
  let width = source.width;
  let height = source.height;
  const severe = ratio != null && Number(ratio) < 0.35;
  const moderate = ratio != null && Number(ratio) < 0.55;
  const maxPasses = severe ? 8 : moderate ? 5 : 1;
  const divisor = severe ? 2 : 1.65;

  if (moderate || severe) {
    while ((width > target.width * 2 || height > target.height * 2) && passes.length < maxPasses) {
      width = Math.max(target.width, Math.round(width / divisor));
      height = Math.max(target.height, Math.round(height / divisor));
      passes.push({ width, height, intermediate: true });
    }
  }

  passes.push({ width: target.width, height: target.height, intermediate: false });

  return {
    renderer: "canvas",
    qualityPath: passes.length > 1 ? "multipass-downscale" : "single-pass-high-quality",
    backingScale: scale,
    canvas,
    target,
    passes,
    passCount: passes.length,
  };
}

export function selectCompactVideoRenderMode({
  dimensions,
  rect,
  isFullscreen = false,
  sourceHighResolution = false,
} = {}) {
  const natural = normalizeVideoDimensions(dimensions?.width, dimensions?.height);
  const rendered = normalizeVideoDimensions(rect?.width, rect?.height);
  const ratio = downscaleRatio(natural, rendered);
  const highResolution = isHighResolutionVideo(natural) || Boolean(sourceHighResolution && natural.width && natural.height);

  if (isFullscreen) {
    return { renderer: "native", mode: "normal", qualityTier: "normal", ratio, backingScale: 1, reason: "fullscreen" };
  }

  if (!highResolution || ratio === null) {
    return {
      renderer: "native",
      mode: "normal",
      qualityTier: "normal",
      ratio,
      backingScale: 1,
      reason: highResolution ? "missing-dimensions" : "not-high-resolution",
    };
  }

  const tier = compactRenderTierForRatio(ratio);
  return {
    renderer: tier.renderer,
    mode: tier.renderer === "canvas" ? `compact-${tier.name}` : "normal",
    qualityTier: tier.name,
    ratio,
    backingScale: tier.backingScale,
    reason: tier.renderer === "canvas" ? "compact-downscale" : "ratio-normal",
  };
}
