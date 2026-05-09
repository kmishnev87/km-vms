import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = fs
  .readFileSync(resolve(__dirname, "../lib/workspaceLayoutCore.js"), "utf8")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${source}
this.visibleWorkspaceTiles = visibleWorkspaceTiles;
this.workspaceCameraIds = workspaceCameraIds;
this.resizeWorkspaceTile = resizeWorkspaceTile;`,
  context
);

const { visibleWorkspaceTiles, workspaceCameraIds, resizeWorkspaceTile } = context;

const tiles = [
  { id: "visible-live", cameraId: "1", stream: "sub", xPct: 0.44 },
  { id: "missing-live", cameraId: "404", stream: "main", xPct: 0.12 },
  { id: "visible-chronology", cameraId: 2, xPct: 0.72 },
];
const cameras = [{ id: 1 }, { id: "2" }];

assert.deepEqual(
  visibleWorkspaceTiles(tiles, cameras).map((tile) => tile.id),
  ["visible-live", "visible-chronology"]
);

assert.equal(workspaceCameraIds(visibleWorkspaceTiles(tiles, cameras)).has("404"), false);
assert.equal(workspaceCameraIds(visibleWorkspaceTiles(tiles, cameras)).has("1"), true);
assert.equal(workspaceCameraIds(visibleWorkspaceTiles(tiles, cameras)).has("2"), true);
assert.deepEqual(visibleWorkspaceTiles(tiles, []), []);
assert.deepEqual([...workspaceCameraIds(null)], []);

const baseResize = {
  startX: 100,
  startY: 100,
  tileX: 0.2,
  tileY: 0.2,
  tileW: 0.4,
  tileH: 0.3,
  workspaceW: 1000,
  workspaceH: 1000,
  minWPct: 0.1,
  minHPct: 0.1,
};

function roundedResize(corner, pointer) {
  const result = resizeWorkspaceTile(null, { ...baseResize, corner }, pointer);
  return Object.fromEntries(Object.entries(result).map(([key, value]) => [key, Number(value.toFixed(6))]));
}

assert.deepEqual(
  roundedResize("bottom-right", { clientX: 200, clientY: 150 }),
  { xPct: 0.2, yPct: 0.2, wPct: 0.5, hPct: 0.35 }
);

assert.deepEqual(
  roundedResize("top-left", { clientX: 0, clientY: 50 }),
  { xPct: 0.1, yPct: 0.15, wPct: 0.5, hPct: 0.35 }
);

assert.deepEqual(
  roundedResize("top-right", { clientX: 300, clientY: 0 }),
  { xPct: 0.2, yPct: 0.1, wPct: 0.6, hPct: 0.4 }
);

assert.deepEqual(
  roundedResize("bottom-left", { clientX: 500, clientY: 500 }),
  { xPct: 0.5, yPct: 0.2, wPct: 0.1, hPct: 0.7 }
);

assert.deepEqual(
  roundedResize("top-left", { clientX: 900, clientY: 800 }),
  { xPct: 0.5, yPct: 0.4, wPct: 0.1, hPct: 0.1 }
);
