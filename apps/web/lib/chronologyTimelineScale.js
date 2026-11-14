// One scale contract for the ruler, range requests and zoom controls.
export const TIMELINE_ZOOM_OPTIONS = [
  { key: "3h", label: "3ч", hours: 3, majorEveryMinutes: 15, minorEveryMinutes: 5 },
  { key: "6h", label: "6ч", hours: 6, majorEveryMinutes: 30, minorEveryMinutes: 15 },
  { key: "12h", label: "12ч", hours: 12, majorEveryMinutes: 60, minorEveryMinutes: 30 },
  { key: "24h", label: "24ч", hours: 24, majorEveryMinutes: 60, minorEveryMinutes: 30 },
  { key: "3d", label: "3д", hours: 72, majorEveryMinutes: 360, minorEveryMinutes: 180 },
  { key: "7d", label: "7д", hours: 168, majorEveryMinutes: 720, minorEveryMinutes: 360 },
];

export const TIMELINE_ZOOM_KEYS = TIMELINE_ZOOM_OPTIONS.map(option => option.key);
export const TIMELINE_ZOOM_HOURS = Object.fromEntries(TIMELINE_ZOOM_OPTIONS.map(option => [option.key, option.hours]));

export function timelineScale(key) {
  return TIMELINE_ZOOM_OPTIONS.find(option => option.key === key) || TIMELINE_ZOOM_OPTIONS[3];
}

export function buildTimelineTickLabel(dt, zoomKey) {
  const pad = value => String(value).padStart(2, "0");
  const dateLabel = `${pad(dt.getUTCDate())}.${pad(dt.getUTCMonth() + 1)}`;
  const timeLabel = `${pad(dt.getUTCHours())}:${pad(dt.getUTCMinutes())}`;
  if (timelineScale(zoomKey).hours <= 24) return { primary: timeLabel, secondary: "", multiline: false };
  if (dt.getUTCHours() === 0 && dt.getUTCMinutes() === 0) return { primary: dateLabel, secondary: "", multiline: false };
  return { primary: dateLabel, secondary: timeLabel, multiline: true };
}

export function timelineAxis(centerMs, scale) {
  const spanMs = scale.hours * 3600000;
  const startMs = centerMs - spanMs / 2;
  const endMs = centerMs + spanMs / 2;
  const majorStepMs = scale.majorEveryMinutes * 60000;
  const minorStepMs = scale.minorEveryMinutes * 60000;
  const axisMarks = [];
  for (let ms = Math.ceil(startMs / minorStepMs) * minorStepMs; ms <= endMs; ms += minorStepMs) {
    const isMajor = ms % majorStepMs === 0;
    axisMarks.push({ ms, isMajor, tickLabel: isMajor ? buildTimelineTickLabel(new Date(ms), scale.key) : null, leftPct: (ms - startMs) / spanMs * 100 });
  }
  return { startMs, endMs, axisMarks };
}

// Minimum painted widths must not seek a short recording into an adjacent gap.
export function recordingRangeTarget(startMs, endMs, ratio) {
  return startMs + Math.max(0, Math.min(1, ratio)) * Math.max(0, endMs - startMs - 1);
}
