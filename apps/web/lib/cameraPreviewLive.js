// A preview is a camera id, not an event id or a persisted workspace change.
export function cameraPreviewId(search) {
  const value = new URLSearchParams(search || "").get("camera_preview");
  return /^[1-9]\d{0,9}$/.test(value || "") && Number(value) <= 2147483647 ? value : null;
}

export function cameraPreviewLiveUrl(id) {
  const value = String(id);
  return cameraPreviewId(`camera_preview=${value}`) ? `/live?camera_preview=${value}` : null;
}
