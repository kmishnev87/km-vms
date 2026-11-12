"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { createCoalescedPlaybackResolver } from "./coalescedPlaybackResolver";
import { reconcilePlaybackMembership } from "./workspaceContinuity";
import { archiveSourceCovers } from "./archiveGroupPlayback";

async function boundedLookup(url, signal) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(abort, 12000);
  try { return await apiFetch(url, { signal: controller.signal }); }
  finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
}

function sourceDuration(response) {
  if (Number(response?.duration_sec) > 0) return Number(response.duration_sec);
  const utc = value => Date.parse(/(?:Z|[+-]\d\d:\d\d)$/.test(value || "") ? value : `${value}Z`);
  return Math.max(0, (utc(response?.file_end) - utc(response?.file_start)) / 1000) || 0;
}

export default function useChronologySources({ tilesRef, tileSourceKey, currentTs, isPlaying, isScrubbingRef, formatProductTimestampParam }) {
  const playbackMapRef = useRef({});
  const [playbackMap, setPlaybackMap] = useState({});
  const playbackResolverRef = useRef(null);
  if (!playbackResolverRef.current) playbackResolverRef.current = createCoalescedPlaybackResolver();

  async function fetchPlaybackForTile(tile, ts, forceReload = false, targetMs = currentTs?.getTime(), signal, automatic = false) {
    const prev = playbackMapRef.current[tile.id];
    const sameCamera = String(prev?.sourceCameraId || prev?.cameraId || "") === String(tile.cameraId || "");
    if (!forceReload && sameCamera && archiveSourceCovers(prev, targetMs)) {
      return automatic ? prev : { ...prev, offsetSec: Math.max(0, (targetMs - prev.sourceStartMs) / 1000) };
    }
    if (!forceReload && automatic && sameCamera && prev?.lookupState === "empty"
      && Date.now() < prev.retryAfterMs && targetMs >= prev.requestedMs && targetMs < prev.requestedMs + 3000) return prev;

    if (!tile.cameraId) {
      return {
        hasVideo: false,
        lookupState: "empty",
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey: forceReload || !prev ? `${tile.id}-empty-${Date.now()}` : prev.playbackKey,
      };
    }

    try {
      const response = await boundedLookup(
        `/chronology/playback?camera_id=${encodeURIComponent(tile.cameraId)}&ts=${encodeURIComponent(ts)}`, signal,
      );

      const hasVideo = Boolean(response?.has_video);
      const relPath = hasVideo ? (response?.rel_path || null) : null;
      const segmentId = response?.segment_id || null;
      const archiveRootId = response?.archive_root_id || null;
      const playbackRef = response?.playback_ref || null;
      const availabilityStatus = String(response?.availability_status || (hasVideo ? "available" : "no_recording"));
      const offsetSec = hasVideo ? Number(response?.offset_sec || 0) : 0;
      const cameraId = String(tile.cameraId);
      const containerFormat = hasVideo ? String(response?.container_format || "").toLowerCase() : "";
      const fileExtension = hasVideo ? String(response?.file_extension || "").toLowerCase() : "";
      const mimeType = hasVideo ? String(response?.mime_type || "").toLowerCase() : "";
      const lookupState = hasVideo ? "available" : availabilityStatus === "no_recording" ? "empty" : "error";
      const sourceStartMs = hasVideo && Number.isFinite(targetMs) ? targetMs - offsetSec * 1000 : null;
      const durationSec = hasVideo ? sourceDuration(response) : 0;

      const sameSource =
        !forceReload &&
        prev &&
        Boolean(prev.hasVideo) === hasVideo &&
        String(prev.cameraId || "") === String(cameraId || "") &&
        String(prev.relPath || "") === String(relPath || "") &&
        String(prev.segmentId || "") === String(segmentId || "") &&
        String(prev.archiveRootId || "") === String(archiveRootId || "");

      return {
        hasVideo,
        lookupState,
        sourceStartMs,
        sourceEndMs: Number.isFinite(sourceStartMs) && durationSec > 0 ? sourceStartMs + durationSec * 1000 : null,
        durationSec,
        requestedMs: targetMs,
        retryAfterMs: Date.now() + 3000,
        cameraId,
        relPath,
        segmentId,
        archiveRootId,
        playbackRef,
        availabilityStatus,
        offsetSec,
        containerFormat,
        fileExtension,
        mimeType,
        playbackKey: sameSource
          ? prev.playbackKey
          : `${tile.id}-${cameraId || "empty"}-${segmentId || relPath || "empty"}-${archiveRootId || "root"}-${availabilityStatus}-${offsetSec}-${containerFormat || fileExtension || "unknown"}-${Date.now()}`,
      };
    } catch (error) {
      if (signal?.aborted) throw error;
      if (sameCamera && archiveSourceCovers(prev, targetMs)) return prev;
      return {
        hasVideo: false,
        lookupState: "error",
        availabilityStatus: "lookup_error",
        cameraId: String(tile.cameraId),
        relPath: null,
        offsetSec: 0,
        playbackKey: `${tile.id}-error-${Date.now()}`,
      };
    }
  }

  async function resolvePlaybackForTimestamp(ts, forceReload = false, automatic = false, targetMs = currentTs?.getTime()) {
    const sourceTiles = tilesRef.current;
    return playbackResolverRef.current.resolve({
      automatic,
      run: signal => Promise.all(sourceTiles.map(async (tile) => {
        const value = await fetchPlaybackForTile(tile, ts, forceReload, targetMs, signal, automatic);
        return [tile.id, value === playbackMapRef.current[tile.id] ? value : { ...value, sourceCameraId: String(tile.cameraId) }];
      })),
      apply: (results) => {
        const nextMap = Object.fromEntries(results);
        if (Object.keys(nextMap).length === Object.keys(playbackMapRef.current).length
          && Object.keys(nextMap).every(id => nextMap[id] === playbackMapRef.current[id])) return;
        playbackMapRef.current = nextMap;
        setPlaybackMap(nextMap);
      },
    });
  }

  useEffect(() => {
    // Membership is not a seek. Keep every unchanged player's source/key and
    // current position; only resolve newly added or reassigned tiles.
    playbackResolverRef.current.invalidate();
    const sourceTiles = tilesRef.current;
    const { retained, missing } = reconcilePlaybackMembership(sourceTiles, playbackMapRef.current);
    playbackMapRef.current = retained;
    setPlaybackMap(retained);
    if (!currentTs || !missing.length) return;
    playbackResolverRef.current.resolve({
      run: signal => Promise.all(missing.map(async (tile) => [tile.id, { ...await fetchPlaybackForTile(tile, formatProductTimestampParam(currentTs), false, currentTs.getTime(), signal), sourceCameraId: String(tile.cameraId) }])),
      apply: (results) => {
        const next = { ...retained, ...Object.fromEntries(results) };
        playbackMapRef.current = next;
        setPlaybackMap(next);
      },
    });
  }, [tileSourceKey]);

  useEffect(() => {
    if (!isPlaying || !currentTs || isScrubbingRef.current) return;
    resolvePlaybackForTimestamp(formatProductTimestampParam(currentTs), false, true, currentTs.getTime());
  }, [currentTs, isPlaying]);

  useEffect(() => () => playbackResolverRef.current.invalidate(), []);
  return { playbackMap, playbackMapRef, playbackResolverRef, resolvePlaybackForTimestamp };
}
