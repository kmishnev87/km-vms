"use client";

import { useMemo } from "react";

const ZOOM_OPTIONS = [
  { key: "24h", label: "24ч", hours: 24 },
  { key: "3d", label: "3д", hours: 72 },
  { key: "7d", label: "7д", hours: 168 },
];

function pad(v) {
  return String(v).padStart(2, "0");
}

function formatTick(dt, hoursSpan) {
  if (hoursSpan <= 24) {
    return `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  }
  return `${pad(dt.getDate())}.${pad(dt.getMonth() + 1)} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
}

function toMs(value) {
  return new Date(value).getTime();
}

export default function ChronologyTimeline({
  currentTs,
  zoomKey,
  onZoomOut,
  onZoomIn,
  onSelectTime,
  rangesByCamera,
  selectedCameraIds,
  cameraNames,
}) {
  const zoom = ZOOM_OPTIONS.find((x) => x.key === zoomKey) || ZOOM_OPTIONS[0];

  const { startMs, endMs, ticks } = useMemo(() => {
    const centerMs = currentTs ? currentTs.getTime() : Date.now();
    const halfSpanMs = (zoom.hours * 3600 * 1000) / 2;
    const startMs = centerMs - halfSpanMs;
    const endMs = centerMs + halfSpanMs;

    const tickCount = zoom.key === "24h" ? 8 : zoom.key === "3d" ? 9 : 8;
    const ticks = [];

    for (let i = 0; i <= tickCount; i += 1) {
      const ms = startMs + ((endMs - startMs) * i) / tickCount;
      ticks.push(new Date(ms));
    }

    return { startMs, endMs, ticks };
  }, [currentTs, zoom]);

  function handleClick(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const ratio = Math.max(0, Math.min(1, x / rect.width));
    const targetMs = startMs + (endMs - startMs) * ratio;
    onSelectTime?.(new Date(targetMs));
  }

  return (
    <div className="chronologyTimelineCard">
      <div className="chronologyTimelineTopBar">
        <div className="chronologyTimelineZoomControls">
          <button
            className="chronologyIconButton"
            onClick={onZoomOut}
            title="Уменьшить масштаб"
          >
            -
          </button>

          <div className="chronologyTimelineZoomLabel">{zoom.label}</div>

          <button
            className="chronologyIconButton"
            onClick={onZoomIn}
            title="Увеличить масштаб"
          >
            +
          </button>
        </div>
      </div>

      <div className="chronologyTimelineTicks">
        {ticks.map((tick, idx) => (
          <div className="chronologyTimelineTick" key={idx}>
            {formatTick(tick, zoom.hours)}
          </div>
        ))}
      </div>

      <div className="chronologyTimelineBody" onClick={handleClick}>
        <div className="chronologyTimelineCenterLine" />

        {selectedCameraIds.length ? (
          <div className="chronologyTimelineTracks">
            {selectedCameraIds.map((cameraId) => {
              const cameraKey = String(cameraId);
              const item = rangesByCamera?.[cameraKey];
              const ranges = item?.ranges || [];
              const cameraName = cameraNames?.[cameraKey] || `Камера ${cameraKey}`;

              return (
                <div className="chronologyTimelineTrackRow" key={cameraKey}>
                  <div className="chronologyTimelineTrackLabel">{cameraName}</div>

                  <div className="chronologyTimelineTrackLane">
                    {ranges.map((range, idx) => {
                      const rangeStart = toMs(range.start);
                      const rangeEnd = toMs(range.end);

                      if (rangeEnd <= startMs || rangeStart >= endMs) {
                        return null;
                      }

                      const clippedStart = Math.max(rangeStart, startMs);
                      const clippedEnd = Math.min(rangeEnd, endMs);

                      const leftPct = ((clippedStart - startMs) / (endMs - startMs)) * 100;
                      const widthPct = ((clippedEnd - clippedStart) / (endMs - startMs)) * 100;

                      return (
                        <div
                          key={idx}
                          className="chronologyTimelineRange"
                          style={{
                            left: `${leftPct}%`,
                            width: `${Math.max(widthPct, 0.5)}%`,
                          }}
                          title={`${range.start} — ${range.end}`}
                        />
                      );
                    })}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="chronologyTimelineEmpty">
            Выбери камеры для отображения диапазонов на таймлайне
          </div>
        )}
      </div>
    </div>
  );
}
