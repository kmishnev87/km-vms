import assert from "node:assert/strict";
import fs from "node:fs";

const source = fs.readFileSync(new URL('../lib/workspaceContinuity.js', import.meta.url), 'utf8');
const { reconcilePlaybackMembership, selectTimelineLabels, timelineRangeWindow, timelineRangeWindowCovers, timelineRangesNeedReload } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const first = { cameraId: '1', playbackKey: 'existing-one', offsetSec: 12 };
const second = { sourceCameraId: '2', cameraId: null, playbackKey: 'no-video' };
const previous = { a: first, b: second };
let result = reconcilePlaybackMembership([{ id: 'a', cameraId: 1 }], previous);
assert.deepEqual(result.missing, []);
assert.equal(result.retained.a, first);
assert.equal(result.retained.b, undefined);
result = reconcilePlaybackMembership([{ id: 'b', cameraId: 2 }, { id: 'a', cameraId: 1 }], previous);
assert.equal(result.retained.a, first);
assert.equal(result.retained.b, second);
result = reconcilePlaybackMembership([{ id: 'a', cameraId: 3 }], previous);
assert.equal(result.missing.length, 1);
assert.deepEqual(result.retained, {});
for (const width of [160, 240, 320, 600, 960, 1600, 2560]) {
  const marks = Array.from({ length: 49 }, (_, index) => ({ ms: index * 1800000, isMajor: index % 2 === 0, tickLabel: { primary: '12:00' }, leftPct: index / 48 * 100 }));
  const visible = selectTimelineLabels(marks, width);
  const positions = marks.filter((mark) => visible.has(mark.ms)).map((mark) => mark.leftPct * width / 100);
  assert.ok(positions.length >= 1);
  for (let i = 1; i < positions.length; i++) assert.ok(positions[i] - positions[i - 1] >= 55.9);
  assert.ok(positions.every((position) => position >= 28 && position <= width - 28));
}
for (const hours of [24, 72, 168]) {
  const spanMs = hours * 3600_000;
  const center = Date.UTC(2026,10,3,9);
  const desired = (offset = 0) => ({fromMs:center+offset-spanMs/2,toMs:center+offset+spanMs/2,zoomKey:String(hours),cameraKey:'38,88'});
  const loaded = {...timelineRangeWindow(center,spanMs),zoomKey:String(hours),cameraKey:'38,88',fetchedAt:1000};
  for (let sec=0;sec<60;sec++) assert.equal(timelineRangesNeedReload(loaded,desired(sec*1000),1000+sec*1000),false);
  assert.equal(timelineRangesNeedReload(loaded,desired(60_000),61_000),true);
  assert.equal(timelineRangesNeedReload(loaded,desired(),500),true);
  assert.equal(timelineRangesNeedReload(loaded,{...desired(),cameraKey:'38'},2000),true);
  assert.equal(timelineRangesNeedReload(loaded,{...desired(),zoomKey:'different'},2000),true);
  assert.equal(timelineRangesNeedReload(loaded,desired(spanMs),2000),true);
  assert.equal(timelineRangeWindowCovers(loaded,desired(30_000)),true);
}
const component = fs.readFileSync(new URL('../components/ChronologyTimeline.js', import.meta.url),'utf8');
const page = fs.readFileSync(new URL('../app/chronology/page.js', import.meta.url),'utf8');
const css = fs.readFileSync(new URL('../app/styles/80-chronology-timeline.css', import.meta.url),'utf8');
assert.ok(!component.includes('Загружаем диапазоны архива'));
assert.ok(component.includes('aria-busy={rangesLoading}'));
assert.ok(page.includes('timelineRangeWindowCovers(rangesInFlightRef.current, desired)'));
assert.ok(page.includes('{ signal: controller.signal }'));
assert.ok(css.includes('inset: 0 var(--chronology-axis-right, 10px) 0 var(--chronology-axis-left, 188px)'));
assert.ok(css.includes('left: var(--chronology-axis-left, 188px)'));
console.log('Workspace continuity, measured timeline spacing, bounded range refresh and quiet loading PASS');
