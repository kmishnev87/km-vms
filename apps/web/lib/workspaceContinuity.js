export function reconcilePlaybackMembership(tiles, previous = {}) {
  const retained = {};
  const missing = [];
  for (const tile of tiles) {
    const playback = previous[tile.id];
    if (playback && String(playback.sourceCameraId ?? playback.cameraId ?? "") === String(tile.cameraId)) retained[tile.id] = playback;
    else missing.push(tile);
  }
  return { retained, missing };
}

export function selectTimelineLabels(marks, width, minimumGap = 56) {
  const major = marks.filter((mark) => mark.isMajor && mark.tickLabel);
  if (!Number.isFinite(width) || width <= 0 || !major.length) return new Set();
  const spacing = major.length > 1 ? (major[1].leftPct - major[0].leftPct) * width / 100 : width;
  const stride = Math.max(1, Math.ceil(minimumGap / Math.max(1, spacing)));
  const step = major.length > 1 ? major[1].ms - major[0].ms : 1;
  const visible = major.filter((mark) => {
    const x = mark.leftPct * width / 100;
    return Math.round(mark.ms / step) % stride === 0 && x >= minimumGap / 2 && x <= width - minimumGap / 2;
  });
  return new Set(visible.map((mark) => mark.ms));
}

export function timelineRangeWindow(centerMs, spanMs) {
  const paddingMs = Math.min(spanMs / 8, 15 * 60 * 1000);
  return { fromMs: centerMs - spanMs / 2 - paddingMs, toMs: centerMs + spanMs / 2 + paddingMs };
}

export function timelineRangeWindowCovers(window, desired) {
  return Boolean(window && window.zoomKey === desired.zoomKey && window.cameraKey === desired.cameraKey
    && window.fromMs <= desired.fromMs && window.toMs >= desired.toMs);
}

export function timelineRangesNeedReload(loaded, desired, nowMs) {
  return !timelineRangeWindowCovers(loaded, desired) || !Number.isFinite(loaded.fetchedAt)
    || nowMs < loaded.fetchedAt || nowMs - loaded.fetchedAt >= 60_000;
}
