export function orderedChronologyCameras(cameras, order, mergeOrder) {
  const ordered = mergeOrder(cameras, order);
  return [...ordered.filter(camera => !camera.deleted), ...ordered.filter(camera => camera.deleted)];
}

export function sameChronologyCameraGroup(cameras, first, second) {
  const a = cameras.find(camera => String(camera.id) === String(first));
  const b = cameras.find(camera => String(camera.id) === String(second));
  return Boolean(a && b && Boolean(a.deleted) === Boolean(b.deleted));
}
