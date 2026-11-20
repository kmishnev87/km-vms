import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../components/CameraEventsHub.js", import.meta.url), "utf8");
const css = readFileSync(new URL("../app/styles/35-camera-events.css", import.meta.url), "utf8");

test("history filters start on the workbench edge, retaining right-side action/scrollbar alignment", () => {
  const rule = css.match(/\.cameraEventsFilters \{([^}]+)\}/)[1];
  assert.match(rule, /padding: 0 calc\(var\(--camera-events-row-padding\) \+ 1px \+ var\(--camera-events-scrollbar, 0px\)\) 0 0;/);
  assert.match(rule, /grid-template-columns: repeat\(4, minmax\(0, 1fr\)\) auto/);
  assert.match(source, /cameraEvents\.camera[\s\S]*cameraEvents\.ai\.source[\s\S]*cameraEvents\.type[\s\S]*cameraEvents\.period/);
  assert.match(source, /const showAiFilters = filters\.source === "ai"/);
  assert.match(source, /\{showAiFilters \? <div className="cameraEventsAiFilters">/);
  assert.doesNotMatch(source, /ruleDraft|cameraEventsTypeFilter|aiFiltersOpen/);
});

test("New stays a checkbox with an independently centered text line, not anonymous flex text", () => {
  assert.match(source, /checked=\{filters\.unread\}[^\n]+<span>\{t\("cameraEvents\.unreadOnly"\)\}<\/span>/);
  assert.match(css, /\.cameraEventsFilterActions \.cameraEventsCheck \{[^}]*height: 36px;[^}]*line-height: 16px;/);
  assert.match(css, /\.cameraEventsFilterActions \.cameraEventsCheck > span \{ display: block; line-height: 16px;/);
  assert.match(source, /changeFilter\("unread", e\.target\.checked\)/);
});
