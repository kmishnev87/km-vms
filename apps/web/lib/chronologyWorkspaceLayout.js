import { sanitizeSidebarCameraOrder } from "./workspaceLayoutCore.js";

const STORAGE_KEY = "vms_chronology_workspace_v1";
const LEGACY_STORAGE_KEY = "vms_chronology" + "2_workspace_v1";
export const WORKSPACE_KEY = "chronology";
const MIGRATION_MARKER_PREFIX = `${STORAGE_KEY}_backend_migrated`;
const LEGACY_CANVAS_W = 1600;
const LEGACY_CANVAS_H = 900;
const MIN_TILE_W = 220;
const MIN_TILE_H = 150;
const DEFAULT_W_PCT = 0.32;
const DEFAULT_H_PCT = 0.34;

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeTile(tile) {
  const hasPercent =
    Number.isFinite(tile?.xPct) &&
    Number.isFinite(tile?.yPct) &&
    Number.isFinite(tile?.wPct) &&
    Number.isFinite(tile?.hPct);

  if (hasPercent) {
    const wPct = clamp(Number(tile.wPct), 0.05, 1);
    const hPct = clamp(Number(tile.hPct), 0.05, 1);
    return {
      id: String(tile.id || `${tile.cameraId || "camera"}-${Date.now()}`),
      cameraId: String(tile.cameraId || ""),
      xPct: clamp(Number(tile.xPct), 0, 1 - wPct),
      yPct: clamp(Number(tile.yPct), 0, 1 - hPct),
      wPct,
      hPct,
      z: Number(tile.z || 2),
    };
  }

  const wPct = clamp(Number(tile?.w || 520) / LEGACY_CANVAS_W, 0.05, 1);
  const hPct = clamp(Number(tile?.h || 300) / LEGACY_CANVAS_H, 0.05, 1);
  return {
    id: String(tile?.id || `${tile?.cameraId || "camera"}-${Date.now()}`),
    cameraId: String(tile?.cameraId || ""),
    xPct: clamp(Number(tile?.x || 0) / LEGACY_CANVAS_W, 0, 1 - wPct),
    yPct: clamp(Number(tile?.y || 0) / LEGACY_CANVAS_H, 0, 1 - hPct),
    wPct,
    hPct,
    z: Number(tile?.z || 2),
  };
}

function readSavedTiles() {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(STORAGE_KEY) || localStorage.getItem(LEGACY_STORAGE_KEY) || "[]";
    const parsed = JSON.parse(raw);
    const tiles = Array.isArray(parsed) ? dedupeTiles(parsed.map(normalizeTile)) : [];
    if (!localStorage.getItem(STORAGE_KEY) && localStorage.getItem(LEGACY_STORAGE_KEY)) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(tiles));
    }
    return tiles;
  } catch {
    return [];
  }
}

function migrationMarkerKey(userId) {
  return `${MIGRATION_MARKER_PREFIX}_${String(userId || "anonymous")}`;
}

function saveTiles(tiles) {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(dedupeTiles(tiles.map(normalizeTile))));
  } catch {}
}

function dedupeTiles(tiles) {
  const seen = new Set();
  return tiles.filter((tile) => {
    const cameraId = String(tile?.cameraId || "");
    if (!cameraId) return true;
    if (seen.has(cameraId)) return false;
    seen.add(cameraId);
    return true;
  });
}

function nextZIndex(tiles) {
  return tiles.reduce((max, tile) => Math.max(max, Number(tile.z || 2)), 2) + 1;
}

function chooseAutoGrid(count, workspaceW, workspaceH) {
  if (count <= 1) return { cols: 1, rows: 1 };

  let best = { cols: count, rows: 1, score: -Infinity };
  const workspaceRatio = workspaceW / Math.max(workspaceH, 1);

  for (let cols = 1; cols <= count; cols += 1) {
    const rows = Math.ceil(count / cols);
    const cellRatio = (workspaceRatio * rows) / cols;
    const areaScore = 1 / (cols * rows);
    const shapePenalty = Math.abs(Math.log(Math.max(cellRatio, 0.01) / (16 / 9)));
    const emptyPenalty = (cols * rows - count) * 0.08;
    const score = areaScore - shapePenalty * 0.03 - emptyPenalty;

    if (score > best.score) {
      best = { cols, rows, score };
    }
  }

  return best;
}

function layoutTiles(source, workspaceBounds) {
  const tiles = dedupeTiles(source);
  if (!tiles.length) return tiles;

  const workspaceW = workspaceBounds?.width || 16;
  const workspaceH = workspaceBounds?.height || 9;
  const { cols, rows } = chooseAutoGrid(tiles.length, workspaceW, workspaceH);
  const wPct = 1 / cols;
  const hPct = 1 / rows;

  return tiles.map((tile, idx) =>
    normalizeTile({
      ...tile,
      xPct: (idx % cols) * wPct,
      yPct: Math.floor(idx / cols) * hPct,
      wPct,
      hPct,
      z: idx + 2,
    })
  );
}

function backendPayload(tiles, sidebarCameraOrder = []) {
  return {
    layout_version: 1,
    tiles: dedupeTiles(tiles.map(normalizeTile)),
    sidebarCameraOrder: sanitizeSidebarCameraOrder(sidebarCameraOrder),
  };
}

export { normalizeTile, readSavedTiles, migrationMarkerKey, saveTiles, dedupeTiles, nextZIndex, chooseAutoGrid, layoutTiles, backendPayload, clamp, MIN_TILE_W, MIN_TILE_H, DEFAULT_W_PCT, DEFAULT_H_PCT };
