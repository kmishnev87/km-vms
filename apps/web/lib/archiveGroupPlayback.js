import { requiredArchiveBuffer } from "./archiveBufferPolicy.js";

// Milliseconds use the product calendar coordinate, never the browser timezone.
export function archiveSourceCovers(playback, targetMs) {
  return Boolean(playback?.hasVideo && Number.isFinite(targetMs)
    && Number.isFinite(playback.sourceStartMs) && Number.isFinite(playback.sourceEndMs)
    && targetMs >= playback.sourceStartMs && targetMs < playback.sourceEndMs);
}
export function archiveTargetOffset(playback, targetMs) {
  return Number.isFinite(targetMs) && Number.isFinite(playback?.sourceStartMs)
    ? Math.max(0, (targetMs - playback.sourceStartMs) / 1000) : Math.max(0, Number(playback?.offsetSec || 0));
}
export function archivePositionMs(playback, mediaTime) {
  return Number.isFinite(playback?.sourceStartMs) && Number.isFinite(Number(mediaTime))
    ? playback.sourceStartMs + Number(mediaTime) * 1000 : null;
}
export function archiveBufferedAhead(video, target = Number(video?.currentTime || 0)) {
  for(let index=0; index<(video?.buffered?.length || 0); index+=1) {
    if(video.buffered.start(index)<=target+0.05 && video.buffered.end(index)>=target)
      return Math.max(0,video.buffered.end(index)-target);
  }
  return 0;
}
export function archiveTargetReady(video, target, { resume = false, speed = 1, reserve = 0.5, capacity = Infinity } = {}) {
  if (!video || video.error || video.seeking || Number(video.readyState) < (resume ? 3 : 2)) return false;
  if (Math.abs(Number(video.currentTime || 0) - target) > 0.2) return false;
  if (!resume) return true;
  const remaining = Number.isFinite(video.duration) ? video.duration - target : Infinity;
  const ahead = requiredArchiveBuffer({reserve,speed,remaining,capacity});
  if (!video.buffered) return false;
  for (let i = 0; i < video.buffered.length; i += 1) {
    if (video.buffered.start(i) <= target + 0.05 && video.buffered.end(i) >= target + ahead) return true;
  }
  return false;
}
export function confirmedArchiveEmpty(playback) {
  return playback?.lookupState === "empty" || (playback?.availabilityStatus === "no_recording" && !playback?.hasVideo);
}
