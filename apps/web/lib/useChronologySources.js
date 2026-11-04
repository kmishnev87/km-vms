"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { createCoalescedPlaybackResolver } from "./coalescedPlaybackResolver";
import { reconcilePlaybackMembership } from "./workspaceContinuity";

export default function useChronologySources({ tilesRef, tileSourceKey, currentTs, isPlaying, isScrubbingRef, formatProductTimestampParam }) {
  const playbackMapRef = useRef({});
  const [playbackMap, setPlaybackMap] = useState({});
  const playbackResolverRef = useRef(null);
  if (!playbackResolverRef.current) playbackResolverRef.current = createCoalescedPlaybackResolver();

  async function fetchPlaybackForTile(tile, ts, forceReload = false) {
    const prev = playbackMapRef.current[tile.id];

    if (!tile.cameraId) {
      return {
        hasVideo: false,
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey: forceReload || !prev ? `${tile.id}-empty-${Date.now()}` : prev.playbackKey,
      };
    }

    try {
      const response = await apiFetch(
        `/chronology/playback?camera_id=${encodeURIComponent(tile.cameraId)}&ts=${encodeURIComponent(ts)}`
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
    } catch (_) {
      return {
        hasVideo: false,
        cameraId: null,
        relPath: null,
        offsetSec: 0,
        playbackKey: `${tile.id}-error-${Date.now()}`,
      };
    }
  }

  async function resolvePlaybackForTimestamp(ts, forceReload = false, automatic = false) {
    const sourceTiles = tilesRef.current;
    return playbackResolverRef.current.resolve({
      automatic,
      run: () => Promise.all(sourceTiles.map(async (tile) => [tile.id, {
        ...await fetchPlaybackForTile(tile, ts, forceReload), sourceCameraId: String(tile.cameraId),
      }])),
      apply: (results) => {
        const nextMap = Object.fromEntries(results);
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
      run: () => Promise.all(missing.map(async (tile) => [tile.id, { ...await fetchPlaybackForTile(tile, formatProductTimestampParam(currentTs), false), sourceCameraId: String(tile.cameraId) }])),
      apply: (results) => {
        const next = { ...retained, ...Object.fromEntries(results) };
        playbackMapRef.current = next;
        setPlaybackMap(next);
      },
    });
  }, [tileSourceKey]);

  useEffect(() => {
    if (!isPlaying || !currentTs || isScrubbingRef.current) return;
    resolvePlaybackForTimestamp(formatProductTimestampParam(currentTs), false, true);
  }, [currentTs, isPlaying]);

  useEffect(() => () => playbackResolverRef.current.invalidate(), []);
  return { playbackMap, playbackMapRef, playbackResolverRef, resolvePlaybackForTimestamp };
}
