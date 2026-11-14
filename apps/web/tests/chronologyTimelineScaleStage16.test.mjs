import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const moduleFrom = async name => import(`data:text/javascript;base64,${Buffer.from(fs.readFileSync(new URL(`../lib/${name}.js`, import.meta.url), "utf8")).toString("base64")}`);
const { TIMELINE_ZOOM_KEYS, TIMELINE_ZOOM_HOURS, timelineScale, timelineAxis, recordingRangeTarget } = await moduleFrom("chronologyTimelineScale");
const { selectTimelineLabels } = await moduleFrom("workspaceContinuity");

test("six shared scales preserve 24h default and give range requests the exact visible span", () => {
  assert.deepEqual(TIMELINE_ZOOM_KEYS, ["3h", "6h", "12h", "24h", "3d", "7d"]);
  assert.equal(timelineScale("unknown").key, "24h");
  for (const key of TIMELINE_ZOOM_KEYS) {
    const { startMs, endMs } = timelineAxis(Date.UTC(2026, 10, 15, 0, 7), timelineScale(key));
    assert.equal(endMs - startMs, TIMELINE_ZOOM_HOURS[key] * 3600000);
    assert.equal((startMs + endMs) / 2, Date.UTC(2026, 10, 15, 0, 7));
  }
});

test("12h/6h/3h use hour/half-hour/quarter-hour labels, with aligned minor ticks across midnight", () => {
  for (const [key, major, minor] of [["12h", 60, 30], ["6h", 30, 15], ["3h", 15, 5]]) {
    const scale = timelineScale(key);
    const axis = timelineAxis(Date.UTC(2026, 10, 15, 0, 7), scale);
    assert.equal(scale.majorEveryMinutes, major);
    assert.equal(scale.minorEveryMinutes, minor);
    for (const tick of axis.axisMarks) {
      assert.ok(tick.ms >= axis.startMs && tick.ms <= axis.endMs);
      assert.equal(tick.ms % (minor * 60000), 0);
      if (tick.isMajor) {
        assert.match(tick.tickLabel.primary, /^\d{2}:\d{2}$/);
        assert.equal(tick.tickLabel.multiline, false);
        assert.equal(tick.ms % (major * 60000), 0);
      } else assert.equal(tick.tickLabel, null);
    }
    for (const width of [180, 390, 800, 1400]) {
      const labels = selectTimelineLabels(axis.axisMarks, width);
      const x = axis.axisMarks.filter(t => labels.has(t.ms)).map(t => t.leftPct * width / 100);
      assert.ok(x.length > 0);
      x.slice(1).forEach((value, i) => assert.ok(value - x[i] >= 56 - 1e-6));
    }
  }
});

test("painted minimum-width subsecond/short recording never seeks into an adjacent gap", () => {
  for (const duration of [300, 1505, 60_000]) for (const ratio of [-1, 0, .5, 1, 2]) {
    const start = Date.UTC(2026, 10, 14, 21, 11, 9);
    const target = recordingRangeTarget(start, start + duration, ratio);
    assert.ok(target >= start && target < start + duration);
  }
});

for (const section of ["live", "chronology"]) test(`${section}: Add all enables frames, preserves existing tiles and does not add deleted cameras`, () => {
  const source = fs.readFileSync(new URL(`../app/${section}/page.js`, import.meta.url), "utf8");
  const fn = source.slice(source.indexOf("  function addAllCameras()"), source.indexOf("  function handleDrop("));
  const existing = { id: "existing", cameraId: "1", stream: "main", muted: false };
  const retained = { id: "retained", cameraId: "9" };
  let tiles = [existing, retained], frame = false;
  const ctx = { cameras: [{ id: 1 }, { id: 2 }, ...(section === "chronology" ? [{ id: 3, deleted: true }] : [])],
    setFrameLayoutActive: value => frame = value, setTiles: update => tiles = update(tiles), activeLayoutTiles: value => value,
    workspaceCameraIds: value => new Set(value.map(t => t.cameraId)), normalizeTile: t => t, nextZIndex: () => 3,
    DEFAULT_W_PCT: .5, DEFAULT_H_PCT: .5, defaultStream: () => "sub" };
  vm.runInNewContext(fn + ";addAllCameras();", ctx);
  assert.equal(frame, true);
  assert.deepEqual(Array.from(tiles, t => t.cameraId), ["1", "9", "2"]);
  assert.equal(tiles[0], existing);
  assert.equal(tiles[1], retained);
  vm.runInNewContext("addAllCameras();", ctx);
  assert.equal(tiles.length, 3, "Add all must be idempotent");
});
