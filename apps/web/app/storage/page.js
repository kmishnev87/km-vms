"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch, canAccessPath, forbiddenMessage } from "../../lib/api";
import { useCurrentUser } from "../../lib/currentUser";
import { useI18n, useLocaleText } from "../../lib/i18n";
import {
  boolLabel,
  cameraStorageRows,
  formatBytes,
  formatDateTime,
  formatPercent,
  humanBlockerReason,
  isStorageAccessDeniedError,
  primaryStorageActionText,
  actionPermissionState,
  accessRightsModel,
  archiveRootScenarioModel,
  freeSpaceTone,
  migrationScenarioModel,
  normalizeReconciliationSummary,
  reconciliationScenarioModel,
  retentionScenarioModel,
  statusLabel,
  storageTopHealthModel,
  topReasonEntries,
} from "../../lib/storageOperations";

const REFRESH_MS = 30000;

function Stat({ label, value, tone = "neutral" }) {
  return (
    <div className={`storageOpsStat storageOpsStat-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Badge({ label, tone = "neutral" }) {
  return <span className={`storageOpsBadge storageOpsBadge-${tone}`}>{label}</span>;
}

function SummaryRow({ label, value, tone = "neutral" }) {
  return (
    <div className={`storageOpsSummaryRow storageOpsSummaryRow-${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Section({ title, children, action = null, className = "" }) {
  return (
    <section className={`storageOpsSection ${className}`}>
      <div className="storageOpsSectionHead">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function reasonText(summary, copy, language) {
  const entries = topReasonEntries(summary);
  if (!entries.length) return copy.noReasons;
  return entries.map(([key, value]) => `${humanBlockerReason(key, language)}: ${value}`).join(", ");
}

function blockerText(blockers, copy, language) {
  const list = Array.isArray(blockers) ? blockers : [];
  if (!list.length) return copy.noReasons;
  const labels = list.map((item) => humanBlockerReason(item, language));
  return Array.from(new Set(labels)).join(" ");
}

function errorDetailText(error, fallback, language) {
  const detail = error?.detail || error?.data?.detail || null;
  if (detail && typeof detail === "object") {
    if (Array.isArray(detail.blockers) && detail.blockers.length) return blockerText(detail.blockers, { noReasons: fallback }, language);
    if (detail.error) return humanBlockerReason(detail.error, language);
    if (detail.reason) return humanBlockerReason(detail.reason, language);
  }
  return error?.message || fallback;
}

function retentionSummaryText(source, copy) {
  if (!source) return copy.retentionNoPreview;
  return copy.retentionPreviewReady
    .replace("{count}", String(source.planned_count || source.deleted_count || 0))
    .replace("{bytes}", formatBytes(source.estimated_freed_bytes ?? source.bytes_freed ?? 0));
}

function reconciliationSummaryText(source, copy) {
  if (!source) return copy.reconciliationNoPreview;
  const normalized = normalizeReconciliationSummary(source);
  return copy.reconciliationPreviewReady
    .replace("{problems}", String(normalized.problemCount))
    .replace("{safe}", String(normalized.safeFixCount))
    .replace("{manual}", String(normalized.reviewOnlyCount || normalized.manualProblemCount))
    .replace("{rows}", String(normalized.totalRows));
}

function healthTone(operations, pathHealth, capacity, policy, reconciliation) {
  if (!operations?.status && !capacity?.total_bytes) return "unknown";
  if (pathHealth?.readable == null || pathHealth?.writable == null || pathHealth?.available == null) return "unknown";
  if (operations?.status === "critical" || operations?.status === "unavailable" || policy?.recording_suspended_by_low_disk || pathHealth?.writable === false) {
    return "error";
  }
  if (
    operations?.status === "warning" ||
    operations?.status === "degraded" ||
    policy?.state === "warning" ||
    policy?.state === "cleanup_threshold" ||
    pathHealth?.available === false ||
    reconciliation?.problem_file_count > 0
  ) {
    return "warning";
  }
  return "ok";
}

function healthTitle(copy, tone) {
  if (tone === "ok") return copy.healthOkTitle;
  if (tone === "warning") return copy.healthWarningTitle;
  if (tone === "error") return copy.healthCriticalTitle;
  return copy.healthUnknownTitle;
}

function healthText(copy, tone) {
  if (tone === "ok") return copy.healthOkText;
  if (tone === "warning") return copy.healthWarningText;
  if (tone === "error") return copy.healthCriticalText;
  return copy.healthUnknownText;
}

function storageSourceLabel(value, copy) {
  const source = String(value || "");
  const labels = {
    host_bind_env: copy.sourceDeployConfig,
    installer_host_snapshot: copy.sourceInstallerSnapshot,
    setup_wizard: copy.sourceSetupWizard,
    database: copy.sourceDatabase,
  };
  return labels[source] || (source ? source.replaceAll("_", " ") : "-");
}

function archiveRootLabel(root, copy) {
  const label = String(root?.label || root?.name || root?.id || "");
  if (/^default archive$/i.test(label) || /^default$/i.test(label)) return copy.defaultArchive;
  return label || copy.defaultArchive;
}

export default function StorageOperationsPage() {
  const [status, setStatus] = useState(null);
  const statusRef = useRef(null);
  const [settings, setSettings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [refreshWarning, setRefreshWarning] = useState("");
  const [accessDenied, setAccessDenied] = useState(false);
  const [rootPath, setRootPath] = useState("");
  const [rootAction, setRootAction] = useState("");
  const [autoFreeMessage, setAutoFreeMessage] = useState("");
  const [archiveRootMessage, setArchiveRootMessage] = useState("");
  const [retentionMessage, setRetentionMessage] = useState("");
  const [retentionPreview, setRetentionPreview] = useState(null);
  const [retentionResult, setRetentionResult] = useState(null);
  const [retentionConfirmed, setRetentionConfirmed] = useState(false);
  const [reconciliationMessage, setReconciliationMessage] = useState("");
  const [reconciliationPreview, setReconciliationPreview] = useState(null);
  const [reconciliationResult, setReconciliationResult] = useState(null);
  const [reconciliationConfirmed, setReconciliationConfirmed] = useState(false);
  const [migrationMessage, setMigrationMessage] = useState("");
  const [migrationResult, setMigrationResult] = useState(null);
  const { currentUser, status: currentUserStatus } = useCurrentUser();
  const { locale: language, t } = useI18n();
  const copy = useLocaleText("storagePage");

  const canOpenStorage = currentUser ? canAccessPath(currentUser, "/storage") : false;

  const loadStatus = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setRefreshing(true);
    try {
      const [data, settingsData] = await Promise.all([
        apiFetch("/storage/status"),
        apiFetch("/settings").catch(() => null),
      ]);
      setStatus(data);
      statusRef.current = data;
      setSettings(settingsData);
      setError("");
      setRefreshWarning("");
      setAccessDenied(false);
      return true;
    } catch (err) {
      if (isStorageAccessDeniedError(err)) {
        setAccessDenied(true);
        setError(forbiddenMessage(language));
        return false;
      }
      if (silent && statusRef.current) {
        setRefreshWarning(t("storagePage.refreshFailedStale", { time: formatDateTime(statusRef.current?.storage_operations?.checked_at, language) }));
        return false;
      }
      setError(err?.message || copy.loadFailed);
      return false;
    } finally {
      setLoading(false);
      if (!silent) setRefreshing(false);
    }
  }, [language, copy.loadFailed, t]);

  useEffect(() => {
    if (currentUserStatus === "loading") return;
    if (!currentUser || !canOpenStorage) {
      setLoading(false);
      setAccessDenied(true);
      setError(forbiddenMessage(language));
      return;
    }

    let cancelled = false;
    let timer = null;
    async function start() {
      const canContinue = await loadStatus({ silent: true });
      if (!cancelled && canContinue) {
        timer = setInterval(() => loadStatus({ silent: true }), REFRESH_MS);
      }
    }
    start();
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [canOpenStorage, currentUser, currentUserStatus, language, loadStatus]);

  const operations = status?.storage_operations || {};
  const storageContract = status?.storage_contract || {};
  const capacity = operations.capacity || {};
  const pathHealth = operations.path_health || {};
  const namespace = operations.namespace_health || {};
  const owned = operations.owned_archive || {};
  const policy = operations.low_disk_policy || {};
  const autoCleanup = operations.auto_free_space_cleanup || {};
  const retention = operations.retention || {};
  const reconciliation = operations.reconciliation || {};
  const recent = operations.recent_operations || {};
  const archiveRoots = operations.archive_roots || status?.archive_roots || [];
  const migrationPreview = operations.migration_preview || status?.migration_preview || {};
  const cameraRows = useMemo(() => cameraStorageRows(operations.per_camera_usage), [operations.per_camera_usage]);
  const usagePercent = Number(capacity.usage_percent || 0);
  const autoFreeEnabled = settings?.auto_free_space_cleanup_enabled ?? policy.auto_free_space_cleanup_enabled ?? autoCleanup.enabled;
  const topHealth = storageTopHealthModel({ operations, pathHealth, capacity, policy, reconciliation, migrationPreview, retention }, language);
  const tone = topHealth.tone || healthTone(operations, pathHealth, capacity, policy, reconciliation);
  const primaryActionText = primaryStorageActionText({ operations, pathHealth, capacity, policy, reconciliation, migrationPreview, retention }, language);
  const accessRights = accessRightsModel(pathHealth, language);
  const availabilityBadge = pathHealth.available === false ? copy.availabilityNeedsCheck : copy.availabilityConfirmed;
  const normalizedReconciliation = normalizeReconciliationSummary(reconciliation, language);
  const manageSettingsPermission = actionPermissionState(currentUser, "manage_settings", language);
  const retentionPermission = actionPermissionState(currentUser, "delete_recordings", language);
  const diagnosticsPermission = actionPermissionState(currentUser, "run_diagnostics", language);
  const retentionScenario = retentionScenarioModel({
    preview: retentionPreview,
    result: retentionResult,
    retention,
    permission: retentionPermission,
    running: rootAction.startsWith("retention-"),
  }, language);
  const reconciliationScenario = reconciliationScenarioModel({
    preview: reconciliationPreview,
    result: reconciliationResult,
    reconciliation,
    canCheck: diagnosticsPermission,
    canApply: manageSettingsPermission,
    running: rootAction.startsWith("reconciliation-"),
  }, language);
  const migrationScenario = migrationScenarioModel({
    preview: migrationPreview,
    result: migrationResult,
    permission: manageSettingsPermission,
    running: rootAction === "preview" || rootAction === "apply-migration",
  }, language);

  async function setAutoFreeSpace(nextEnabled) {
    if (!manageSettingsPermission.allowed) {
      setAutoFreeMessage(manageSettingsPermission.reason);
      return;
    }
    setRootAction("auto-free");
    setAutoFreeMessage("");
    try {
      const updated = await apiFetch("/settings", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto_free_space_cleanup_enabled: Boolean(nextEnabled) }),
      });
      setSettings(updated);
      setAutoFreeMessage(nextEnabled ? copy.autoFreeEnabled : copy.autoFreeDisabled);
      await loadStatus({ silent: true });
    } catch (err) {
      setAutoFreeMessage(errorDetailText(err, copy.autoFreeChangeFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function validateRoot() {
    if (!rootPath.trim()) return;
    if (!manageSettingsPermission.allowed) {
      setArchiveRootMessage(manageSettingsPermission.reason);
      return;
    }
    setRootAction("validate");
    setArchiveRootMessage("");
    try {
      const result = await apiFetch("/storage/archive-roots/validate", {
        method: "POST",
        body: JSON.stringify({ root_path: rootPath.trim(), create_namespace: false }),
      });
      setArchiveRootMessage(result.ok ? copy.rootAvailable : t("storagePage.validateFailed", { problem: humanBlockerReason(result.problem || "unavailable", language) }));
    } catch (err) {
      setArchiveRootMessage(errorDetailText(err, copy.rootValidateFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function addRoot() {
    if (!rootPath.trim()) return;
    if (!manageSettingsPermission.allowed) {
      setArchiveRootMessage(manageSettingsPermission.reason);
      return;
    }
    setRootAction("add");
    setArchiveRootMessage("");
    try {
      await apiFetch("/storage/archive-roots", {
        method: "POST",
        body: JSON.stringify({ root_path: rootPath.trim(), label: "Archive root", make_active: false, confirm: false }),
      });
      setRootPath("");
      setArchiveRootMessage(copy.rootAdded);
      await loadStatus({ silent: true });
    } catch (err) {
      setArchiveRootMessage(errorDetailText(err, copy.rootNotAdded, language));
    } finally {
      setRootAction("");
    }
  }

  async function activateRoot(rootId) {
    if (!rootId) return;
    if (!manageSettingsPermission.allowed) {
      setArchiveRootMessage(manageSettingsPermission.reason);
      return;
    }
    if (!window.confirm(copy.switchConfirm)) return;
    setRootAction(`activate-${rootId}`);
    setArchiveRootMessage("");
    try {
      await apiFetch(`/storage/archive-roots/${encodeURIComponent(rootId)}/activate`, {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
      setArchiveRootMessage(copy.rootSwitched);
      await loadStatus({ silent: true });
    } catch (err) {
      setArchiveRootMessage(errorDetailText(err, copy.rootNotSwitched, language));
    } finally {
      setRootAction("");
    }
  }

  async function runRetentionPreview() {
    if (!retentionPermission.allowed) {
      setRetentionMessage(retentionPermission.reason);
      return;
    }
    setRootAction("retention-preview");
    setRetentionResult(null);
    setRetentionConfirmed(false);
    setRetentionMessage("");
    try {
      const preview = await apiFetch("/recordings/retention/dry-run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      setRetentionPreview(preview);
      setRetentionMessage(copy.retentionPreviewCompleted);
    } catch (err) {
      setRetentionMessage(errorDetailText(err, copy.previewFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function applyRetentionPlan() {
    if (!retentionPreview?.planned_count || !retentionConfirmed) return;
    if (!retentionPermission.allowed) {
      setRetentionMessage(retentionPermission.reason);
      return;
    }
    setRootAction("retention-apply");
    setRetentionMessage("");
    try {
      const result = await apiFetch("/recordings/retention/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      setRetentionResult(result);
      setRetentionPreview(null);
      setRetentionConfirmed(false);
      setRetentionMessage(copy.retentionApplyCompleted);
      await loadStatus({ silent: true });
    } catch (err) {
      setRetentionMessage(errorDetailText(err, copy.applyBlocked, language));
    } finally {
      setRootAction("");
    }
  }

  async function runReconciliationPreview() {
    if (!diagnosticsPermission.allowed) {
      setReconciliationMessage(diagnosticsPermission.reason);
      return;
    }
    setRootAction("reconciliation-preview");
    setReconciliationResult(null);
    setReconciliationConfirmed(false);
    setReconciliationMessage("");
    try {
      const preview = await apiFetch("/storage/reconciliation/summary");
      setReconciliationPreview(preview);
      setReconciliationMessage(copy.reconciliationCheckCompleted);
    } catch (err) {
      setReconciliationMessage(errorDetailText(err, copy.previewFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function applyReconciliationSafe() {
    if (!reconciliationPreview || !reconciliationConfirmed) return;
    if (!manageSettingsPermission.allowed) {
      setReconciliationMessage(manageSettingsPermission.reason);
      return;
    }
    setRootAction("reconciliation-apply");
    setReconciliationMessage("");
    try {
      const result = await apiFetch("/storage/reconcile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: "apply_safe" }),
      });
      setReconciliationResult(result);
      setReconciliationPreview(null);
      setReconciliationConfirmed(false);
      setReconciliationMessage(copy.reconciliationApplyCompleted);
      await loadStatus({ silent: true });
    } catch (err) {
      setReconciliationMessage(errorDetailText(err, copy.applyBlocked, language));
    } finally {
      setRootAction("");
    }
  }

  async function refreshMigrationPreview() {
    if (!manageSettingsPermission.allowed) {
      setMigrationMessage(manageSettingsPermission.reason);
      return;
    }
    setRootAction("preview");
    setMigrationMessage("");
    try {
      await apiFetch("/storage/migration/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_root_id: null }),
      });
      setMigrationMessage(copy.previewUpdated);
      await loadStatus({ silent: true });
    } catch (err) {
      setMigrationMessage(errorDetailText(err, copy.previewFailed, language));
    } finally {
      setRootAction("");
    }
  }

  async function applyMigration() {
    if (!migrationPreview?.apply_available) return;
    if (!manageSettingsPermission.allowed) {
      setMigrationMessage(manageSettingsPermission.reason);
      return;
    }
    if (!window.confirm(copy.applyConfirm)) return;
    setRootAction("apply-migration");
    setMigrationMessage("");
    setMigrationResult(null);
    try {
      const result = await apiFetch("/storage/migration/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_root_id: migrationPreview.target_root_id || null,
          plan_id: migrationPreview.plan_id || null,
          confirm: true,
        }),
      });
      setMigrationResult(result);
      setMigrationMessage(copy.applyCompleted);
      await loadStatus({ silent: true });
    } catch (err) {
      const detail = err?.detail || err?.data?.detail || null;
      setMigrationResult(detail && typeof detail === "object" ? detail : { status: "blocked", blockers: [{ reason: err?.message || copy.applyBlocked }] });
      setMigrationMessage(errorDetailText(err, copy.applyBlocked, language));
    } finally {
      setRootAction("");
    }
  }

  return (
    <Layout>
      <div className="storageOpsPage">
        <header className="pageHeader storageOpsHeader">
          <div>
            <h1 className="pageTitle">{copy.title}</h1>
            <div className="pageSubtitle">{copy.subtitle}</div>
          </div>
          <button className="button secondary small" type="button" onClick={() => loadStatus()} disabled={refreshing || loading || accessDenied}>
            {refreshing ? copy.refreshing : copy.refresh}
          </button>
        </header>

        {loading ? (
          <div className="storageOpsState">{copy.loading}</div>
        ) : accessDenied ? (
          <div className="storageOpsState storageOpsState-error">{error || forbiddenMessage(language)}</div>
        ) : error ? (
          <div className="storageOpsState storageOpsState-error">{error}</div>
        ) : (
          <>
            <section className={`storageOpsOverview storageOpsOverview-${tone}`}>
              <div className="storageOpsHealthMain">
                <span className="storageOpsEyebrow">{copy.lastCheck}: {formatDateTime(operations.checked_at, language)}</span>
                <strong>{healthTitle(copy, tone)}</strong>
                <p>{topHealth.reason || healthText(copy, tone)}</p>
              </div>
              <div className="storageOpsHealthMetrics">
                <Stat label={copy.total} value={formatBytes(capacity.total_bytes)} />
                <Stat label={copy.used} value={`${formatBytes(capacity.used_bytes)} / ${formatPercent(capacity.usage_percent)}`} />
                <Stat label={copy.free} value={`${formatBytes(capacity.free_bytes)} / ${formatPercent(capacity.free_percent)}`} tone={freeSpaceTone(capacity, policy)} />
                <Badge label={accessRights.label} tone={accessRights.tone} />
                <Badge label={availabilityBadge} tone={pathHealth.available === false ? "warning" : "ok"} />
              </div>
              <div className="storageOpsHealthAction">
                <strong>{copy.primaryAction}</strong>
                <span>{primaryActionText}</span>
              </div>
            </section>
            {refreshWarning ? <div className="storageOpsState storageOpsState-warning">{refreshWarning}</div> : null}

            <div className="storageOpsGrid">
              <Section title={copy.archiveSpace} className="storageOpsSection-wide">
                <div className="storageOpsCapacityBar" aria-label={copy.storageUsage}>
                  <span style={{ width: `${Math.max(0, Math.min(100, usagePercent))}%` }} />
                </div>
                <div className="storageOpsStats">
                  <Stat label={copy.archiveSize} value={formatBytes(owned.size_bytes)} />
                  <Stat label={copy.segments} value={String(owned.segments_count || 0)} />
                  <Stat label={copy.problems} value={String(normalizedReconciliation.problemCount || 0)} tone={normalizedReconciliation.problemCount ? "warning" : "neutral"} />
                </div>
              </Section>

              <Section title={copy.safeActions}>
                <div className="storageOpsActions storageOpsActionsColumn">
                  <button className="button secondary small" type="button" onClick={runReconciliationPreview} disabled={!!rootAction || !reconciliationScenario.canCheck}>{rootAction === "reconciliation-preview" ? copy.checking : copy.reconciliationDryRun}</button>
                  <button className="button secondary small" type="button" onClick={runRetentionPreview} disabled={!!rootAction || !retentionScenario.canPreview}>{rootAction === "retention-preview" ? copy.calculating : copy.retentionDryRun}</button>
                  <button className="button secondary small" type="button" onClick={refreshMigrationPreview} disabled={!!rootAction || !migrationScenario.canPreview}>{rootAction === "preview" ? copy.calculating : copy.refreshMigrationPreview}</button>
                </div>
                <div className="storageOpsNote">{copy.safeActionNote}</div>
                {!reconciliationScenario.canCheck ? <div className="storageOpsNote storageOpsNoteStrong">{reconciliationScenario.checkPermissionReason}</div> : null}
                {!retentionScenario.canPreview ? <div className="storageOpsNote storageOpsNoteStrong">{retentionScenario.permissionReason}</div> : null}
              </Section>

              <Section title={copy.archivePath} className="storageOpsSection-diagnostics">
                <div className="storageOpsStats">
                  <Stat label={copy.archiveRootLocation} value={storageContract.archive_primary_path || storageContract.archive_host_path || storageContract.storage_host_path || "-"} />
                  <Stat label={copy.source} value={storageSourceLabel(storageContract.archive_primary_path_source, copy)} />
                </div>
                <SummaryRow label={copy.accessExplanation} value={pathHealth.available === false ? copy.accessLimitedText : copy.accessOkText} tone={pathHealth.available === false ? "warning" : "neutral"} />
              </Section>

              <Section title={copy.autoFreeSpace}>
                <div className="storageOpsStats">
                  <Stat label={copy.state} value={autoFreeEnabled ? copy.on : copy.off} tone={autoFreeEnabled ? "ok" : "neutral"} />
                  <Stat label={copy.lastRun} value={formatDateTime(autoCleanup.last_finished_at || autoCleanup.last_started_at, language)} />
                  <Stat label={copy.deleted} value={String(autoCleanup.last_summary?.deleted_count || 0)} />
                  <Stat label={copy.freed} value={formatBytes(autoCleanup.last_summary?.bytes_freed)} />
                </div>
                <div className="storageOpsActions">
                  <button className="button secondary small" type="button" onClick={() => setAutoFreeSpace(!autoFreeEnabled)} disabled={!!rootAction || !manageSettingsPermission.allowed}>
                    {rootAction === "auto-free" ? copy.saving : autoFreeEnabled ? copy.disableAutoFree : copy.enableAutoFree}
                  </button>
                </div>
                {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                {autoFreeMessage ? <div className="storageOpsNote storageOpsNoteStrong">{autoFreeMessage}</div> : null}
                <div className="storageOpsNote">{copy.lowDiskNote}</div>
                <div className="storageOpsNote">{copy.autoFreeNote}</div>
                <SummaryRow label={copy.blockersReasons} value={reasonText(autoCleanup.last_summary, copy, language)} />
              </Section>

              <Section title={copy.retention}>
                <div className="storageOpsStats">
                  <Stat label={copy.state} value={statusLabel(retention.last_status, language)} />
                  <Stat label={copy.lastRun} value={formatDateTime(retention.last_finished_at || retention.last_started_at, language)} />
                  <Stat label={copy.deleted} value={String(retention.last_summary?.deleted_count || 0)} />
                  <Stat label={copy.freed} value={formatBytes(retention.last_summary?.bytes_freed)} />
                </div>
                <SummaryRow label={copy.actionState} value={statusLabel(retentionScenario.status, language)} tone={retentionScenario.status.includes("failed") || retentionScenario.status.includes("permission") ? "warning" : "neutral"} />
                <SummaryRow label={copy.preview} value={retentionSummaryText(retentionPreview || retentionResult, copy)} />
                <div className="storageOpsActions">
                  <button className="button secondary small" type="button" onClick={runRetentionPreview} disabled={!!rootAction || !retentionScenario.canPreview}>{rootAction === "retention-preview" ? copy.calculating : copy.retentionDryRun}</button>
                  <label className="storageOpsConfirm">
                    <input type="checkbox" checked={retentionConfirmed} onChange={(event) => setRetentionConfirmed(event.target.checked)} disabled={!retentionScenario.canApply || !!rootAction} />
                    <span>{copy.retentionConfirm}</span>
                  </label>
                  <button className="button small dangerButton" type="button" onClick={applyRetentionPlan} disabled={!retentionScenario.canApply || !retentionConfirmed || !!rootAction}>{rootAction === "retention-apply" ? copy.applying : copy.retentionApply}</button>
                </div>
                {!retentionPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{retentionPermission.reason}</div> : null}
                {retentionMessage ? <div className="storageOpsNote storageOpsNoteStrong">{retentionMessage}</div> : null}
                <div className="storageOpsNote">{copy.retentionSafetyNote}</div>
              </Section>

              <Section title={copy.integrity}>
                <div className="storageOpsStats">
                  <Stat label={copy.state} value={statusLabel(normalizedReconciliation.status, language)} tone={normalizedReconciliation.problemCount ? "warning" : "neutral"} />
                  <Stat label={copy.problems} value={String(normalizedReconciliation.problemCount || 0)} />
                  <Stat label={copy.safeFixes} value={String(normalizedReconciliation.safeFixCount || 0)} />
                  <Stat label={copy.manualReview} value={String(normalizedReconciliation.reviewOnlyCount || normalizedReconciliation.manualProblemCount || 0)} />
                  <Stat label={copy.lastCheck} value={formatDateTime(reconciliation.last_checked_at, language)} />
                </div>
                <SummaryRow label={copy.actionState} value={statusLabel(reconciliationScenario.status, language)} tone={reconciliationScenario.status.includes("permission") ? "warning" : "neutral"} />
                <SummaryRow label={copy.preview} value={reconciliationSummaryText(reconciliationPreview || reconciliationResult, copy)} />
                <div className="storageOpsActions">
                  <button className="button secondary small" type="button" onClick={runReconciliationPreview} disabled={!!rootAction || !reconciliationScenario.canCheck}>{rootAction === "reconciliation-preview" ? copy.checking : copy.reconciliationDryRun}</button>
                  <label className="storageOpsConfirm">
                    <input type="checkbox" checked={reconciliationConfirmed} onChange={(event) => setReconciliationConfirmed(event.target.checked)} disabled={!reconciliationScenario.canApply || !!rootAction} />
                    <span>{copy.reconciliationConfirm}</span>
                  </label>
                  <button className="button small" type="button" onClick={applyReconciliationSafe} disabled={!reconciliationScenario.canApply || !reconciliationConfirmed || !!rootAction}>{rootAction === "reconciliation-apply" ? copy.applying : copy.reconciliationApply}</button>
                </div>
                {reconciliationScenario.noAutoFixReason ? <div className="storageOpsNote storageOpsNoteStrong">{copy.reconciliationNoAutoFixes}</div> : null}
                {reconciliationScenario.categories?.length ? (
                  <div className="storageOpsNote">
                    {copy.reconciliationCategories}: {reconciliationScenario.categories.map((item) => `${item.label}: ${item.count}`).join(", ")}
                  </div>
                ) : null}
                {!diagnosticsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{diagnosticsPermission.reason}</div> : null}
                {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{reconciliationScenario.applyPermissionReason}</div> : null}
                {reconciliationMessage ? <div className="storageOpsNote storageOpsNoteStrong">{reconciliationMessage}</div> : null}
                <div className="storageOpsNote">{copy.integrityNote}</div>
              </Section>

              <Section title={copy.archiveRoots}>
                <div className="storageOpsNote">{copy.rootsNote}</div>
                <div className="storageOpsTableWrap">
                  <table className="storageOpsTable storageOpsTable-compact">
                    <thead>
                      <tr><th>{copy.root}</th><th>{copy.state}</th><th>{copy.records}</th><th>{copy.size}</th><th>{copy.action}</th></tr>
                    </thead>
                    <tbody>
                      {archiveRoots.map((root) => {
                        const rootScenario = archiveRootScenarioModel({
                          root,
                          permission: manageSettingsPermission,
                          running: rootAction === `activate-${root.id}`,
                        }, language);
                        return (
                          <tr key={root.id}>
                            <td><strong>{archiveRootLabel(root, copy)}</strong><span>{root.is_active ? copy.activeRoot : copy.oldInactive}</span></td>
                            <td>{root.is_available ? copy.available : (rootScenario.reason || copy.unavailable)}</td>
                            <td>{root.segments_count || 0}</td>
                            <td>{formatBytes(root.size_bytes)}</td>
                            <td><button className="button secondary small" type="button" disabled={!rootScenario.canActivate} onClick={() => activateRoot(root.id)}>{rootAction === `activate-${root.id}` ? copy.switching : copy.makeActive}</button></td>
                          </tr>
                        );
                      })}
                      {!archiveRoots.length ? <tr><td colSpan="5">{copy.noRoots}</td></tr> : null}
                    </tbody>
                  </table>
                </div>
                <details className="storageOpsDetails storageOpsAdvancedRoot">
                  <summary>{copy.addArchiveRoot}</summary>
                  <div className="storageOpsAdvancedRootBody">
                    <div className="storageOpsNote">{copy.addArchiveRootNote}</div>
                    <div className="storageOpsRootForm">
                      <input className="input" value={rootPath} onChange={(event) => setRootPath(event.target.value)} placeholder={copy.archiveRootPlaceholder} />
                      <button className="button secondary small" type="button" onClick={validateRoot} disabled={!!rootAction || !rootPath.trim() || !manageSettingsPermission.allowed}>{rootAction === "validate" ? copy.validating : copy.validate}</button>
                      <button className="button small" type="button" onClick={addRoot} disabled={!!rootAction || !rootPath.trim() || !manageSettingsPermission.allowed}>{rootAction === "add" ? copy.adding : copy.add}</button>
                    </div>
                    {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                    {archiveRootMessage ? <div className="storageOpsNote storageOpsNoteStrong">{archiveRootMessage}</div> : null}
                  </div>
                </details>
              </Section>

              <Section title={copy.migrationPreview}>
                <div className="storageOpsStats">
                  <Stat label={copy.move} value={`${migrationPreview.total_would_move_count || 0} / ${formatBytes(migrationPreview.total_would_move_bytes)}`} />
                  <Stat label={copy.willStay} value={String(migrationPreview.total_would_stay_count || 0)} />
                  <Stat label={copy.previewOnlyMigration} value={copy.yes} />
                  <Stat label={copy.blockers} value={String((migrationPreview.blockers || []).length)} tone={(migrationPreview.blockers || []).length ? "warning" : "neutral"} />
                  <Stat label={copy.applyState} value={migrationPreview.apply_available ? copy.available : copy.unavailable} tone={migrationPreview.apply_available ? "ok" : "warning"} />
                </div>
                <SummaryRow label={copy.actionState} value={statusLabel(migrationScenario.status, language)} tone={migrationScenario.status.includes("blocked") || migrationScenario.status.includes("failed") || migrationScenario.status.includes("permission") ? "warning" : "neutral"} />
                <div className="storageOpsNote">{copy.migrationNote}</div>
                <div className="storageOpsActions">
                  <button className="button secondary small" type="button" onClick={refreshMigrationPreview} disabled={!!rootAction || !migrationScenario.canPreview}>{rootAction === "preview" ? copy.calculating : copy.refreshMigrationPreview}</button>
                  <button className="button small" type="button" onClick={applyMigration} disabled={!!rootAction || !migrationScenario.canApply}>{rootAction === "apply-migration" ? copy.applying : copy.applyMigration}</button>
                </div>
                {migrationScenario.blockerReason ? <SummaryRow label={copy.blockers} value={migrationScenario.blockerReason} tone="warning" /> : null}
                {!manageSettingsPermission.allowed ? <div className="storageOpsNote storageOpsNoteStrong">{manageSettingsPermission.reason}</div> : null}
                {migrationMessage ? <div className="storageOpsNote storageOpsNoteStrong">{migrationMessage}</div> : null}
                {migrationResult ? <SummaryRow label={copy.applyReport} value={`${statusLabel(migrationResult.status, language)}; ${copy.executed}: ${(migrationResult.executed || []).length}; ${copy.sourcePreserved}: ${boolLabel(migrationResult.source_preserved, language)}; ${copy.cleanupPending}: ${boolLabel(migrationResult.cleanup_pending, language)}`} /> : null}
                {migrationScenario.manualReviewRequired ? <div className="storageOpsNote">{copy.migrationManualReview}</div> : null}
              </Section>

              <Section title={copy.diagnostics}>
                <details className="storageOpsDetails">
                  <summary>{copy.technicalDetails}</summary>
                  <dl>
                    <div><dt>{copy.namespaceState}</dt><dd>{namespace.storage_namespace || "-"}</dd></div>
                    <div><dt>{copy.namespaceExists}</dt><dd>{boolLabel(namespace.namespace_exists, language)}</dd></div>
                    <div><dt>{copy.scanMode}</dt><dd>{namespace.scan_mode || "-"}</dd></div>
                    <div><dt>{copy.partialScanReason}</dt><dd>{namespace.partial_reason || "-"}</dd></div>
                    <div><dt>{copy.dockerPath}</dt><dd>{storageContract.storage_container_path || storageContract.container_runtime_storage_root || status?.container_runtime_storage_root || "/storage/archive"}</dd></div>
                    <div><dt>{copy.missingNoMetadata}</dt><dd>{`${reconciliation.missing_file_count || 0} / ${reconciliation.orphan_file_count || 0}`}</dd></div>
                    <div><dt>{copy.reasonOrphanFile}</dt><dd>{String(reconciliation.orphan_file_count || 0)}</dd></div>
                    <div><dt>{copy.invalidOutside}</dt><dd>{`${reconciliation.invalid_path_count || 0} / ${reconciliation.path_outside_storage_count || 0}`}</dd></div>
                    <div><dt>{copy.ownershipBoundary}</dt><dd>{copy.ownershipBoundaryText}</dd></div>
                    <div><dt>{copy.foreignSkipped}</dt><dd>{String(owned.skipped_foreign_metadata_rows || 0)}</dd></div>
                    <div><dt>{copy.deletedExcluded}</dt><dd>{String(owned.deleted_metadata_rows_excluded || 0)}</dd></div>
                  </dl>
                </details>
              </Section>
            </div>

            <Section title={copy.byCameras}>
              {cameraRows.length ? (
                <div className="storageOpsTableWrap">
                  <table className="storageOpsTable">
                    <thead>
                      <tr>
                        <th>{copy.camera}</th>
                        <th>{copy.size}</th>
                        <th>{copy.segments}</th>
                        <th>{copy.missingFiles} / {copy.problems}</th>
                        <th>{copy.range}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {cameraRows.map((row) => (
                        <tr key={row.camera_id || row.camera_name}>
                          <td><strong>{row.camera_name}</strong><span>ID {row.camera_id || "-"}</span></td>
                          <td>{formatBytes(row.size_bytes)}</td>
                          <td>{row.segment_count}</td>
                          <td>{row.missing_file_count} / {row.problem_file_count}</td>
                          <td>{formatDateTime(row.oldest_recording_at, language)} - {formatDateTime(row.newest_recording_at, language)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="storageOpsEmpty">{copy.noCameraOwned}</div>
              )}
            </Section>

            {recent.available && recent.items?.length ? (
              <Section title={copy.recentOperations}>
                <div className="storageOpsRecent">
                  {recent.items.map((item, index) => (
                    <div className="storageOpsRecentItem" key={`${item.type || "operation"}-${index}`}>
                      <strong>{item.title || statusLabel(item.type, language)}</strong>
                      <span>{item.summary || statusLabel(item.status, language)}</span>
                    </div>
                  ))}
                </div>
              </Section>
            ) : null}
          </>
        )}
      </div>
    </Layout>
  );
}
