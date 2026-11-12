"use client";
import { useEffect, useState } from "react";
import { apiFetch, captureAuthSession } from "./api";
import { CAMERA_EVENT_REFRESH } from "./cameraEvents";

export default function useTimelineCameraEvents(cameraIds, centerMs, hours) {
  const [events, setEvents] = useState([]);
  const [truncated, setTruncated] = useState(false);
  const [revision, setRevision] = useState(0);
  const key = cameraIds.map(String).sort().join(",");
  const hour = Number.isFinite(centerMs) ? Math.floor(centerMs / 3600000) : 0;
  useEffect(() => {
    const refresh = () => setRevision(value => value + 1);
    window.addEventListener(CAMERA_EVENT_REFRESH, refresh);
    return () => window.removeEventListener(CAMERA_EVENT_REFRESH, refresh);
  }, []);
  useEffect(() => {
    if (!key) { setEvents([]); setTruncated(false); return undefined; }
    const controller = new AbortController();
    const current = captureAuthSession();
    const half = Number(hours) * 1800000 + 3600000;
    // Product-calendar coordinates, not browser-local instants. One hour of
    // margin avoids a new backend request for every playback clock tick.
    const from = new Date(hour * 3600000 - half).toISOString().slice(0, 19);
    const to = new Date(hour * 3600000 + half).toISOString().slice(0, 19);
    const params = new URLSearchParams({ camera_ids: key, from, to });
    setEvents([]);
    apiFetch(`/camera-events/markers?${params}`, { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted && current()) { setEvents(value.items || []); setTruncated(Boolean(value.truncated)); }
    }).catch(() => { if (!controller.signal.aborted && current()) { setEvents([]); setTruncated(false); } });
    return () => controller.abort();
  }, [key, hour, hours, revision]);
  return { events, truncated };
}
