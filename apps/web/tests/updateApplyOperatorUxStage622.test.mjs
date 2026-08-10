import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  updateApplyOperatorModel,
  updateApplyRecoveryText,
  updateApplyStepRows,
} from "../lib/settingsPageHelpers.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const settingsPage = fs.readFileSync(resolve(webRoot, "app/settings/page.js"), "utf8");
const css = fs.readFileSync(resolve(webRoot, "app/styles/20-settings-maintenance.css"), "utf8");

function cssRule(selector) {
  const start = css.lastIndexOf(`${selector} {`);
  assert.notEqual(start, -1, `${selector} rule not found`);
  const end = css.indexOf("\n}", start);
  assert.notEqual(end, -1, `${selector} rule is not closed`);
  return css.slice(start, end + 2);
}

const t = {
  updateCurrent: "Current",
  updateCommitPending: "Pending",
  updateCommitUnavailable: "No data",
  updateCommitVerified: "Commit verified",
  updateApplyStepDone: "Done",
  updateApplyTimelineCurrent: "Current version",
  updateApplyHeadlines: {
    current: "System is current",
    available: "Update available",
    running: "Update is running",
    completed: "Completed successfully",
    blocked: "Attention required",
  },
  updateApplySummaries: {
    current: "Installed version matches the published release.",
    available: "Review the release.",
    running: "Running.",
    completed: "Installed and verified.",
    blocked: "Blocked.",
  },
  updateApplyReleaseTitleFallback: "Published release",
  updateApplyReleaseSummaryFallback: "Release notes are not localized.",
  updateApplyRecoveryUnknown: "Unknown",
  updateApplyRecoveryRunning: "Running",
  updateApplyRecoveryProvider: "Provider-specific recovery",
  updateApplyRecoveryLiveCheckFailedWithSnapshot: "Live failed but snapshot available",
  updateApplyRecoveryRefreshRequired: "Refresh-required recovery",
  updateApplyRecoveryMissingCommit: "Missing commit recovery",
  updateApplyRecoveryIdentity: "Identity-specific recovery",
  updateApplyRecoveryInstalledNewer: "Installed-newer recovery",
  maintenanceLabels: {
    current: "Current version",
    available: "Available version",
    releaseTitle: "Release",
    releaseSummary: "Changes",
    status: "Status",
    source: "Source",
    installedCommit: "Installed commit",
    targetCommit: "Target commit",
    gitHead: "Git HEAD",
    metadataSource: "Metadata",
    releaseIdentity: "Release identity",
    provider: "Provider",
    verification: "Commit check",
    elapsed: "Elapsed",
  },
  maintenanceStatuses: {
    current: "Current",
    update_available: "Update available",
    completed: "Completed",
    failed: "Failed",
    pending: "Pending",
    request: "Request",
    preflight: "Preflight",
    applying: "Updating",
    health_check: "Testing",
    commit_verification: "Commit check",
    unknown: "Unknown",
  },
};

assert.equal(settingsPage.includes("settingsUpdateApplyHero"), false);
assert.equal(settingsPage.includes("settingsUpdateApplyCompact"), true);
assert.equal(settingsPage.includes("settingsMaintenanceOverall"), true);
assert.equal(settingsPage.includes("settingsUpdateApplySummaryGrid"), true);
assert.equal(settingsPage.includes("settingsUpdateApplySupport"), false);
assert.equal(settingsPage.includes("settingsMaintenanceModalHeader"), true);
assert.equal(settingsPage.includes("settingsMaintenanceBackupManager"), true);
assert.equal(settingsPage.includes('className="settingsMaintenanceBackupCreate"'), false);
assert.equal(settingsPage.includes("<details className=\"settingsUpdateApplyTechnical\">"), false);
assert.equal(settingsPage.includes("updateApplyOperator.showApplyButton"), true);
assert.equal(settingsPage.includes("raw JSON"), false);
assert.equal(settingsPage.includes("helper logs"), false);
assert.equal(settingsPage.includes('name="token"'), false);
assert.equal(settingsPage.includes('name="url"'), false);
assert.equal(settingsPage.includes('name="repo"'), false);
assert.equal(settingsPage.includes("reason is shown above"), false);
assert.equal(settingsPage.includes("The reason is shown in this panel"), false);
assert.equal(settingsPage.includes("причина показана в блоке выше"), false);
assert.equal(settingsPage.includes("Причина показана в этом блоке"), false);

for (const label of ["updateApplyHeadlines", "updateApplySummaries", "updateApplyHistoryLimited", "updateApplySupportTitle", "updateApplySupportAction"]) {
  assert.equal(settingsPage.includes(label), true);
}

assert.equal(css.includes(".settingsUpdateApplyHero"), true);
assert.equal(css.includes(".settingsUpdateApplyTimelineDot"), true);
assert.equal(css.includes(".settingsUpdateApplyTimeline li.is-idle"), true);
assert.equal(css.includes(".settingsMaintenanceModalHeader"), true);
assert.equal(css.includes(".settingsMaintenanceBackupManager"), true);
assert.equal(css.includes("settingsUpdateApplyHero.is-blocked"), true);
assert.equal(settingsPage.includes("settingsUpdateApplyVersionRows"), false);
assert.equal(css.includes(".settingsUpdateApplyVersionRows"), false);
assert.equal(settingsPage.includes("settingsUpdateApplyHeroPrimaryValue"), false);
assert.equal(css.includes(".settingsUpdateApplyHeroPrimaryValue"), false);
assert.match(cssRule(".settingsUpdateApplyCompact"), /grid-template-columns: minmax\(0, 1fr\) auto/);
assert.equal(css.includes(".settingsUpdateApplySupport"), false);
assert.equal(settingsPage.includes("<OperationDialog"), true);
assert.equal(css.includes("overflow-wrap: anywhere"), true);
assert.equal(settingsPage.includes("settingsUpdateApplyProgressRule"), false);
assert.equal(css.includes(".settingsUpdateApplyProgressRule"), false);
assert.equal(settingsPage.includes("{t.updateApplyLastState}"), false);
assert.match(cssRule(".settingsUpdateApplySummaryGrid"), /grid-template-columns: minmax\(0, 1fr\)/);
assert.equal(settingsPage.includes("updateApplyOperator.releaseChangelog.map"), true);
assert.match(
  settingsPage,
  /updateApplyInstalledVersion[\s\S]*?updateApplyAvailableVersion[\s\S]*?updateApplyPublishedAt[\s\S]*?releaseTitle/,
);

assert.equal(updateApplyRecoveryText("provider_unavailable", {}, t), "Provider-specific recovery");
assert.equal(updateApplyRecoveryText("trusted_snapshot_stale", {}, t), "Refresh-required recovery");
assert.equal(updateApplyRecoveryText("manifest_commit_changed", {}, t), "Refresh-required recovery");
assert.equal(updateApplyRecoveryText("trusted_commit_missing", {}, t), "Missing commit recovery");

const current = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: {
      version: "0.7.4",
      title: "Stage 6.2.1 Targeted Timeout Progress Step Dofix",
      summary: "Public update foundation and reliability hardening.",
      commit: "a".repeat(40),
      installed_at: "2026-07-02T05:02:09Z",
      metadata_status: "adopted",
    },
    available_release: {
      version: "0.7.4",
      title: "Stage 6.2.1 Targeted Timeout Progress Step Dofix",
      summary: "Public update foundation and reliability hardening.",
      commit: "a".repeat(40),
    },
  },
  { status: "idle", apply_history: { state: "missing" } },
  t,
  "en",
);

assert.equal(current.headline, "System is current");
assert.equal(current.severity, "ok");
assert.equal(current.canApply, false);
assert.equal(current.showApplyButton, false);
assert.equal(current.commitStatus, "Commit verified");
assert.equal(current.timeline.length, 5);
assert.equal(current.timeline.every((step) => step.status === "idle" && step.timeLabel === ""), true);
assert.equal(current.detailUnavailable, true);

const available = updateApplyOperatorModel(
  {
    status: "update_available",
    can_apply_from_ui: true,
    comparison: { status: "update_available" },
    installed_release: { version: "0.7.3", commit: "a".repeat(40) },
    available_release: {
      version: "0.7.4",
      published_at: "2026-07-31T08:00:00Z",
      title: "Long release title that should wrap inside the panel without clipping or raw hash noise",
      summary: "Operator UX redesign.",
      commit: "b".repeat(40),
    },
  },
  { status: "idle" },
  t,
  "en",
);

assert.equal(available.headline, "Update available");
assert.equal(available.severity, "warning");
assert.equal(available.canApply, true);
assert.equal(available.showApplyButton, true);
assert.equal(available.availableVersion, "0.7.4");
assert.notEqual(available.publishedAt, "");
assert.equal(available.targetCommitShort, "bbbbbbbbbbbb...");
assert.equal(available.timeline.every((step) => step.status === "idle"), true);

const availableWithoutPublishedDate = updateApplyOperatorModel(
  {
    status: "update_available",
    can_apply_from_ui: true,
    comparison: { status: "update_available" },
    installed_release: { version: "0.7.3", commit: "a".repeat(40) },
    available_release: { version: "0.7.4", commit: "b".repeat(40) },
  },
  { status: "idle" },
  t,
  "en",
);
assert.equal(availableWithoutPublishedDate.availableVersion, "0.7.4");
assert.equal(availableWithoutPublishedDate.publishedAt, "");
assert.equal(current.availableVersion, "");

const providerUnavailable = updateApplyOperatorModel(
  {
    status: "provider_unavailable",
    can_apply_from_ui: false,
    comparison: { status: "provider_unavailable" },
    installed_release: { version: "0.7.6", commit: "a".repeat(40) },
    available_release: { version: "0.7.6", commit: "a".repeat(40) },
  },
  { status: "idle" },
  t,
  "en",
);

assert.equal(providerUnavailable.headline, "Attention required");
assert.equal(providerUnavailable.severity, "blocked");
assert.equal(providerUnavailable.summary, "Provider-specific recovery");
assert.notEqual(providerUnavailable.summary, t.updateApplySummaries.blocked);

const failedLiveCheckWithSnapshot = updateApplyOperatorModel(
  {
    status: "check_failed",
    can_apply_from_ui: true,
    comparison: { status: "check_failed", can_apply_from_ui: true, trusted_apply_candidate_status: "fresh" },
    installed_release: { version: "0.7.3", commit: "a".repeat(40) },
    trusted_apply_candidate: {
      available: true,
      fresh: true,
      can_apply_from_ui: true,
      freshness: { age_seconds: 30, fresh_for_seconds: 900 },
      latest: {
        version: "0.7.4",
        title: "Trusted snapshot release",
        summary: "Snapshot remains usable after provider timeout.",
        commit: "b".repeat(40),
        source_ref: "v0.7.4",
      },
    },
  },
  { status: "idle" },
  t,
  "en",
);

assert.equal(failedLiveCheckWithSnapshot.headline, "Update available");
assert.equal(failedLiveCheckWithSnapshot.severity, "warning");
assert.equal(failedLiveCheckWithSnapshot.summary, "Live failed but snapshot available");
assert.equal(failedLiveCheckWithSnapshot.canApply, true);
assert.equal(failedLiveCheckWithSnapshot.showApplyButton, true);
assert.equal(failedLiveCheckWithSnapshot.availableVersion, "0.7.4");
assert.equal(failedLiveCheckWithSnapshot.targetCommitShort, "bbbbbbbbbbbb...");
assert.equal(failedLiveCheckWithSnapshot.timeline.length, 5);

const projectedFailedCheckWithSnapshot = updateApplyOperatorModel(
  {
    status: "update_available",
    last_check_status: "check_failed",
    can_apply_from_ui: true,
    comparison: { status: "update_available", can_apply_from_ui: true },
    installed_release: { version: "0.7.3", commit: "a".repeat(40) },
    trusted_apply_candidate: {
      available: true,
      fresh: true,
      can_apply_from_ui: true,
      freshness: { age_seconds: 35, fresh_for_seconds: 900 },
      latest: {
        version: "0.7.4",
        title: "Trusted snapshot release",
        summary: "Snapshot remains usable after provider timeout.",
        commit: "b".repeat(40),
        source_ref: "v0.7.4",
      },
    },
  },
  { status: "idle" },
  t,
  "en",
);

assert.equal(projectedFailedCheckWithSnapshot.headline, "Update available");
assert.equal(projectedFailedCheckWithSnapshot.severity, "warning");
assert.equal(projectedFailedCheckWithSnapshot.summary, "Live failed but snapshot available");
assert.equal(projectedFailedCheckWithSnapshot.canApply, true);

const completedWithSteps = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: { version: "0.7.4", commit: "a".repeat(40) },
    available_release: { version: "0.7.4", commit: "a".repeat(40) },
  },
  {
    status: "completed",
    expected_commit: "a".repeat(40),
    installed_commit: "a".repeat(40),
    commit_verified: true,
    release_identity: { metadata_status: "complete" },
    steps: [
      { name: "request", status: "completed" },
      { name: "preflight", status: "completed" },
      { name: "applying", status: "completed" },
      { name: "health_check", status: "completed" },
      { name: "commit_verification", status: "completed" },
    ],
  },
  t,
  "en",
);

assert.equal(completedWithSteps.headline, "Completed successfully");
assert.equal(completedWithSteps.timeline.every((step) => step.status === "completed" && step.timeLabel === ""), true);

const failedWithSteps = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: { version: "0.7.4", commit: "a".repeat(40) },
    available_release: { version: "0.7.4", commit: "a".repeat(40) },
  },
  {
    status: "failed",
    steps: [
      { name: "queued", status: "completed" },
      { name: "preflight", status: "completed" },
      { name: "overlay", status: "failed" },
      { name: "health_check", status: "pending" },
      { name: "commit_verification", status: "pending" },
    ],
  },
  t,
  "en",
);

assert.deepEqual(
  failedWithSteps.timeline.map((step) => step.status),
  ["completed", "completed", "failed", "pending", "pending"],
);

const oldFailedHistory = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: { version: "0.7.4", commit: "a".repeat(40) },
    available_release: { version: "0.7.4", commit: "a".repeat(40) },
  },
  {
    status: "idle",
    last_apply_summary: {
      status: "failed",
      steps: [
        { name: "queued", status: "completed" },
        { name: "preflight", status: "failed" },
      ],
    },
  },
  t,
  "en",
);

assert.equal(oldFailedHistory.timeline.every((step) => step.status === "idle"), true);

const ruFallback = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: { version: "0.7.4", commit: "a".repeat(40) },
    available_release: {
      version: "0.7.4",
      title: "Stage 6.3.0 Settings Maintenance",
      summary: "English-only release notes.",
      commit: "a".repeat(40),
    },
  },
  { status: "idle" },
  t,
  "ru",
);

assert.equal(ruFallback.releaseTitle, "Published release");
assert.equal(ruFallback.releaseSummary, "Release notes are not localized.");

const localizedCurrent = updateApplyOperatorModel(
  {
    status: "current",
    can_apply_from_ui: false,
    comparison: { status: "current" },
    installed_release: {
      version: "0.8.5",
      commit_sha: "c".repeat(40),
      installed_at: "2026-07-30T08:00:00Z",
      metadata_status: "complete",
      identity_validity: "valid",
      title: "Plain current title",
      summary: "Plain current summary",
      changelog: ["Plain current change"],
      title_i18n: {
        en: "English current title",
        ru: "Русское название",
        "zh-CN": "中文标题",
      },
      summary_i18n: {
        en: "English current summary",
        ru: "Русское описание",
        "zh-CN": "中文说明",
      },
      changelog_i18n: {
        en: ["English current change"],
        ru: ["Русское изменение"],
        "zh-CN": ["中文变更"],
      },
    },
    available_release: {
      version: "0.8.5",
      title: "Must not replace installed notes",
      summary: "Must not replace installed summary",
      commit_sha: "c".repeat(40),
    },
  },
  { status: "idle" },
  t,
  "ru",
);

assert.equal(localizedCurrent.releaseTitle, "Русское название");
assert.equal(localizedCurrent.releaseSummary, "Русское описание");
assert.deepEqual(localizedCurrent.releaseChangelog, ["Русское изменение"]);

const localizedAvailable = updateApplyOperatorModel(
  {
    status: "update_available",
    can_apply_from_ui: true,
    comparison: { status: "update_available" },
    installed_release: {
      version: "0.8.5",
      commit_sha: "c".repeat(40),
    },
    available_release: {
      version: "0.8.6",
      commit_sha: "d".repeat(40),
      title: "Plain candidate title",
      summary: "s".repeat(600),
      changelog: ["Plain candidate change"],
      title_i18n: {
        en: "English candidate title",
        ru: "Русский кандидат",
        "zh-CN": "中文候选版本",
      },
      summary_i18n: {
        en: "English candidate summary",
        ru: "Русское описание кандидата",
        "zh-CN": "中文候选版本说明",
      },
      changelog_i18n: {
        en: ["English candidate change"],
        ru: ["Русское изменение кандидата"],
        "zh-CN": ["中文候选版本变更"],
      },
    },
  },
  { status: "idle" },
  t,
  "zh-CN",
);

assert.equal(localizedAvailable.releaseTitle, "中文候选版本");
assert.equal(localizedAvailable.releaseSummary, "中文候选版本说明");
assert.deepEqual(localizedAvailable.releaseChangelog, ["中文候选版本变更"]);

const plainLongSummary = updateApplyOperatorModel(
  {
    status: "update_available",
    can_apply_from_ui: true,
    comparison: { status: "update_available" },
    installed_release: { version: "0.8.5", commit_sha: "c".repeat(40) },
    available_release: {
      version: "0.8.6",
      commit_sha: "d".repeat(40),
      title: "Plain candidate title",
      summary: "s".repeat(600),
    },
  },
  { status: "idle" },
  t,
  "ru",
);

assert.equal(plainLongSummary.releaseTitle, "Plain candidate title");
assert.equal(plainLongSummary.releaseSummary.length, 600);
assert.deepEqual(plainLongSummary.releaseChangelog, []);

const releaseNotesModel = (release) => updateApplyOperatorModel(
  {
    status: "update_available",
    can_apply_from_ui: true,
    comparison: { status: "update_available" },
    installed_release: { version: "0.8.5", commit_sha: "c".repeat(40) },
    available_release: {
      version: "0.8.6",
      commit_sha: "d".repeat(40),
      title: "Candidate",
      ...release,
    },
  },
  { status: "idle" },
  t,
  "en",
);

const exactDuplicateNotes = releaseNotesModel({
  summary: "Same text",
  changelog: [" Same text ", "Other"],
});
assert.equal(exactDuplicateNotes.releaseSummary, "Same text");
assert.deepEqual(exactDuplicateNotes.releaseChangelog, ["Other"]);

const changelogOnlyNotes = releaseNotesModel({
  changelog: ["Only changelog"],
});
assert.equal(changelogOnlyNotes.releaseSummary, "");
assert.deepEqual(changelogOnlyNotes.releaseChangelog, ["Only changelog"]);

const absentNotes = releaseNotesModel({});
assert.equal(absentNotes.releaseSummary, "Release notes are not localized.");
assert.deepEqual(absentNotes.releaseChangelog, []);

const nonExactNotes = releaseNotesModel({
  summary: "Same text",
  changelog: ["same text", "Same text.", "Same text"],
});
assert.deepEqual(nonExactNotes.releaseChangelog, ["same text", "Same text."]);
assert.equal(
  settingsPage.includes("updateApplyOperator.releaseSummary ?"),
  true,
);

const installedDate = "2026-07-30T08:00:00Z";
const finishedDate = "2026-07-30T09:00:00Z";
const dateStatus = {
  status: "current",
  can_apply_from_ui: false,
  comparison: { status: "current" },
  installed_release: {
    version: "0.8.5",
    commit_sha: "e".repeat(40),
    installed_at: installedDate,
    metadata_status: "complete",
    identity_validity: "valid",
  },
};
const formatEnglishDate = (value) => new Intl.DateTimeFormat("en-US", {
  year: "2-digit",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
}).format(new Date(value));
const dateModel = (summary) => updateApplyOperatorModel(
  dateStatus,
  { status: "idle", last_apply_summary: summary },
  t,
  "en",
);
const matchingSummary = {
  expected_commit: "e".repeat(40),
  installed_commit: "e".repeat(40),
  finished_at: finishedDate,
};

assert.equal(
  dateModel({ ...matchingSummary, status: "failed", commit_verified: true }).finishedAt,
  formatEnglishDate(installedDate),
);
assert.equal(
  dateModel({ ...matchingSummary, status: "blocked", commit_verified: true }).finishedAt,
  formatEnglishDate(installedDate),
);
assert.equal(
  dateModel({ ...matchingSummary, status: "completed" }).finishedAt,
  formatEnglishDate(installedDate),
);
assert.equal(
  dateModel({ ...matchingSummary, status: "completed", commit_verified: true }).finishedAt,
  formatEnglishDate(finishedDate),
);
assert.equal(
  dateModel({
    ...matchingSummary,
    status: "completed",
    commit_verified: true,
    expected_commit: "e".repeat(12),
  }).finishedAt,
  formatEnglishDate(installedDate),
);
assert.equal(
  updateApplyOperatorModel(
    {
      ...dateStatus,
      installed_release: {
        ...dateStatus.installed_release,
        installed_at: "not-a-date",
      },
    },
    {
      status: "idle",
      last_apply_summary: {
        ...matchingSummary,
        status: "failed",
        commit_verified: true,
        finished_at: "also-not-a-date",
      },
    },
    t,
    "en",
  ).finishedAt,
  "-",
);

const internalSteps = [
  { name: "queued", status: "completed" },
  { name: "preflight", status: "completed" },
  { name: "acquire_source", status: "completed" },
  { name: "extracting", status: "completed" },
  { name: "validating_source", status: "completed" },
  { name: "overlay", status: "completed" },
  { name: "compose_config", status: "completed" },
  { name: "rebuilding", status: "running" },
  { name: "restarting", status: "pending" },
  { name: "health_check", status: "pending" },
  { name: "commit_verification", status: "pending" },
];
const macroSteps = updateApplyStepRows({ steps: internalSteps }, t);
assert.deepEqual(macroSteps.map((step) => step.name), ["request", "preflight", "applying", "health_check", "commit_verification"]);
assert.equal(macroSteps.length, 5);
assert.equal(macroSteps[2].status, "running");
assert.equal(macroSteps.some((step) => ["extracting", "validating_source", "overlay", "compose_config", "rebuilding", "restarting"].includes(step.name)), false);

const completedRuntimeMacroSteps = updateApplyStepRows({
  steps: [
    { name: "request", status: "completed" },
    { name: "preflight", status: "completed" },
    { name: "apply", status: "completed" },
    { name: "health_check", status: "completed" },
    { name: "commit_verification", status: "completed" },
  ],
}, t);
assert.deepEqual(
  completedRuntimeMacroSteps.map((step) => step.status),
  ["completed", "completed", "completed", "completed", "completed"],
);

for (const wording of [
  'updateApplyStart: "Обновить KM VMS"',
  'updateApplyStart: "Update KM VMS"',
  'updateApplyStart: "更新 KM VMS"',
  'updateApplyConfirm: "Установить доступное обновление KM VMS?"',
  'updateApplyConfirm: "Install the available KM VMS update?"',
  'updateApplyConfirm: "安装可用的 KM VMS 更新？"',
]) {
  assert.equal(settingsPage.includes(wording), true);
}
