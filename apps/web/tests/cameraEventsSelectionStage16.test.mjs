import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { eventIsSelected, filteredSelectionBody, selectedEventCount, snapshotHistoryQuery, toggleSnapshotEvent } from "../lib/cameraEventSelection.js";

const source = readFileSync(new URL("../components/CameraEventsHub.js", import.meta.url), "utf8");
const css = readFileSync(new URL("../app/styles/35-camera-events.css", import.meta.url), "utf8");

test("cross-page selection uses server membership, not whichever rows happen to be visible", () => {
  const first = { id: "snapshot", count: 75, visible_ids: ["first", "second"], excluded_ids: [] };
  assert.equal(selectedEventCount([], first), 75);
  assert.equal(eventIsSelected("first", [], first), true);
  assert.equal(eventIsSelected("newer", [], first), false);
  const unchecked = toggleSnapshotEvent(first, "first", false);
  assert.equal(selectedEventCount([], unchecked), 74);
  assert.equal(eventIsSelected("first", [], unchecked), false);
  assert.equal(selectedEventCount([], toggleSnapshotEvent(unchecked, "first", false)), 74);
  const nextPage = { ...unchecked, visible_ids: ["third", "fourth"] };
  assert.equal(eventIsSelected("third", [], nextPage), true);
  assert.equal(selectedEventCount([], nextPage), 74);
  assert.equal(toggleSnapshotEvent(nextPage, "not-in-snapshot", true), nextPage);
});

test("the selection freezes the original date boundary while moving to another page", () => {
  const query = "limit=25&offset=0&since=2026-11-01T00%3A00%3A00Z&camera_id=12&kind=person&unread=true";
  const snapshot = { id: "fixed", query };
  const next = new URLSearchParams(snapshotHistoryQuery("since=newer", 25, snapshot));
  assert.equal(next.get("since"), "2026-11-01T00:00:00Z");
  assert.equal(next.get("offset"), "25");
  assert.equal(next.get("selection_id"), "fixed");
  assert.deepEqual(filteredSelectionBody(query, "cursor", [{ id: "one" }]), {
    through: "cursor", camera_id: 12, kind: "person", since: "2026-11-01T00:00:00Z", until: null, unread: true, visible_ids: ["one"] });
});

test("all-filter controls keep their space and filter changes invalidate a late selection response", () => {
  assert.match(source, /visibility: selectedCount \? "visible" : "hidden"/);
  assert.match(source, /owner === selectionGeneration\.current/);
  assert.match(source, /function changeFilter[^\n]*clearSelection\(\)/);
  assert.match(css, /\.cameraEventsSelectionSummary[^}]*grid-template-columns: auto minmax\(0, 1fr\)/);
  assert.match(source, /deleteTarget\.ids \|\| deleteTarget\.snapshot \? "cameraEvents\.deleteSelectedConfirm"/);
});

test("history does not misrepresent a saved positive observation as a currently ongoing event", () => {
  assert.doesNotMatch(source, /row\.active|t\("cameraEvents\.active"\)/);
  assert.match(source, /row\.state_unconfirmed/);
});

test("event actions use the existing monitor and clock visual language, not a recording-camera glyph", () => {
  assert.match(source, /data-event-action=\{action\}/);
  assert.match(source, /action === "live"[^\n]*M12 16V20/);
  assert.doesNotMatch(source, /M12 4v12M3 10h18/);
  assert.match(source, /action === "archive"[^\n]*scale\(1\.0625\)[^\n]*<circle cx="10\.5"/);
  assert.doesNotMatch(source, /m15 10 6-3v10l-6-3z/);
});
