// Group only the archive UI. A removed camera must never be put back into Live.
export function partitionArchiveCameras(items, isDeleted = item => item.deleted) {
  const active = [], archived = [];
  for (const item of Array.isArray(items) ? items : []) (isDeleted(item) ? archived : active).push(item);
  return { active, archived };
}

export function recordingCameraOptions(data) {
  if (Array.isArray(data?.camera_items)) return data.camera_items.map(item => ({ ...item, byId: true }));
  // Compatibility with a NAS before the additive grouped-catalog contract.
  const retained = new Map((data?.export_items || []).map(item => [item.name, item]));
  return (data?.items || []).map(name => ({ name, value: name, deleted: retained.get(name)?.deleted === true, byId: false }));
}
