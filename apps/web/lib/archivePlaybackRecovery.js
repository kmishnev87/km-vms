// Rearm consecutive-error recovery only after real playback progress or a
// stable, decoded paused frame. Metadata alone is not a successful recovery.
export function isArchiveRecoveryHealthy(video, startTime, stableReadyMs = 0) {
  if (!video || startTime === null || video.error || Number(video.readyState) < 3) return false;
  return Number(video.currentTime) - Number(startTime) >= 1 || (video.paused && stableReadyMs >= 2000);
}
