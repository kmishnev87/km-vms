export function visibleWorkspaceTiles(tiles, cameras) {
  const cameraIds = new Set((Array.isArray(cameras) ? cameras : []).map((camera) => String(camera?.id || "")));
  return (Array.isArray(tiles) ? tiles : []).filter((tile) => cameraIds.has(String(tile?.cameraId || "")));
}

export function workspaceCameraIds(tiles) {
  return new Set((Array.isArray(tiles) ? tiles : []).map((tile) => String(tile?.cameraId || "")).filter(Boolean));
}
