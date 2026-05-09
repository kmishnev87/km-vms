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
this.workspaceCameraIds = workspaceCameraIds;`,
  context
);

const { visibleWorkspaceTiles, workspaceCameraIds } = context;

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
