export const ALL_RECORDING_CAMERAS = "__all__";

export function resolveEffectiveRecordingCamera(selectedCamera, cameraOptions) {
  const selected = String(selectedCamera || ALL_RECORDING_CAMERAS);
  if (selected === ALL_RECORDING_CAMERAS) return ALL_RECORDING_CAMERAS;
  const available = new Set((cameraOptions || []).map((camera) => String(camera || "")));
  return available.has(selected) ? selected : ALL_RECORDING_CAMERAS;
}
