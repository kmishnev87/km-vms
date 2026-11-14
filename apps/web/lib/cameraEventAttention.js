// Local attention only: viewing here must never mute another client or phone.
export function cameraIsBeingWatched(cameraId, doc) {
  const id = String(cameraId ?? "");
  if (!/^[1-9]\d*$/.test(id) || !doc || doc.visibilityState !== "visible") return false;
  const viewport = doc.defaultView;
  for (const element of doc.querySelectorAll(`[data-render-context="live"][data-live-camera-id="${id}"]`)) {
    if (element.getAttribute("data-watch-playing") !== "true") continue;
    if (doc.fullscreenElement && !doc.fullscreenElement.contains(element)) continue;
    const video = element.querySelector("video");
    if (!video || video.paused || video.ended || video.error || video.readyState < 2) continue;
    const bounds = element.getBoundingClientRect();
    const left = Math.max(0, bounds.left), top = Math.max(0, bounds.top);
    const right = Math.min(viewport?.innerWidth || 0, bounds.right);
    const bottom = Math.min(viewport?.innerHeight || 0, bounds.bottom);
    if (right <= left || bottom <= top) continue;
    // Overlapping tiles/modals are not watched merely because a player exists.
    for (const [x, y] of [[.5, .5], [.25, .25], [.75, .75]]) {
      const visible = doc.elementFromPoint(left + (right-left)*x, top + (bottom-top)*y);
      if (visible && element.contains(visible)) return true;
    }
  }
  return false;
}

export function silenceWatchedCameraEvent(event, doc) {
  const kinds = [event?.kind, event?.rule_kind, ...(event?.object_kinds || [])];
  // A critical new alarm remains independent of ordinary activity/viewing.
  if (kinds.some(kind => kind === "fire" || kind === "tamper")) return false;
  return cameraIsBeingWatched(event?.camera_id, doc);
}
