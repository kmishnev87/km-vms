import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { partitionArchiveCameras, recordingCameraOptions } from "../lib/archiveCameraGroups.js";
const read = path => readFileSync(new URL(path, import.meta.url), "utf8");

test("active and retained groups preserve names, identities and independent order", () => {
  const values = [{id:1,name:"Same",deleted:true,value:"camera:1"},{id:2,name:"Same",deleted:false,value:"camera:2"}];
  const options = recordingCameraOptions({camera_items:values});
  const groups = partitionArchiveCameras(options);
  assert.equal(groups.active[0].value,"camera:2");
  assert.equal(groups.archived[0].value,"camera:1");
  assert(options.every(item => item.byId));
  assert(values[0].deleted, "never mutate the source into an active camera");
});
test("older NAS name filters remain literal, including a token-like camera name", () => {
  const options = recordingCameraOptions({items:["camera:12","Old"],export_items:[{id:"1",name:"Old",deleted:true}]});
  assert.deepEqual(options.map(item => [item.value,item.deleted,item.byId]), [["camera:12",false,false],["Old",true,false]]);
});
test("Recordings uses a real compact, keyboard-operable grouped selector", () => {
  const selector=read("../components/ArchiveCameraSelect.js");
  for(const marker of ['role="listbox"','role="option"','role="separator"','ArrowDown','ArrowUp','Escape']) assert(selector.includes(marker));
  assert.match(read("../app/recordings/page.js"), /selected\?\.byId\) params\.set\("camera_id"/);
  assert.match(read("../app/recordings/page.js"), /camera_id: Number\(cameraItems/);
  assert.match(read("../app/styles/40-09-storage-migration-integrity.css"), /\.archiveCameraOption[^}]*height: 30px/);
});
test("Storage groups BEFORE pagination and retains equal inline compact rows on small screens", () => {
  const source=read("../app/storage/page.js"), css=read("../app/styles/40-09-storage-migration-integrity.css");
  assert(source.indexOf('partitionArchiveCameras(cameraStorageRows') < source.indexOf('cameraRows.slice'));
  assert.match(source,/className="storageArchivedDivider"/);
  assert.match(source,/className="storageCompactCameraRow"/);
  assert.doesNotMatch(source,/className="storageOpsCameraCards"/);
  assert.match(css,/\.storageOpsCompactCameras \.storageOpsCameraTable \{ display: block/);
  assert.match(css,/\.storageOpsCompactCameras[^}]*height: 30px/);
});
test("Events use shared columns and account for the real scrollbar, not only equal button boxes", () => {
  const source=read("../components/CameraEventsHub.js"),css=read("../app/styles/35-camera-events.css");
  for(const marker of ['.cameraEventsSelection {','.cameraEventsRow {']) {
    const body=css.slice(css.indexOf(marker)).split('}')[0];assert(body.includes('grid-template-columns: var(--camera-events-columns)'));
  }
  assert.match(source,/node\.offsetWidth - node\.clientWidth/);
  assert.match(css,/grid-template-columns: repeat\(3, minmax\(0, 1fr\)\) auto/);
  assert.match(css,/\.cameraEventsTotal[^}]*grid-column: 4/);
  assert.match(css,/\.cameraEventsFilterActions \.cameraEventsCheck[^}]*white-space: nowrap/);
});
