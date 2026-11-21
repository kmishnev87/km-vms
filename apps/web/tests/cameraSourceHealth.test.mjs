import assert from "node:assert/strict";
import test from "node:test";
import { buildDashboardStatusSummary, buildOperatorWarnings } from "../lib/operatorWarnings.js";
import { canUserAccessRoute } from "../lib/routePermissions.js";
import { statusView } from "../lib/recordingCenter.js";

test("both camera event and AI source faults open the affected camera in AI and Recording", () => {
  const status = { domains: { cameras: { severity: "warning", items: [], source_issues: [
    { kind: "ai", camera_id: 3, retry_available: true, cameras: [{camera_id: 3, recording_state: "recording"}] },
    { kind: "camera", camera_id: 5, cameras: [{camera_id: 5, recording_state: "emergency"}] },
  ] } } };
  const summary = buildDashboardStatusSummary(status);
  assert.equal(summary.severity, "warning");
  assert.equal(summary.problem_count, 2);
  assert.deepEqual(summary.problems.map(problem => [problem.source_issue.kind, problem.action]), [
    ["ai", { href: "/ai-recording#camera-3", label: "Открыть AI и запись" }],
    ["camera", { href: "/ai-recording#camera-5", label: "Открыть AI и запись" }],
  ]);
  assert.equal(summary.problems[0].source_issue.retry_available, true);
  for (const problem of summary.problems) {
    assert.ok(canUserAccessRoute({permissions: ["manage_cameras"]}, problem.action.href));
    assert.ok(!canUserAccessRoute({permissions: ["run_diagnostics"]}, problem.action.href));
  }
  assert.deepEqual(buildOperatorWarnings(status, { domains: ["cameras"] }), []);
});

test("camera row distinguishes confirmed video, failed analysis, and an ordinary pause", () => {
  const row = (actual_status, extra = {}, enabled = true) => ({ recording_enabled: enabled,
    runtime: { actual_status, ai: { phase: "failed" }, ai_managed: true, ...extra } });
  assert.equal(statusView(row("recording")).key, "status_recording_ai_fault");
  assert.equal(statusView(row("source_fault")).key, "status_ai_fault");
  assert.equal(statusView(row("starting", {emergency_requested: true})).key, "status_starting");
  assert.equal(statusView(row("recording", {emergency_recording: true})).key, "emergency_active");
  assert.equal(statusView(row("recording", {emergency_recording: true}, false)).key, "status_paused");
  assert.equal(statusView(row("events_disabled")).tone, "quiet");
  assert.equal(statusView(row("linked_source_unavailable", {source_status: "source_events_disabled"})).tone, "quiet");
  assert.equal(statusView(row("waiting", {ai: {phase: "healthy"}})).tone, "quiet");
});

test("one reachability card does not masquerade as a subscription fault", () => {
  const camera = (camera_id, dependent = false) => ({ camera_id, dependent });
  const reasons = [
    { kind: "camera", cause: "unreachable", camera_id: 3, cameras: [camera(3), camera(5), camera(11, true)] },
    { kind: "camera", cause: "no_response", camera_id: 7, cameras: [camera(7)] },
    { kind: "camera", cause: "subscription", camera_id: 9, cameras: [camera(9)] },
  ];
  const problems = buildDashboardStatusSummary({ domains: { cameras: { severity: "warning", source_issues: reasons } } }).problems;
  assert.deepEqual(new Map(problems.map(problem => [problem.title, problem.action.href])), new Map([
    ["Камеры недоступны", "/cameras"],
    ["Нет ответа по каналу событий", "/ai-recording#camera-7"],
    ["Сбой приёма событий", "/ai-recording#camera-9"],
  ]));
});
