import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = fs
  .readFileSync(resolve(__dirname, "../lib/operatorWarnings.js"), "utf8")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${source}\nthis.buildOperatorWarnings = buildOperatorWarnings;\nthis.buildDashboardStatusSummary = buildDashboardStatusSummary;\nthis.userCanReadRuntimeStatus = userCanReadRuntimeStatus;\nthis.isRuntimeStatusAccessDenied = isRuntimeStatusAccessDenied;\nthis.shouldStopRuntimeStatusPolling = shouldStopRuntimeStatusPolling;`,
  context
);
const {
  buildOperatorWarnings,
  buildDashboardStatusSummary,
  userCanReadRuntimeStatus,
  isRuntimeStatusAccessDenied,
  shouldStopRuntimeStatusPolling,
} = context;

function runtime(domains) {
  return { domains };
}

function ids(payload, options) {
  return Array.from(buildOperatorWarnings(runtime(payload), options).map((item) => item.id));
}

assert.deepEqual(
  ids({ storage: { severity: "error", available: false, reason_codes: ["storage_unavailable"] } }),
  ["storage-unavailable"]
);

assert.deepEqual(
  ids({ storage: { severity: "warning", writable: false, reason_codes: ["storage_unwritable", "storage_low_space"] } }),
  ["storage-unwritable", "storage-low-space"]
);

assert.deepEqual(
  ids({ recorder: { severity: "error", safe_reason_codes: ["recorder_heartbeat_stale"] } }),
  ["recorder-stale"]
);

assert.deepEqual(
  ids({
    cameras: {
      items: [
        { severity: "ok", reason_codes: ["disabled"] },
        { severity: "warning", reason_codes: ["no_evidence"] },
        { severity: "error", reason_codes: ["recording_failed"] },
      ],
    },
  }),
  ["camera-recording-errors", "camera-recording-warnings"]
);

assert.deepEqual(
  ids({
    live: {
      items: [
        { severity: "unknown", state: "unknown", running: false, ready: false, reason_codes: ["no_evidence"] },
        { severity: "ok", reason_codes: ["not_applicable"] },
      ],
    },
  }),
  []
);

const neutralOverview = buildDashboardStatusSummary(runtime({
  cameras: { severity: "ok", items: [] },
  live: {
    severity: "unknown",
    items: [
      { severity: "unknown", state: "unknown", running: false, ready: false, reason_codes: ["no_evidence"] },
      { severity: "ok", reason_codes: ["not_requested"] },
    ],
  },
  recorder: { severity: "ok" },
  storage: { severity: "ok" },
  retention: { severity: "ok" },
  reconciliation: { severity: "ok" },
}));
assert.equal(neutralOverview.severity, "ok");
assert.equal(neutralOverview.problem_count, 0);
assert.equal(neutralOverview.rows.find((row) => row.domain === "live").severity, "ok");
assert.equal(JSON.stringify(neutralOverview).includes("Онлайн"), true);

assert.deepEqual(
  ids({
    live: {
      items: [
        { severity: "warning", state: "starting", running: true, ready: false, reason_codes: ["live_starting"] },
        { severity: "error", state: "failed", running: false, ready: false, reason_codes: ["live_failed"] },
      ],
    },
  }),
  ["live-stream-errors", "live-stream-starting"]
);

assert.deepEqual(
  ids({ retention: { severity: "warning", reason_codes: ["retention_completed_with_warnings"] } }),
  ["retention-warning"]
);

assert.deepEqual(
  ids({
    reconciliation: {
      severity: "warning",
      reason_codes: ["reconciliation_problems_found", "cleanup_candidates_present"],
      problem_file_count: 3,
      cleanup_candidate_count: 2,
    },
  }),
  ["reconciliation-cleanup", "reconciliation-problems"]
);

const bounded = buildOperatorWarnings(runtime({
  storage: { severity: "error", available: false, reason_codes: ["storage_unavailable"] },
  recorder: { severity: "error", safe_reason_codes: ["recorder_heartbeat_stale"] },
  cameras: { items: [{ severity: "warning", reason_codes: ["recording_stale"] }] },
}), { limit: 2 });
assert.equal(bounded.length, 2);
assert.deepEqual(Array.from(bounded.map((item) => item.id)), ["storage-unavailable", "recorder-stale"]);

const rendered = JSON.stringify(buildOperatorWarnings(runtime({
  live: { items: [{ severity: "error", state: "failed", running: false, ready: false, reason_codes: ["live_failed"], command: ["ff", "mpeg -i rt", "sp://user:se", "cret@example.test/live"].join("") }] },
})));
assert.equal(rendered.includes(["rt", "sp://"].join("")), false);
assert.equal(rendered.includes(["se", "cret"].join("")), false);
assert.equal(rendered.includes(["ff", "mpeg"].join("")), false);

const warningOverview = buildDashboardStatusSummary(runtime({
  storage: { severity: "warning", reason_codes: ["storage_low_space"] },
  cameras: {
    items: [
      { severity: "error", reason_codes: ["recording_failed"] },
      { severity: "ok", reason_codes: ["disabled"] },
    ],
  },
  live: { items: [{ severity: "ok", reason_codes: ["not_applicable"] }] },
}));
assert.equal(warningOverview.severity, "error");
assert.equal(warningOverview.problem_count, 2);
assert.equal(
  JSON.stringify(warningOverview.problems.map((item) => [item.domain_label, item.action?.href]).slice(0, 2)),
  JSON.stringify([["Камеры", "/cameras"], ["Хранилище", "/settings"]])
);
assert.equal(JSON.stringify(warningOverview).includes("rtsp://"), false);
assert.equal(JSON.stringify(warningOverview).includes("Authorization"), false);
assert.equal(JSON.stringify(warningOverview).includes("debug"), false);

assert.equal(userCanReadRuntimeStatus({ permissions: ["run_diagnostics"] }), true);
assert.equal(userCanReadRuntimeStatus({ permissions: ["view_live"] }), false);
assert.equal(userCanReadRuntimeStatus(null), false);

for (const message of [
  "HTTP 401",
  "HTTP 403",
  "Not authenticated",
  "Invalid token",
  "Forbidden",
  "Section unavailable. User permissions are limited.",
]) {
  assert.equal(isRuntimeStatusAccessDenied(new Error(message)), true);
  assert.equal(shouldStopRuntimeStatusPolling(new Error(message)), true);
}

assert.equal(shouldStopRuntimeStatusPolling(new Error("Network failed")), false);
