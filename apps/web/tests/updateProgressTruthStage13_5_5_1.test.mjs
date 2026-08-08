import assert from "node:assert/strict";
import {
  updateApplyOperatorModel,
  updateApplyProgressText,
} from "../lib/settingsPageHelpers.js";

const t = {
  updateApplyProgressIndeterminate: "In progress…",
  updateApplyHeadlines: { running: "Running", unknown: "Unknown" },
  updateApplySummaries: { running: "Running summary", unknown: "Unknown summary" },
  updateApplyResults: { running: "Running", unknown: "Unknown" },
  updateApplyRecoveryRunning: "Running recovery",
  updateApplyRecoveryUnknown: "Unknown recovery",
  updateApplyReleaseTitleFallback: "Release",
  updateApplyReleaseSummaryFallback: "Summary",
  updateCommitPending: "Pending",
  updateCommitUnavailable: "Unavailable",
  updateCommitVerified: "Verified",
  maintenanceStatuses: {
    unknown: "Unknown",
    acquire_source: "Acquire source",
    extracting: "Extracting",
    request: "Request",
    preflight: "Preflight",
    applying: "Applying",
    health_check: "Health",
    commit_verification: "Verification",
    pending: "Pending",
    running: "Running",
    completed: "Completed",
  },
};

for (const percent of [0, 42, 100]) {
  const status = {
    status: "acquire_source",
    current_step: "acquire_source",
    progress_percent: percent,
    progress_current: percent,
    progress_total: 100,
    progress_unit: "bytes",
  };
  assert.equal(
    updateApplyProgressText(status, t),
    `Acquire source — ${percent}%`,
  );
}

assert.equal(
  updateApplyProgressText(
    { status: "extracting", current_step: "extracting" },
    t,
  ),
  "Extracting — In progress…",
);
assert.equal(
  updateApplyProgressText(
    {
      status: "acquire_source",
      current_step: "acquire_source",
      progress_percent: 50,
      progress_current: 42,
      progress_total: 100,
      progress_unit: "bytes",
    },
    t,
  ),
  "Acquire source — In progress…",
);

const running = {
  status: "acquire_source",
  current_step: "acquire_source",
  progress_percent: 42,
  progress_current: 42,
  progress_total: 100,
  progress_unit: "bytes",
  steps: [{ name: "acquire_source", status: "running" }],
};
const updateStatus = {
  status: "identity_incomplete",
  comparison: { status: "identity_incomplete" },
  installed_release: { version: "0.8.9", metadata_status: "complete" },
  available_release: { version: "0.9.0", commit: "b".repeat(40) },
};
assert.equal(
  updateApplyOperatorModel(updateStatus, running, t, "en").summary,
  "Acquire source — 42%",
);

const terminal = updateApplyOperatorModel(
  { ...updateStatus, status: "current", comparison: { status: "current" } },
  { status: "failed", current_step: "health_check" },
  {
    ...t,
    updateApplyHeadlines: { ...t.updateApplyHeadlines, blocked: "Blocked" },
    updateApplyResults: { ...t.updateApplyResults, blocked: "Blocked" },
    updateApplyRecoveryFailed: "Update failed",
  },
  "en",
);
assert.equal(terminal.summary, "Update failed");
assert.equal(terminal.summary.includes("42%"), false);
