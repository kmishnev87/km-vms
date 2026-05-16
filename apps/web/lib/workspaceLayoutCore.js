export function visibleWorkspaceTiles(tiles, cameras) {
  const cameraIds = new Set((Array.isArray(cameras) ? cameras : []).map((camera) => String(camera?.id || "")));
  return (Array.isArray(tiles) ? tiles : []).filter((tile) => cameraIds.has(String(tile?.cameraId || "")));
}

export function workspaceCameraIds(tiles) {
  return new Set((Array.isArray(tiles) ? tiles : []).map((tile) => String(tile?.cameraId || "")).filter(Boolean));
}

export const SIDEBAR_CAMERA_REORDER_MIME = "application/x-km-vms-sidebar-camera-reorder";
export const LIVE_CAMERA_DROP_MIME = "application/x-camera-id";
export const LIVE_CAMERA_STREAM_DROP_MIME = "application/x-camera-stream";
export const CHRONOLOGY_CAMERA_DROP_MIME = "application/x-chronology-camera-id";

export function compareCamerasByName(a, b) {
  const nameA = String(a?.name || "").localeCompare(String(b?.name || ""), undefined, { sensitivity: "base", numeric: true });
  if (nameA !== 0) return nameA;
  return String(a?.id || "").localeCompare(String(b?.id || ""), undefined, { numeric: true });
}

export function sanitizeSidebarCameraOrder(order) {
  if (!Array.isArray(order)) return [];
  const seen = new Set();
  const result = [];
  order.forEach((value) => {
    if (value && typeof value === "object") return;
    const id = String(value || "").trim();
    if (!id || seen.has(id)) return;
    seen.add(id);
    result.push(id);
  });
  return result;
}

export function mergeSidebarCameraOrder(cameras, savedOrder) {
  const source = Array.isArray(cameras) ? cameras : [];
  const byId = new Map(source.map((camera) => [String(camera?.id || ""), camera]).filter(([id]) => Boolean(id)));
  const cleanOrder = sanitizeSidebarCameraOrder(savedOrder);
  const ordered = [];
  const used = new Set();

  cleanOrder.forEach((id) => {
    const camera = byId.get(id);
    if (!camera || used.has(id)) return;
    used.add(id);
    ordered.push(camera);
  });

  const missing = source
    .filter((camera) => {
      const id = String(camera?.id || "");
      return id && !used.has(id);
    })
    .sort(compareCamerasByName);

  return ordered.length ? [...ordered, ...missing] : [...source].sort(compareCamerasByName);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function resizeWorkspaceTile(tile, resizeState, pointer) {
  const workspaceW = Math.max(Number(resizeState?.workspaceW || 0), 1);
  const workspaceH = Math.max(Number(resizeState?.workspaceH || 0), 1);
  const minWPct = clamp(Number(resizeState?.minWPct || 0.05), 0.01, 1);
  const minHPct = clamp(Number(resizeState?.minHPct || 0.05), 0.01, 1);
  const corner = String(resizeState?.corner || "bottom-right");

  const dxPct = (Number(pointer?.clientX || 0) - Number(resizeState?.startX || 0)) / workspaceW;
  const dyPct = (Number(pointer?.clientY || 0) - Number(resizeState?.startY || 0)) / workspaceH;
  const startX = clamp(Number(resizeState?.tileX ?? tile?.xPct ?? 0), 0, 1);
  const startY = clamp(Number(resizeState?.tileY ?? tile?.yPct ?? 0), 0, 1);
  const startW = clamp(Number(resizeState?.tileW ?? tile?.wPct ?? minWPct), minWPct, 1 - startX);
  const startH = clamp(Number(resizeState?.tileH ?? tile?.hPct ?? minHPct), minHPct, 1 - startY);
  const startRight = clamp(startX + startW, minWPct, 1);
  const startBottom = clamp(startY + startH, minHPct, 1);

  let xPct = startX;
  let yPct = startY;
  let wPct = startW;
  let hPct = startH;

  if (corner.includes("left")) {
    const nextLeft = clamp(startX + dxPct, 0, startRight - minWPct);
    xPct = nextLeft;
    wPct = startRight - nextLeft;
  } else if (corner.includes("right")) {
    const nextRight = clamp(startRight + dxPct, startX + minWPct, 1);
    wPct = nextRight - startX;
  }

  if (corner.includes("top")) {
    const nextTop = clamp(startY + dyPct, 0, startBottom - minHPct);
    yPct = nextTop;
    hPct = startBottom - nextTop;
  } else if (corner.includes("bottom")) {
    const nextBottom = clamp(startBottom + dyPct, startY + minHPct, 1);
    hPct = nextBottom - startY;
  }

  return {
    xPct: clamp(xPct, 0, 1 - minWPct),
    yPct: clamp(yPct, 0, 1 - minHPct),
    wPct: clamp(wPct, minWPct, 1 - xPct),
    hPct: clamp(hPct, minHPct, 1 - yPct),
  };
}
