export function visibleWorkspaceTiles(tiles, cameras) {
  const cameraIds = new Set((Array.isArray(cameras) ? cameras : []).map((camera) => String(camera?.id || "")));
  return (Array.isArray(tiles) ? tiles : []).filter((tile) => cameraIds.has(String(tile?.cameraId || "")));
}

export function workspaceCameraIds(tiles) {
  return new Set((Array.isArray(tiles) ? tiles : []).map((tile) => String(tile?.cameraId || "")).filter(Boolean));
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
