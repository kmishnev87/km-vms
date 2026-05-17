"use client";

import { useMemo, useRef, useState } from "react";

const ZOOM_OPTIONS = [
  { key: "24h", label: "24ч", hours: 24, majorEveryMinutes: 60, minorEveryMinutes: 30 },
  { key: "3d", label: "3д", hours: 72, majorEveryMinutes: 360, minorEveryMinutes: 180 },
  { key: "7d", label: "7д", hours: 168, majorEveryMinutes: 720, minorEveryMinutes: 360 },
];

const DRAG_THRESHOLD_PX = 4;

function pad(value) {
  return String(value).padStart(2, "0");
}

function buildTickLabel(dt, zoomKey) {
  const dateLabel = `${pad(dt.getDate())}.${pad(dt.getMonth() + 1)}`;
  const timeLabel = `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  const isMidnight = dt.getHours() === 0 && dt.getMinutes() === 0;

  if (zoomKey === "24h") {
    return {
      primary: timeLabel,
      secondary: "",
      multiline: false,
    };
  }

  if (isMidnight) {
    return {
      primary: dateLabel,
      secondary: "",
      multiline: false,
    };
  }

  return {
    primary: dateLabel,
    secondary: timeLabel,
    multiline: true,
  };
}

function parseNaiveDateTime(value) {
  if (!value) return Number.NaN;

  const match = String(value).match(
    /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/
  );

  if (!match) {
    return new Date(value).getTime();
  }

  const [, year, month, day, hours, minutes, seconds = "00"] = match;
  return new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hours),
    Number(minutes),
    Number(seconds)
  ).getTime();
}

function alignMs(ms, stepMs) {
  return Math.floor(ms / stepMs) * stepMs;
}

export default function ChronologyTimeline({
  currentTs,
  committedTs,
  zoomKey,
  onZoomOut,
  onZoomIn,
  onPreviewTime,
  onDragStart,
  onDragEnd,
  onSelectTime,
  rangesByCamera,
  selectedCameraIds,
  cameraNames,
  currentTimeLabel,
  committedTimeLabel,
  isPreviewing = false,
  rangesLoading = false,
  rangesError = false,
  compact = false,
}) {
  const rootRef = useRef(null);
  const dragStateRef = useRef(null);
  const [dragging, setDragging] = useState(false);

  const zoom = ZOOM_OPTIONS.find((item) => item.key === zoomKey) || ZOOM_OPTIONS[0];
  const committedMs = committedTs ? committedTs.getTime() : Number.NaN;

  const { startMs, endMs, axisMarks } = useMemo(() => {
    const centerMs = currentTs ? currentTs.getTime() : Date.now();
    const halfSpanMs = (zoom.hours * 3600 * 1000) / 2;
    const nextStartMs = centerMs - halfSpanMs;
    const nextEndMs = centerMs + halfSpanMs;

    const majorStepMs = zoom.majorEveryMinutes * 60 * 1000;
    const minorStepMs = zoom.minorEveryMinutes * 60 * 1000;
    const marks = [];

    for (let ms = alignMs(nextStartMs, minorStepMs); ms <= nextEndMs; ms += minorStepMs) {
      if (ms < nextStartMs) continue;

      const isMajor = ms % majorStepMs === 0;
      const tickLabel = isMajor ? buildTickLabel(new Date(ms), zoom.key) : null;

      marks.push({
        ms,
        isMajor,
        tickLabel,
        leftPct: ((ms - nextStartMs) / (nextEndMs - nextStartMs)) * 100,
      });
    }

    return {
      startMs: nextStartMs,
      endMs: nextEndMs,
      axisMarks: marks,
    };
  }, [currentTs, zoom]);

  const committedLeftPct = Number.isFinite(committedMs)
    ? ((committedMs - startMs) / (endMs - startMs)) * 100
    : Number.NaN;
  const showCommittedMarker =
    isPreviewing &&
    Number.isFinite(committedLeftPct) &&
    committedLeftPct >= 0 &&
    committedLeftPct <= 100 &&
    Math.abs(committedLeftPct - 50) > 0.6;
  const hasSelectedCameras = selectedCameraIds.length > 0;
  const visibleRangeCount = hasSelectedCameras
    ? selectedCameraIds.reduce((count, cameraId) => {
        const item = rangesByCamera?.[String(cameraId)];
        const ranges = Array.isArray(item?.ranges) ? item.ranges : [];
        return (
          count +
          ranges.filter((range) => {
            const rangeStart = parseNaiveDateTime(range.start);
            const rangeEnd = parseNaiveDateTime(range.end);
            return Number.isFinite(rangeStart) && Number.isFinite(rangeEnd) && rangeEnd > startMs && rangeStart < endMs;
          }).length
        );
      }, 0)
    : 0;
  const showNoRangeState = hasSelectedCameras && !rangesLoading && !rangesError && visibleRangeCount === 0;

  function handlePointerDown(event) {
    if (event.button !== 0) return;

    const rect = rootRef.current?.getBoundingClientRect();
    if (!rect) return;

    event.currentTarget?.setPointerCapture?.(event.pointerId);
    dragStateRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startCenterMs: currentTs ? currentTs.getTime() : Date.now(),
      startMs,
      endMs,
      spanMs: endMs - startMs,
      rect,
      active: false,
    };
  }

  function handlePointerMove(event) {
    const state = dragStateRef.current;
    if (!state || state.pointerId !== event.pointerId) return;

    const deltaX = event.clientX - state.startClientX;
    if (!state.active && Math.abs(deltaX) < DRAG_THRESHOLD_PX) {
      return;
    }

    if (!state.active) {
      state.active = true;
      setDragging(true);
      onDragStart?.();
    }

    if (!state.rect.width) return;

    event.preventDefault();
    const msPerPx = state.spanMs / state.rect.width;
    const nextMs = state.startCenterMs - deltaX * msPerPx;
    onPreviewTime?.(new Date(nextMs));
  }

  function finishPointerInteraction(event, cancelled = false) {
    const state = dragStateRef.current;
    if (!state || state.pointerId !== event.pointerId) return;

    dragStateRef.current = null;
    event.currentTarget?.releasePointerCapture?.(event.pointerId);

    if (cancelled) {
      setDragging(false);
      if (state.active) onDragEnd?.(new Date(state.startCenterMs));
      return;
    }

    if (state.active) {
      const deltaX = event.clientX - state.startClientX;
      const msPerPx = state.spanMs / state.rect.width;
      const nextMs = state.startCenterMs - deltaX * msPerPx;
      setDragging(false);
      onDragEnd?.(new Date(nextMs));
      return;
    }

    setDragging(false);
    const clickX = Math.max(0, Math.min(state.rect.width, event.clientX - state.rect.left));
    const ratio = clickX / state.rect.width;
    const targetMs = state.startMs + (state.endMs - state.startMs) * ratio;
    onSelectTime?.(new Date(targetMs));
  }

  return (
    <div className={`chronologyTimelineCard ${compact ? "compact" : ""} ${isPreviewing ? "previewing" : ""}`}>
      <div className="chronologyTimelineFrame">
        <div className="chronologyTimelineHeader">
          <div className="chronologyTimelineZoomControls">
            <button
              className="chronologyTimelineZoomButton"
              onClick={onZoomOut}
              title="Уменьшить диапазон"
            >
              -
            </button>

            <div className="chronologyTimelineZoomLabel">{zoom.label}</div>

            <button
              className="chronologyTimelineZoomButton"
              onClick={onZoomIn}
              title="Увеличить диапазон"
            >
              +
            </button>
          </div>

          <div className="chronologyTimelineHeaderCenter">
            <div className={`chronologyTimelineCenterLabel ${isPreviewing ? "preview" : ""}`}>
              <span className="chronologyTimelineStateText">{isPreviewing ? "Предпросмотр" : "Выбрано"}</span>
              <span>{currentTimeLabel || "—"}</span>
            </div>
          </div>
        </div>

        <div
          ref={rootRef}
          className={`chronologyTimelineBody ${dragging ? "isDragging" : ""}`}
          data-chronology-timeline-pointer="true"
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={(event) => finishPointerInteraction(event)}
          onPointerCancel={(event) => finishPointerInteraction(event, true)}
        >
          <div className="chronologyTimelineTimeLayer">
            <div className="chronologyTimelineAxis">
              {axisMarks.map((mark) => (
                <div
                  key={`${mark.ms}-${mark.isMajor ? "major" : "minor"}`}
                  className={`chronologyTimelineAxisMark ${mark.isMajor ? "major" : "minor"}`}
                  style={{ left: `${mark.leftPct}%` }}
                >
                  <div className="chronologyTimelineAxisLine" />
                  {mark.isMajor && mark.tickLabel ? (
                    <div className={`chronologyTimelineAxisLabel ${mark.tickLabel.multiline ? "multiline" : ""}`}>
                      <span>{mark.tickLabel.primary}</span>
                      {mark.tickLabel.secondary ? <span>{mark.tickLabel.secondary}</span> : null}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>

            <div className="chronologyTimelineCenterLine" />
            {showCommittedMarker ? (
              <div className="chronologyTimelineCommittedMarker" style={{ left: `${committedLeftPct}%` }}>
                <div className="chronologyTimelineCommittedLine" />
                <div className="chronologyTimelineCommittedLabel">
                  <span>Выбрано</span>
                  <span>{committedTimeLabel || "—"}</span>
                </div>
              </div>
            ) : null}
          </div>

          {rangesLoading ? <div className="chronologyTimelineStatus">Загружаем диапазоны архива...</div> : null}
          {rangesError ? <div className="chronologyTimelineStatus error">Диапазоны архива сейчас недоступны</div> : null}
          {showNoRangeState ? <div className="chronologyTimelineStatus">Нет архивных диапазонов в выбранном окне</div> : null}

          {hasSelectedCameras ? (
            <div className="chronologyTimelineTracks">
              {selectedCameraIds.map((cameraId) => {
                const cameraKey = String(cameraId);
                const item = rangesByCamera?.[cameraKey];
                const ranges = item?.ranges || [];
                const cameraName = cameraNames?.[cameraKey] || `Камера ${cameraKey}`;
                let visibleRangesForCamera = 0;

                return (
                  <div className="chronologyTimelineTrackRow" key={cameraKey}>
                    <div className="chronologyTimelineTrackLabel" title={cameraName}>{cameraName}</div>

                    <div className="chronologyTimelineTrackLane">
                      {ranges.map((range, index) => {
                        const rangeStart = parseNaiveDateTime(range.start);
                        const rangeEnd = parseNaiveDateTime(range.end);

                        if (!Number.isFinite(rangeStart) || !Number.isFinite(rangeEnd)) {
                          return null;
                        }

                        if (rangeEnd <= startMs || rangeStart >= endMs) {
                          return null;
                        }
                        visibleRangesForCamera += 1;

                        const clippedStart = Math.max(rangeStart, startMs);
                        const clippedEnd = Math.min(rangeEnd, endMs);
                        const leftPct = ((clippedStart - startMs) / (endMs - startMs)) * 100;
                        const widthPct = ((clippedEnd - clippedStart) / (endMs - startMs)) * 100;

                        return (
                          <div
                            key={index}
                            className="chronologyTimelineRange"
                            style={{
                              left: `${leftPct}%`,
                              width: `${Math.max(widthPct, 0.35)}%`,
                            }}
                            title={`${range.start} — ${range.end}`}
                          />
                        );
                      })}
                      {!rangesLoading && !rangesError && visibleRangesForCamera === 0 ? (
                        <div className="chronologyTimelineTrackEmpty">нет архива</div>
                      ) : null}
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
    </div>
  );
}
