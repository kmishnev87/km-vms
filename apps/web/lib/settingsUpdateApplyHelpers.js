import {
  boundedContractText,
  boundedFiniteNumber,
  formatMaintenanceMessage,
  maintenanceStatusText,
  normalizeMaintenanceBackendText,
} from "./settingsPageSharedHelpers.js";

export const UPDATE_APPLY_RUNNING_STATUSES = ["queued", "starting_helper", "preflight", "acquire_source", "downloading", "extracting", "validating_source", "overlay", "applying", "compose_config", "rebuilding", "restarting", "health_check", "commit_verification", "preparing", "staging", "activating", "reconnecting", "rolling_back"];
export const UPDATE_APPLY_POLL_INTERVAL_MS = 5000;
const UPDATE_APPLY_STALE_DEFAULT_SECONDS = 180;
const UPDATE_APPLY_PRESERVED_TERMINAL_STATUSES = new Set([
  "failed",
  "failed_rolled_back",
  "blocked",
  "stalled",
  "cancelled",
  "canceled",
]);

export function updateApplyProgressText(applyStatus, t) {
  const phase = maintenanceStatusText(
    applyStatus?.current_step || applyStatus?.phase || applyStatus?.status,
    t,
  );
  const percent = applyStatus?.progress_percent;
  const current = applyStatus?.progress_current;
  const total = applyStatus?.progress_total;
  const unit = applyStatus?.progress_unit;
  const exactProgress = (
    Number.isInteger(percent)
    && Number.isInteger(current)
    && Number.isInteger(total)
    && percent >= 0
    && percent <= 100
    && current >= 0
    && total > 0
    && current <= total
    && ["bytes", "items"].includes(unit)
    && percent === Math.floor((current * 100) / total)
  );
  if (exactProgress) return `${phase} — ${percent}%`;
  return `${phase} — ${t.updateApplyProgressIndeterminate || "In progress…"}`;
}
export function updateApplyIsRunning(status) {
  return UPDATE_APPLY_RUNNING_STATUSES.includes(status || "");
}

export function updateApplyReconnectTiming(applyStatus, receivedAtMs) {
  const received = boundedFiniteNumber(receivedAtMs, 0, 0, Number.MAX_SAFE_INTEGER);
  const staleAfterSeconds = boundedFiniteNumber(
    applyStatus?.stale_after_seconds,
    UPDATE_APPLY_STALE_DEFAULT_SECONDS,
    1,
    3600,
  );
  const lastProgressAgeSeconds = boundedFiniteNumber(
    applyStatus?.last_progress_age_seconds,
    0,
    0,
    staleAfterSeconds,
  );
  const allowanceMs = UPDATE_APPLY_POLL_INTERVAL_MS * 2;
  const remainingMs = Math.max(0, staleAfterSeconds - lastProgressAgeSeconds) * 1000;
  const hardDeadlineMs = received + (staleAfterSeconds * 1000) + allowanceMs;
  return {
    receivedAtMs: received,
    staleAfterSeconds,
    lastProgressAgeSeconds,
    deadlineMs: Math.min(received + remainingMs + allowanceMs, hardDeadlineMs),
    hardDeadlineMs,
  };
}

export function updateApplyTransportPhase(applyStatus, transportError, timing, nowMs) {
  if (!transportError) return "connected";
  if (!updateApplyIsRunning(applyStatus?.status || applyStatus?.effective_status || "")) return "unknown";
  const now = boundedFiniteNumber(nowMs, Number.MAX_SAFE_INTEGER, 0, Number.MAX_SAFE_INTEGER);
  const deadline = boundedFiniteNumber(timing?.deadlineMs, -1, 0, Number.MAX_SAFE_INTEGER);
  return deadline >= 0 && now <= deadline ? "reconnecting" : "unknown";
}

export function updateApplyCandidateSnapshot(updateStatus) {
  const trustedCandidate = updateApplyTrustedCandidateRelease(updateStatus);
  const latest = trustedCandidate.version && (trustedCandidate.commit || trustedCandidate.commit_sha)
    ? trustedCandidate
    : updateStatus?.latest || updateStatus?.latest_release || {};
  return Object.freeze({
    version: boundedContractText(latest.version || latest.latest_version, 80),
    commit: boundedContractText(latest.commit || latest.commit_sha || latest.build_id, 80).toLowerCase(),
    title: boundedContractText(latest.title, 240),
  });
}

export function updateApplyEffectiveStatus(updateStatus, applyStatus, transportContext = "") {
  const status = boundedContractText(applyStatus?.effective_status || applyStatus?.status || updateStatus?.status || "unknown", 80).toLowerCase();
  if (applyStatus?.is_stale || status === "stalled") return "stalled";
  if (status === "completed" && applyStatus?.expected_commit && applyStatus?.commit_verified === false) return "failed";
  const context = transportContext && typeof transportContext === "object"
    ? transportContext
    : { applyError: transportContext };
  if (context.applyError) {
    if (UPDATE_APPLY_PRESERVED_TERMINAL_STATUSES.has(status) || status === "completed") return status;
    if (transportContext && typeof transportContext !== "object" && updateApplyIsRunning(status)) return "reconnecting";
    return updateApplyTransportPhase(applyStatus, context.applyError, context.reconnectTiming, context.nowMs);
  }
  return status;
}

export function formatDurationSeconds(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "-";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return mins ? `${hours}h ${mins}m` : `${hours}h`;
}

export function shortCommit(value) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.length > 12 ? `${text.slice(0, 12)}...` : text;
}

export function updateApplyTrustedCandidateRelease(updateStatus) {
  const candidate = updateStatus?.trusted_apply_candidate;
  if (!candidate?.fresh || !candidate?.latest) return {};
  const latest = candidate.latest || {};
  const available = candidate.available_release || {};
  return {
    version: available.version || latest.version,
    title: available.title || latest.title,
    summary: available.summary || latest.summary,
    changelog: available.changelog || latest.breaking_changes || [],
    title_i18n: available.title_i18n || latest.title_i18n,
    summary_i18n: available.summary_i18n || latest.summary_i18n,
    changelog_i18n: available.changelog_i18n || latest.changelog_i18n,
    published_at: available.published_at || latest.published_at,
    tag: available.tag || latest.source_ref || latest.git_ref,
    commit: available.commit_sha || latest.commit,
    commit_sha: available.commit_sha || latest.commit,
    commit_short: available.commit_short || shortCommit(latest.commit),
    provider: available.provider || candidate.source || "trusted_snapshot",
  };
}

export function updateApplyFactRows(updateStatus, applyStatus, t) {
  const installedRelease = updateStatus?.installed_release || {};
  const trustedRelease = updateApplyTrustedCandidateRelease(updateStatus);
  const availableRelease = updateStatus?.available_release || trustedRelease;
  const latest = updateStatus?.latest || updateStatus?.latest_release || trustedRelease;
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  const labels = t.maintenanceLabels || {};
  const comparison = updateStatus?.comparison || {};
  const status = comparison.status || updateStatus?.status || "unknown";
  const title = availableRelease.title || latest.title || installedRelease.title || "-";
  const summary = availableRelease.summary || latest.release_notes_summary || installedRelease.summary || "-";
  const verification = applyStatus?.expected_commit
    ? (applyStatus.commit_verified ? t.updateCommitVerified : t.updateCommitPending)
    : t.updateCommitUnavailable;
  return [
    [labels.current, installedRelease.version || installed.app_version || updateStatus?.installed?.installed_version || "-"],
    [labels.available, availableRelease.version || latest.version || latest.latest_version || "-"],
    [labels.releaseTitle || "Release", title],
    [labels.releaseSummary || "Summary", summary],
    [labels.status || "Status", maintenanceStatusText(status, t)],
    [labels.verification, verification],
    [labels.currentStep || "Current step", maintenanceStatusText(applyStatus?.current_step || applyStatus?.phase || status, t)],
    [labels.lastProgress || "Last progress", formatDurationSeconds(applyStatus?.last_progress_age_seconds)],
    [labels.elapsed || "Elapsed", formatDurationSeconds(applyStatus?.elapsed_seconds)],
  ];
}

export function updateApplyTechnicalRows(updateStatus, applyStatus, t) {
  const installedRelease = updateStatus?.installed_release || {};
  const availableRelease = updateStatus?.available_release || {};
  const evidence = updateStatus?.evidence || {};
  const latest = updateStatus?.latest || updateStatus?.latest_release || {};
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  const labels = t.maintenanceLabels || {};
  const targetCommit = latest.commit || latest.build_id || applyStatus?.expected_commit || applyStatus?.source?.commit || "";
  const installedCommit = applyStatus?.installed_commit || installed.git_commit || installed.installed_commit || "";
  const sourceRef = availableRelease.tag || latest.git_ref || latest.source_ref || applyStatus?.source?.apply_ref || applyStatus?.source?.ref || updateStatus?.source_channel?.source_channel_id || "";
  return [
    [labels.source, sourceRef || "-"],
    [labels.installedCommit, installedRelease.commit_short || shortCommit(installedCommit) || "-"],
    [labels.targetCommit, availableRelease.commit_short || shortCommit(targetCommit) || "-"],
    [labels.gitHead || "Git HEAD", evidence.git_head_short || shortCommit(evidence.git_head) || "-"],
    [labels.metadataSource || "Metadata", installedRelease.metadata_source || "-"],
    [labels.releaseIdentity || "Release identity", applyStatus?.release_identity?.metadata_status || installedRelease.metadata_status || "-"],
    [labels.provider || "Provider", availableRelease.provider || updateStatus?.source_channel?.trusted_source_type || "-"],
  ].filter(([, value]) => value !== "-");
}

function updateApplyReleaseValue(updateStatus, key) {
  const installedRelease = updateStatus?.installed_release || {};
  const trustedRelease = updateApplyTrustedCandidateRelease(updateStatus);
  const availableRelease = updateStatus?.available_release || trustedRelease;
  const latest = updateStatus?.latest || updateStatus?.latest_release || trustedRelease;
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  if (key === "currentVersion") return installedRelease.version || installed.app_version || updateStatus?.installed?.installed_version || "-";
  if (key === "availableVersion") return availableRelease.version || latest.version || latest.latest_version || "-";
  if (key === "publishedAt") return availableRelease.published_at || latest.published_at || "";
  if (key === "title") return availableRelease.title || latest.title || installedRelease.title || latest.release_notes_summary || "-";
  if (key === "summary") return availableRelease.summary || latest.release_notes_summary || installedRelease.summary || "";
  if (key === "installedAt") return installedRelease.installed_at || installed.installed_at || "";
  if (key === "targetCommit") return availableRelease.commit || latest.commit || latest.build_id || "";
  if (key === "installedCommit") return installedRelease.commit || installedRelease.commit_sha || installed.git_commit || installed.installed_commit || "";
  if (key === "metadataStatus") return installedRelease.metadata_status || installedRelease.identity_validity || "";
  return "";
}

function updateApplyReleaseNotesSource(updateStatus) {
  const status = normalizedUpdateApplyState(
    updateStatus?.comparison?.status || updateStatus?.status,
  );
  const useCandidate = (
    status === "update_available"
    || Boolean(updateStatus?.can_apply_from_ui)
    || Boolean(updateStatus?.trusted_apply_candidate?.can_apply_from_ui)
  );
  if (!useCandidate) return updateStatus?.installed_release || {};
  const available = updateStatus?.available_release;
  if (available && typeof available === "object") return available;
  const trusted = updateApplyTrustedCandidateRelease(updateStatus);
  if (Object.keys(trusted).length) return trusted;
  return updateStatus?.latest || updateStatus?.latest_release || {};
}

function localizedReleaseValue(updateStatus, key, t, lang) {
  const release = updateApplyReleaseNotesSource(updateStatus);
  const localized = release?.[`${key}_i18n`];
  const exact = localized && typeof localized === "object"
    ? localized[lang]
    : "";
  if (typeof exact === "string" && exact.trim()) return exact.trim();
  const plain = release?.[key];
  if (typeof plain === "string" && plain.trim()) return plain.trim();
  return key === "title"
    ? t.updateApplyReleaseTitleFallback || "KM VMS release"
    : "";
}

function localizedReleaseChangelog(updateStatus, lang) {
  const release = updateApplyReleaseNotesSource(updateStatus);
  const localized = release?.changelog_i18n;
  const exact = localized && typeof localized === "object"
    ? localized[lang]
    : null;
  const source = Array.isArray(exact)
    ? exact
    : Array.isArray(release?.changelog)
      ? release.changelog
      : [];
  return source
    .filter((item) => typeof item === "string" && item.trim())
    .slice(0, 20)
    .map((item) => item.trim().slice(0, 180));
}

function localizedReleaseNotes(updateStatus, t, lang) {
  let summary = localizedReleaseValue(updateStatus, "summary", t, lang);
  let changelog = localizedReleaseChangelog(updateStatus, lang);
  if (summary) {
    changelog = changelog.filter((item) => item.trim() !== summary.trim());
  }
  if (!summary && !changelog.length) {
    summary = t.updateApplyReleaseSummaryFallback || "";
  }
  return { summary, changelog };
}

const FULL_COMMIT_RE = /^[0-9a-f]{40}$/i;
const ISO_TIMESTAMP_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

function validApplyTimestamp(value) {
  if (
    typeof value !== "string"
    || !value
    || value.length > 80
    || !ISO_TIMESTAMP_RE.test(value)
  ) {
    return "";
  }
  return Number.isNaN(new Date(value).getTime()) ? "" : value;
}

function honestUpdateFinishedAt(updateStatus, lastSummary) {
  const installedRelease = updateStatus?.installed_release || {};
  const fallback = validApplyTimestamp(installedRelease.installed_at);
  const expectedCommit = String(lastSummary?.expected_commit || "").trim();
  const operationInstalledCommit = String(
    lastSummary?.installed_commit || "",
  ).trim();
  const currentInstalledCommit = String(
    installedRelease.commit_sha || installedRelease.commit || "",
  ).trim();
  const metadataStatus = normalizedUpdateApplyState(
    installedRelease.metadata_status,
  );
  const identityValidity = normalizedUpdateApplyState(
    installedRelease.identity_validity,
  );
  const finishedAt = validApplyTimestamp(lastSummary?.finished_at);
  if (
    normalizedUpdateApplyState(lastSummary?.status) === "completed"
    && lastSummary?.commit_verified === true
    && FULL_COMMIT_RE.test(expectedCommit)
    && FULL_COMMIT_RE.test(operationInstalledCommit)
    && FULL_COMMIT_RE.test(currentInstalledCommit)
    && expectedCommit.toLowerCase() === operationInstalledCommit.toLowerCase()
    && expectedCommit.toLowerCase() === currentInstalledCommit.toLowerCase()
    && metadataStatus === "complete"
    && identityValidity === "valid"
    && finishedAt
  ) {
    return finishedAt;
  }
  return fallback;
}

function releaseConfirmedText(value, t) {
  const key = String(value || "").trim().toLowerCase();
  if (["adopted", "already_adopted", "official_update", "valid"].includes(key)) return t.yes || "Yes";
  if (!key) return "-";
  return maintenanceStatusText(key, t);
}

function formatApplyDate(value, lang = "ru") {
  const timestamp = validApplyTimestamp(value);
  if (!timestamp) return "-";
  const date = new Date(timestamp);
  return new Intl.DateTimeFormat(lang === "en" ? "en-US" : lang === "zh-CN" ? "zh-CN" : "ru-RU", {
    year: "2-digit",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function applyStepIcon(status) {
  if (status === "completed") return "check";
  if (status === "failed") return "alert";
  if (status === "running") return "pulse";
  if (status === "idle") return "idle";
  return "dot";
}

function defaultUpdateApplyTimeline(t) {
  return ["request", "preflight", "applying", "health_check", "commit_verification"].map((name) => ({
    name,
    label: maintenanceStatusText(name, t),
    status: "idle",
    statusLabel: "",
    icon: "idle",
    timeLabel: "",
  }));
}

const UPDATE_APPLY_ATTENTION_STATES = new Set([
  "failed",
  "failed_rolled_back",
  "check_failed",
  "stalled",
  "reconnecting",
  "blocked",
  "cancelled",
  "canceled",
]);

const UPDATE_CHECK_BLOCKING_STATES = new Set([
  "check_failed",
  "identity_incomplete",
  "installed_identity_drift",
  "metadata_stale",
  "provider_unavailable",
  "no_release_published",
  "installed_newer_than_available",
]);

function normalizedUpdateApplyState(value) {
  return String(value || "").trim().toLowerCase();
}

function isUpdateApplyAttentionState(value) {
  return UPDATE_APPLY_ATTENTION_STATES.has(normalizedUpdateApplyState(value));
}

export function updateApplyOperatorModel(updateStatus, applyStatus, t, lang = "ru", transportContext = "") {
  const context = transportContext && typeof transportContext === "object"
    ? transportContext
    : { applyError: transportContext };
  const effective = updateApplyEffectiveStatus(updateStatus, applyStatus, context);
  const comparison = updateStatus?.comparison || {};
  const status = comparison.status || updateStatus?.status || effective || "unknown";
  const normalizedEffective = normalizedUpdateApplyState(effective);
  const normalizedStatus = normalizedUpdateApplyState(status);
  const normalizedUpdateStatus = normalizedUpdateApplyState(updateStatus?.status);
  const normalizedLastCheckStatus = normalizedUpdateApplyState(
    updateStatus?.last_check_status || updateStatus?.last_update_check?.status,
  );
  const lastKnownRunning = updateApplyIsRunning(applyStatus?.status || "");
  const running = lastKnownRunning && normalizedEffective !== "unknown";
  const stateUnknown = Boolean(context.unresolvedSubmission) || (normalizedEffective === "unknown" && Boolean(context.applyError));
  const trustedCandidate = updateStatus?.trusted_apply_candidate || {};
  const freshTrustedCandidateAvailable = Boolean(trustedCandidate.fresh && trustedCandidate.can_apply_from_ui && trustedCandidate.latest);
  const liveCheckFailedWithCandidate = (
    normalizedUpdateStatus === "check_failed"
    || normalizedLastCheckStatus === "check_failed"
  ) && freshTrustedCandidateAvailable;
  const canApply = Boolean((updateStatus?.can_apply_from_ui || freshTrustedCandidateAvailable) && !lastKnownRunning && !applyStatus?.is_stale && !stateUnknown && !context.unresolvedSubmission);
  const lastSummary = applyStatus?.last_apply_summary || null;
  const currentVersion = updateApplyReleaseValue(updateStatus, "currentVersion");
  const targetCommit = updateApplyReleaseValue(updateStatus, "targetCommit") || applyStatus?.expected_commit || applyStatus?.source?.commit || "";
  const installedCommit = applyStatus?.installed_commit || updateApplyReleaseValue(updateStatus, "installedCommit");
  const operationExpectedCommit = applyStatus?.expected_commit || lastSummary?.expected_commit || "";
  const operationInstalledCommit = applyStatus?.installed_commit || lastSummary?.installed_commit || installedCommit;
  const operationCommitVerified = Boolean(
    operationExpectedCommit &&
    operationInstalledCommit &&
    operationExpectedCommit === operationInstalledCommit &&
    applyStatus?.commit_verified !== false &&
    lastSummary?.commit_verified !== false
  );
  const comparisonCommitVerified = Boolean(installedCommit && targetCommit && installedCommit === targetCommit);
  const commitVerified = effective === "completed"
    ? operationCommitVerified
    : Boolean(applyStatus?.commit_verified || lastSummary?.commit_verified || comparisonCommitVerified);
  const metadataStatusValue = updateApplyReleaseValue(updateStatus, "metadataStatus") || applyStatus?.release_identity?.metadata_status;
  const identityComplete = normalizedUpdateApplyState(metadataStatusValue) === "complete";
  const terminalSuccess = effective === "completed" && commitVerified && identityComplete;
  const presentedTerminalSuccess = terminalSuccess && !stateUnknown;
  const terminalVerificationIncomplete = effective === "completed" && !terminalSuccess;
  const current = status === "current" && !canApply && !running;
  const available = status === "update_available" && canApply;
  const hasAvailableRelease = Boolean(available || liveCheckFailedWithCandidate);
  const availableVersion = hasAvailableRelease
    ? updateApplyReleaseValue(updateStatus, "availableVersion")
    : "";
  const publishedAtValue = hasAvailableRelease
    ? updateApplyReleaseValue(updateStatus, "publishedAt")
    : "";
  const suppressUpdateCheckBlocker = lastKnownRunning && Boolean(context.applyError || running);
  const helperFailure = isUpdateApplyAttentionState(normalizedEffective) && normalizedEffective !== "reconnecting";
  const updateCheckFailure = !suppressUpdateCheckBlocker && !stateUnknown && (
    isUpdateApplyAttentionState(normalizedStatus) ||
    isUpdateApplyAttentionState(normalizedUpdateStatus) ||
    isUpdateApplyAttentionState(normalizedLastCheckStatus) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedEffective) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedStatus) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedUpdateStatus) ||
    UPDATE_CHECK_BLOCKING_STATES.has(normalizedLastCheckStatus)
  );
  const failed = terminalVerificationIncomplete || helperFailure || (!liveCheckFailedWithCandidate && updateCheckFailure);
  const severity = failed ? "blocked" : running || stateUnknown || available || liveCheckFailedWithCandidate || context.updateError ? "warning" : "ok";
  const headlineKey = stateUnknown
    ? "unknown"
    : presentedTerminalSuccess
    ? "completed"
    : running
      ? "running"
      : failed
          ? "blocked"
          : available || liveCheckFailedWithCandidate
            ? "available"
            : current
              ? "current"
              : "unknown";
  const headline = t.updateApplyHeadlines?.[headlineKey] || maintenanceStatusText(presentedTerminalSuccess ? "completed" : status, t);
  const recoveryStatus = terminalVerificationIncomplete
    ? (commitVerified ? "identity_incomplete" : "failed")
    : stateUnknown
      ? "unknown"
      : isUpdateApplyAttentionState(normalizedEffective) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedEffective)
    ? normalizedEffective
    : isUpdateApplyAttentionState(normalizedUpdateStatus) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedUpdateStatus)
      ? normalizedUpdateStatus
    : isUpdateApplyAttentionState(normalizedLastCheckStatus) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedLastCheckStatus)
      ? normalizedLastCheckStatus
    : isUpdateApplyAttentionState(normalizedStatus) || UPDATE_CHECK_BLOCKING_STATES.has(normalizedStatus)
      ? normalizedStatus
      : failed && normalizedStatus && normalizedStatus !== "unknown"
        ? normalizedStatus
        : effective;
  const recoverySummary = liveCheckFailedWithCandidate
    ? (t.updateApplyRecoveryLiveCheckFailedWithSnapshot || updateApplyRecoveryText("provider_unavailable", applyStatus, t))
    : updateApplyRecoveryText(recoveryStatus, applyStatus, t);
  const summary = failed || liveCheckFailedWithCandidate || stateUnknown
    ? recoverySummary
    : running
      ? updateApplyProgressText(applyStatus, t)
      : (t.updateApplySummaries?.[headlineKey] || recoverySummary);
  const updateResult = presentedTerminalSuccess
    ? (t.updateApplyResults?.completedVerified || headline)
    : t.updateApplyResults?.[headlineKey] || headline;
  const finishedAt = honestUpdateFinishedAt(updateStatus, lastSummary);
  const releaseNotes = localizedReleaseNotes(updateStatus, t, lang);
  const elapsed = running
    ? formatDurationSeconds(applyStatus?.elapsed_seconds)
    : lastSummary?.elapsed_seconds
      ? formatDurationSeconds(lastSummary.elapsed_seconds)
      : "-";
  const liveStepsAvailable = Array.isArray(applyStatus?.steps) && applyStatus.steps.length;
  const historyStepsAvailable = Array.isArray(lastSummary?.steps) && lastSummary.steps.length;
  const stepsSource = liveStepsAvailable
    ? applyStatus
    : historyStepsAvailable
      ? lastSummary
      : null;
  const terminalTimelineTruth = liveStepsAvailable && [
    "completed",
    "failed",
    "failed_rolled_back",
    "blocked",
    "cancelled",
    "canceled",
  ].includes(normalizedUpdateApplyState(applyStatus?.status));
  const inactiveTimeline = !running && !terminalTimelineTruth;
  const timeline = updateApplyStepRows(stepsSource || {}, t).map((step) => ({
    ...step,
    status: inactiveTimeline ? "idle" : step.status,
    icon: applyStepIcon(inactiveTimeline ? "idle" : step.status),
    timeLabel: inactiveTimeline ? "" : (step.time_label || (step.status === "running" ? step.statusLabel : "")),
  }));
  const detailUnavailable = Boolean((lastSummary?.history_detail_status || applyStatus?.apply_history?.state === "missing") && !timeline.some((step) => step.timeLabel && /:/.test(step.timeLabel)));
  const safeTimeline = timeline.length ? timeline : defaultUpdateApplyTimeline(t);
  return {
    status: effective,
    severity,
    headline,
    summary,
    showHeroSummary: !presentedTerminalSuccess,
    updateResult,
    currentVersion,
    availableVersion,
    publishedAt: publishedAtValue ? formatApplyDate(publishedAtValue, lang) : "",
    releaseTitle: localizedReleaseValue(updateStatus, "title", t, lang),
    releaseSummary: releaseNotes.summary,
    releaseChangelog: releaseNotes.changelog,
    installedAt: formatApplyDate(
      updateStatus?.installed_release?.installed_at,
      lang,
    ),
    finishedAt: formatApplyDate(finishedAt, lang),
    elapsed,
    lastProgress: running ? formatDurationSeconds(applyStatus?.last_progress_age_seconds) : "",
    commitStatus: commitVerified ? t.updateCommitVerified : targetCommit ? t.updateCommitPending : t.updateCommitUnavailable,
    commitVerified,
    installedCommitShort: shortCommit(installedCommit) || "-",
    targetCommitShort: shortCommit(targetCommit) || "-",
    metadataStatus: releaseConfirmedText(metadataStatusValue, t),
    canApply,
    canCheck: true,
    showApplyButton: canApply || lastKnownRunning || Boolean(context.unresolvedSubmission),
    timeline: safeTimeline.slice(0, 5),
    detailUnavailable,
    diagnosticsRows: updateApplyTechnicalRows(updateStatus, applyStatus, t),
    stateUnknown,
    reconnecting: normalizedEffective === "reconnecting",
  };
}

export function updateApplyRecoveryText(status, applyStatus, t) {
  const effective = status || "unknown";
  if (effective === "stalled") return applyStatus?.error?.operator_action || t.updateApplyRecoveryStalled;
  if (effective === "reconnecting") return t.updateApplyRecoveryReconnecting;
  if (effective === "completed" && applyStatus?.commit_verified) return t.updateApplyRecoveryCompleted;
  if (effective === "completed" && applyStatus?.expected_commit && applyStatus?.commit_verified === false) return t.updateApplyRecoveryCommitMismatch;
  if (effective === "failed") return t.updateApplyRecoveryFailed;
  if (effective === "failed_rolled_back") return t.updateApplyRecoveryRolledBack || t.updateApplyRecoveryFailed;
  if (effective === "check_failed") return t.updateApplyRecoveryCheckFailed || t.updateApplyRecoveryFailed;
  if (["update_check_required", "trusted_snapshot_stale", "trusted_snapshot_invalidated", "manifest_version_changed", "manifest_commit_changed"].includes(effective)) {
    return t.updateApplyRecoveryRefreshRequired || t.updateApplyRecoveryCheckFailed || t.updateApplyRecoveryBlocked;
  }
  if (effective === "trusted_commit_missing") return t.updateApplyRecoveryMissingCommit || t.updateApplyRecoveryBlocked;
  if (effective === "blocked" || effective === "not_configured") return t.updateApplyRecoveryBlocked;
  if (updateApplyIsRunning(effective)) return t.updateApplyRecoveryRunning;
  if (effective === "current") return t.updateApplyRecoveryCurrent;
  if (effective === "update_available") return t.updateApplyRecoveryAvailable;
  if (effective === "identity_incomplete" || effective === "installed_identity_drift" || effective === "metadata_stale") return t.updateApplyRecoveryIdentity;
  if (effective === "provider_unavailable" || effective === "no_release_published") return t.updateApplyRecoveryProvider;
  if (effective === "installed_newer_than_available") return t.updateApplyRecoveryInstalledNewer;
  return t.updateApplyRecoveryUnknown;
}

export function updateApplyStepRows(applyStatus, t) {
  const steps = Array.isArray(applyStatus?.steps) ? applyStatus.steps : [];
  const stageNames = ["request", "preflight", "applying", "health_check", "commit_verification"];
  const stageFor = (name) => {
    if (name === "queued" || name === "request" || name === "starting_helper") return "request";
    if (name === "preflight") return "preflight";
    if (["health_check", "reconnecting", "rolling_back"].includes(name)) return "health_check";
    if (name === "commit_verification" || name === "completed") return "commit_verification";
    if ([
      "acquire_source",
      "downloading",
      "extracting",
      "validating_source",
      "overlay",
      "apply",
      "applying",
      "compose_config",
      "rebuilding",
      "restarting",
      "preparing",
      "staging",
      "activating",
    ].includes(name)) return "applying";
    return "";
  };
  const rank = { failed: 5, running: 4, completed: 3, pending: 2, idle: 1 };
  const normalizeStatus = (value) => {
    const status = String(value || "pending").trim().toLowerCase();
    if (["failed", "error", "blocked", "cancelled", "canceled", "stalled"].includes(status)) return "failed";
    if (["running", "in_progress", "starting", "active"].includes(status)) return "running";
    if (["completed", "complete", "ok", "done", "verified"].includes(status)) return "completed";
    if (status === "idle") return "idle";
    return "pending";
  };
  const grouped = new Map(stageNames.map((name) => [name, {
    name,
    label: maintenanceStatusText(name, t),
    status: "pending",
    statusLabel: maintenanceStatusText("pending", t),
  }]));
  for (const step of steps) {
    const name = String(step?.name || "").trim();
    const stage = stageFor(name);
    if (!stage || !grouped.has(stage)) continue;
    const status = normalizeStatus(step?.status);
    const current = grouped.get(stage);
    if (rank[status] >= rank[current.status]) {
      grouped.set(stage, {
        name: stage,
        label: maintenanceStatusText(stage, t),
        status,
        statusLabel: maintenanceStatusText(status, t),
        ...((step?.time_label || step?.completed_at || step?.updated_at) ? { time_label: step?.time_label || step?.completed_at || step?.updated_at } : {}),
      });
    }
  }
  return stageNames.map((name) => grouped.get(name));
}

export function updateApplyButtonText(applyStatus, t) {
  const step = applyStatus?.current_step || applyStatus?.phase || applyStatus?.status;
  if (!updateApplyIsRunning(applyStatus?.status || "")) return t.updateApplyStart;
  if (step === "rebuilding") return t.updateApplyButtonRebuilding || maintenanceStatusText("rebuilding", t);
  if (step === "health_check") return t.updateApplyButtonHealth || maintenanceStatusText("health_check", t);
  if (step === "commit_verification") return t.updateApplyButtonVerification || maintenanceStatusText("commit_verification", t);
  return t.updateApplyButtonRunning || maintenanceStatusText(step, t);
}

function normalizeUpdateNoticeCode(item) {
  const raw = String(item?.code || item?.category || item?.reason || item?.status || item?.phase || "").trim().toLowerCase();
  if (raw) return raw;
  const message = String(item?.message || item?.error_message || "").trim().toLowerCase();
  if (!message) return "";
  if (message.includes("installed source metadata is unavailable or invalid")) return "source_metadata_invalid";
  if (message.includes("last update metadata is unavailable or invalid")) return "update_metadata_invalid";
  if (message.includes("source metadata schema is unsupported")) return "source_metadata_unsupported_schema";
  if (message.includes("update metadata schema is unsupported")) return "update_metadata_unsupported_schema";
  if (message.includes("installed commit value is not a valid")) return "installed_commit_invalid";
  if (message.includes("trusted manifest") && message.includes("not configured")) return "trusted_manifest_not_configured";
  if (message.includes("commit does not match")) return "commit_mismatch";
  if (message.includes("token") && (message.includes("missing") || message.includes("configured"))) return "token_not_configured";
  if (message.includes("migration")) return "requires_migration";
  if (message.includes("backup")) return "requires_backup";
  if (message.includes("manual")) return "requires_manual_action";
  return "";
}

export function formatUpdateNotice(item, t, lang = "ru") {
  const code = normalizeUpdateNoticeCode(item);
  const labels = t.updateWarningLabels || {};
  if (code && labels[code]) return labels[code];
  if (code.startsWith("source_metadata_") && labels.source_metadata_invalid) return labels.source_metadata_invalid;
  if (code.startsWith("update_metadata_") && labels.update_metadata_invalid) return labels.update_metadata_invalid;
  if ((code === "requires_migration" || code === "release_requires_migration" || code === "migration_required") && labels.requires_migration) return labels.requires_migration;
  if ((code === "requires_backup" || code === "release_requires_backup" || code === "backup_required") && labels.requires_backup) return labels.requires_backup;
  if ((code === "requires_manual_action" || code === "manual_action_required") && labels.requires_manual_action) return labels.requires_manual_action;
  if ((code === "trusted_manifest_not_configured" || code === "manifest_not_configured" || code === "not_configured") && labels.trusted_manifest_not_configured) return labels.trusted_manifest_not_configured;
  if ((code === "private_token_missing" || code === "token_not_configured") && labels.token_not_configured) return labels.token_not_configured;
  if ((code === "update_check_already_running" || code === "manual_update_check_rate_limited") && labels[code]) return labels[code];
  const raw = String(item?.message || item?.error_message || item?.code || "").trim();
  if (lang === "en" && raw && !/stack|trace|authorization|bearer|token|secret|\.env|rtsp:|onvif/i.test(raw) && raw.length <= 140) {
    return labels[code] || raw;
  }
  return t.updateWarningGeneric || "Update warning is present.";
}

export function updateApplyErrorMessages(error, t, lang = "ru") {
  if (!error || typeof error !== "object") return [];
  const categoryKey = normalizeMaintenanceBackendText(error.category);
  const categoryMessage = categoryKey ? t.maintenanceMessageLabels?.[categoryKey] || "" : "";
  const messages = [
    categoryMessage || (error.message ? formatMaintenanceMessage(error.message, t, lang, "error") : ""),
    error.operator_action ? formatMaintenanceMessage(error.operator_action, t, lang, "action") : "",
  ].filter(Boolean);
  return [...new Set(messages)];
}

export function buildUpdateApplyConfirmation(t, updateStatus) {
  const trustedRelease = updateApplyTrustedCandidateRelease(updateStatus);
  const latest = updateStatus?.latest || updateStatus?.latest_release || trustedRelease;
  const installed = updateStatus?.installed_build || updateStatus?.installed || {};
  const lines = [t.updateApplyConfirm];
  if (installed.app_version || updateStatus?.installed?.installed_version) lines.push(`${t.updateCurrent}: ${installed.app_version || updateStatus?.installed?.installed_version}`);
  if (latest.version || latest.latest_version) lines.push(`${t.updateLatest}: ${latest.version || latest.latest_version}`);
  if (latest.commit || latest.commit_sha || latest.build_id) lines.push(`${t.maintenanceLabels?.targetCommit}: ${shortCommit(latest.commit || latest.commit_sha || latest.build_id)}`);
  lines.push(t.updateApplyConfirmRestart);
  return lines.filter(Boolean).join("\n");
}
