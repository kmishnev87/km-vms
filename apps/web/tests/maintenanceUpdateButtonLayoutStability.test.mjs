import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { updateApplyOperatorModel } from "../lib/settingsUpdateApplyHelpers.js";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const surface = read("../components/SettingsMaintenanceSurface.js");
const controller = read("../lib/settingsMaintenanceController.js");
const css = read("../app/styles/20-07-maintenance-final-layout.css");
const panel = surface.slice(surface.indexOf('<section className="settingsUpdateApplyPanel">'));
const compact = panel.indexOf('<div className="settingsUpdateApplyCompact">');
const action = panel.indexOf('className="settingsUpdateApplyActions settingsUpdateApplyPrimaryAction"');

test("apply action is always rendered below the header, beside the installed release", () => {
  assert.ok(compact > 0 && action > compact);
  assert.doesNotMatch(panel.slice(0, compact), /onClick=\{startUpdateApply\}/);
  assert.doesNotMatch(surface, /updateApplyOperator\.showApplyButton/);
  const primary = panel.slice(action, panel.indexOf("</section>"));
  assert.match(primary, /onClick=\{startUpdateApply\} disabled=\{!updateApplyAllowed\}/);
  assert.doesNotMatch(primary.slice(0, primary.indexOf("<button")), /\?\s*\(/);
  assert.match(primary, /updateApplyRunning \|\| updateApplyHasUnknownLaunch \|\| updateApplyOperator\.stateUnknown/);
});

test("compact row reserves a permanent action column and stacks on narrow screens", () => {
  assert.match(css, /\.settingsUpdateApplyCompact\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) auto;/);
  assert.match(css, /\.settingsUpdateApplyPrimaryAction\s*\{\s*align-self:\s*start;/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.settingsUpdateApplyCompact,[^{]*\{\s*grid-template-columns:\s*1fr;/);
  assert.match(css, /\.settingsUpdateApplyPanel \.settingsMaintenanceHeaderTools\s*\{\s*display:\s*grid;\s*grid-template-columns:\s*minmax\(0, 1fr\) auto;\s*justify-self:\s*stretch;/);
  assert.match(css, /\.settingsUpdateApplyPanel \.settingsMaintenanceHeaderTools > \.settingsUpdateApplyActions\s*\{\s*grid-column:\s*2;\s*grid-row:\s*1;/);
});

const allowedExpression = controller.match(/const updateApplyAllowed = Boolean\(([\s\S]*?)\n  \);/);
assert.ok(allowedExpression);
const isAllowed = new Function(
  "updateApplyOperator", "updateApplyPending", "updateApplyDialog", "maintenanceBusy", "currentRestorePending",
  `return Boolean(${allowedExpression[1]});`,
);
const copy = { maintenanceStatuses: {}, updateApplyHeadlines: {}, updateApplySummaries: {} };
const installed = { version: "0.9.10", metadata_status: "complete", commit_sha: "a".repeat(40) };
const base = { status: "current", check_state: "checked", can_apply_from_ui: false, installed_release: installed };
const available = { ...base, status: "update_available", comparison: { status: "update_available" },
  can_apply_from_ui: true, available_release: { version: "0.9.11", commit: "b".repeat(40) } };
const model = (status, apply = { status: "idle" }, context = {}) => updateApplyOperatorModel(status, apply, copy, "en", context);
const allowed = (operator, overrides = {}) => isAllowed(
  operator, overrides.pending || null, overrides.dialog || null, overrides.busy || "", overrides.restore || null,
);

test("visible does not mean enabled: unchecked, current, blocked and failed checks stay disabled", () => {
  for (const status of [null, base, { ...base, check_state: "not_checked" },
    { ...base, status: "blocked" }, { ...base, status: "check_failed" }]) {
    assert.equal(allowed(model(status)), false);
  }
  assert.equal(allowed(model(available)), true);
});

test("checking, running, unknown launch, confirmation and restore retain their launch guards", () => {
  const candidate = model(available);
  for (const override of [{ busy: "update" }, { busy: "update-apply" }, { busy: "backup-create" },
    { pending: {} }, { dialog: {} }, { restore: {} }]) {
    assert.equal(allowed(candidate, override), false);
  }
  assert.equal(allowed(model(available, { status: "preflight" })), false);
  assert.equal(allowed(model(available, { status: "idle", is_stale: true })), false);
  assert.equal(allowed(model(available, { status: "idle" }, { unresolvedSubmission: true })), false);
});

test("a fresh trusted candidate retains the supported recovery path after a live check failure", () => {
  const snapshot = { ...base, status: "check_failed", last_check_status: "check_failed",
    trusted_apply_candidate: { fresh: true, can_apply_from_ui: true, latest: { version: "0.9.11", commit: "b".repeat(40) } } };
  assert.equal(allowed(model(snapshot)), true);
  assert.equal(allowed(model({ ...snapshot, trusted_apply_candidate: { ...snapshot.trusted_apply_candidate, fresh: false } })), false);
});
