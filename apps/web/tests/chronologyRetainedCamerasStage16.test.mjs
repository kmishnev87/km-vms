import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { orderedChronologyCameras, sameChronologyCameraGroup } from "../lib/chronologyCameraCatalog.js";

const cameras = [{ id: 1, name: "Yard", deleted: false }, { id: 2, name: "Old yard", deleted: true }, { id: 3, name: "Door", deleted: false }];
test("retained archives stay below active cameras even with a mixed saved order", () => {
  const values = orderedChronologyCameras(cameras, [], () => [cameras[1], cameras[2], cameras[0]]);
  assert.deepEqual(values.map(item => item.id), [3, 1, 2]);
  assert.equal(sameChronologyCameraGroup(cameras, 1, 2), false);
  assert.equal(sameChronologyCameraGroup(cameras, 1, 3), true);
});
test("chronology requests the archive catalog and draws one group divider without changing Live", () => {
  const chronology = fs.readFileSync(new URL("../app/chronology/page.js", import.meta.url), "utf8");
  const live = fs.readFileSync(new URL("../app/live/page.js", import.meta.url), "utf8");
  assert.match(chronology, /apiFetch\("\/chronology\/cameras"\)/);
  assert.match(chronology, /camera\.deleted && !orderedCameras\[index - 1\]\?\.deleted/);
  assert.match(chronology, /sameChronologyCameraGroup\(cameras, sourceId, targetId\)/);
  assert.match(live, /apiFetch\("\/viewer\/cameras"\)/);
  assert.doesNotMatch(live, /chronology\/cameras/);
});
