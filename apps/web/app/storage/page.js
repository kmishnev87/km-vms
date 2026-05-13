"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
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
  lowDiskPolicyText,
  policyStateLabel,
  statusLabel,
  topReasonEntries,
} from "../../lib/storageOperations";

const REFRESH_MS = 30000;

function isAccessDenied(error) {
  const message = String(error?.message || error || "").toLowerCase();
  return message.includes("401") || message.includes("403") || message.includes("permission") || message.includes("\u0434\u043e\u0441\u0442\u0443\u043f");
}

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

function SummaryRow({ label, value }) {
  return (
    <div className="storageOpsSummaryRow">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Section({ title, children, action = null }) {
  return (
    <section className="storageOpsSection">
      <div className="storageOpsSectionHead">
        <h2>{title}</h2>
        {action}
      </div>
      {children}
    </section>
  );
}

function reasonText(summary, copy) {
  const entries = topReasonEntries(summary);
  if (!entries.length) return copy.noReasons;
  const labels = {
    missing_file: copy.reasonMissingFile,
    orphan_file: copy.reasonOrphanFile,
    invalid_path: copy.reasonInvalidPath,
    path_outside_storage: copy.reasonOutsideStorage,
    unknown: copy.reasonUnknown,
  };
  return entries.map(([key, value]) => `${labels[key] || String(key).replaceAll("_", " ")}: ${value}`).join(", ");
}

export default function StorageOperationsPage() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [accessDenied, setAccessDenied] = useState(false);
  const [rootPath, setRootPath] = useState("");
  const [rootAction, setRootAction] = useState("");
  const [rootMessage, setRootMessage] = useState("");
  const [migrationResult, setMigrationResult] = useState(null);
  const { currentUser, status: currentUserStatus } = useCurrentUser();
  const { locale: language, t } = useI18n();
  const copy = useLocaleText("storagePage");

  const canOpenStorage = currentUser ? canAccessPath(currentUser, "/storage") : false;

  const loadStatus = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setRefreshing(true);
    try {
      const data = await apiFetch("/storage/status");
      setStatus(data);
      setError("");
      setAccessDenied(false);
      return true;
    } catch (err) {
      if (isAccessDenied(err)) {
        setAccessDenied(true);
        setError(forbiddenMessage(language));
        return false;
      }
      setError(err?.message || copy.loadFailed);
      return false;
    } finally {
      setLoading(false);
      if (!silent) setRefreshing(false);
    }
  }, [language, copy.loadFailed]);

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

  async function validateRoot() {
    if (!rootPath.trim()) return;
    setRootAction("validate");
    setRootMessage("");
    try {
      const result = await apiFetch("/storage/archive-roots/validate", {
        method: "POST",
        body: JSON.stringify({ root_path: rootPath.trim(), create_namespace: false }),
      });
      setRootMessage(result.ok ? copy.rootAvailable : t("storagePage.validateFailed", { problem: result.problem || copy.unavailable }));
    } catch (err) {
      setRootMessage(err?.message || copy.rootValidateFailed);
    } finally {
      setRootAction("");
    }
  }

  async function addRoot() {
    if (!rootPath.trim()) return;
    setRootAction("add");
    setRootMessage("");
    try {
      await apiFetch("/storage/archive-roots", {
        method: "POST",
        body: JSON.stringify({ root_path: rootPath.trim(), label: "Archive root", make_active: false, confirm: false }),
      });
      setRootPath("");
      setRootMessage(copy.rootAdded);
      await loadStatus({ silent: true });
    } catch (err) {
      setRootMessage(err?.message || copy.rootNotAdded);
    } finally {
      setRootAction("");
    }
  }

  async function activateRoot(rootId) {
    if (!rootId) return;
    if (!window.confirm(copy.switchConfirm)) return;
    setRootAction(`activate-${rootId}`);
    setRootMessage("");
    try {
      await apiFetch(`/storage/archive-roots/${encodeURIComponent(rootId)}/activate`, {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      });
      setRootMessage(copy.rootSwitched);
      await loadStatus({ silent: true });
    } catch (err) {
      setRootMessage(err?.message || copy.rootNotSwitched);
    } finally {
      setRootAction("");
    }
  }

  async function refreshMigrationPreview() {
    setRootAction("preview");
    setRootMessage("");
    try {
      await apiFetch("/storage/migration/preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target_root_id: null }),
      });
      setRootMessage(copy.previewUpdated);
      await loadStatus({ silent: true });
    } catch (err) {
      setRootMessage(err?.message || copy.previewFailed);
    } finally {
      setRootAction("");
    }
  }

  async function applyMigration() {
    if (!migrationPreview?.apply_available) return;
    if (!window.confirm(copy.applyConfirm)) return;
    setRootAction("apply-migration");
    setRootMessage("");
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
      setRootMessage(copy.applyCompleted);
      await loadStatus({ silent: true });
    } catch (err) {
      const detail = err?.detail || err?.data?.detail || null;
      setMigrationResult(detail && typeof detail === "object" ? detail : { status: "blocked", blockers: [{ reason: err?.message || copy.applyBlocked }] });
      setRootMessage(err?.message || copy.applyBlocked);
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
            <section className="storageOpsOverview">
              <div>
                <span className="storageOpsEyebrow">{copy.lastCheck}: {formatDateTime(operations.checked_at, language)}</span>
                <strong>{statusLabel(operations.status, language)}</strong>
                <p>{lowDiskPolicyText(policy, language)}</p>
              </div>
              <div className="storageOpsBadges">
                <Badge label={`${copy.read}: ${boolLabel(pathHealth.readable, language)}`} tone={pathHealth.readable ? "ok" : "error"} />
                <Badge label={`${copy.write}: ${boolLabel(pathHealth.writable, language)}`} tone={pathHealth.writable ? "ok" : "error"} />
                <Badge label={`${copy.availability}: ${boolLabel(pathHealth.available, language)}`} tone={pathHealth.available ? "ok" : "error"} />
              </div>
            </section>

            <div className="storageOpsGrid">
              <Section title={copy.archivePath}>
                <div className="storageOpsStats">
                  <Stat label="NAS/server" value={storageContract.archive_primary_path || storageContract.archive_host_path || storageContract.storage_host_path || "-"} />
                  <Stat label="Docker" value={storageContract.storage_container_path || storageContract.container_runtime_storage_root || status?.container_runtime_storage_root || "/storage/archive"} />
                  <Stat label={copy.source} value={storageContract.archive_primary_path_source || "-"} />
                </div>
              </Section>

              <Section title={copy.capacity}>
                <div className="storageOpsCapacityBar" aria-label={copy.storageUsage}>
                  <span style={{ width: `${Math.max(0, Math.min(100, usagePercent))}%` }} />
                </div>
                <div className="storageOpsStats">
                  <Stat label={copy.total} value={formatBytes(capacity.total_bytes)} />
                  <Stat label={copy.used} value={`${formatBytes(capacity.used_bytes)} / ${formatPercent(capacity.usage_percent)}`} />
                  <Stat label={copy.free} value={`${formatBytes(capacity.free_bytes)} / ${formatPercent(capacity.free_percent)}`} tone={policy.state === "critical" ? "error" : policy.state === "warning" || policy.state === "cleanup_threshold" ? "warning" : "neutral"} />
                </div>
              </Section>

              <Section title={copy.lowDiskPolicy}>
                <div className="storageOpsStats">
                  <Stat label={copy.policy} value={policyStateLabel(policy, language)} tone={policy.auto_free_space_cleanup_enabled ? "ok" : "neutral"} />
                  <Stat label={copy.warning} value={`<${policy.warning_threshold_percent ?? 10}% ${copy.freeSuffix}`} />
                  <Stat label={copy.autoFree} value={policy.auto_free_space_cleanup_enabled ? `<${policy.cleanup_threshold_percent ?? 5}% ${copy.freeSuffix}` : copy.off} />
                  <Stat label={copy.critical} value={`<${policy.critical_threshold_percent ?? 1}% ${copy.freeSuffix}`} tone={policy.recording_suspended_by_low_disk ? "error" : "neutral"} />
                </div>
                <div className="storageOpsNote">
                  {copy.lowDiskNote}
                </div>
                <SummaryRow label={copy.recordingSuspended} value={boolLabel(policy.recording_suspended_by_low_disk, language)} />
              </Section>

              <Section title={copy.autoFreeSpace}>
                <div className="storageOpsStats">
                  <Stat label={copy.state} value={autoCleanup.enabled ? copy.on : copy.off} />
                  <Stat label={copy.lastRun} value={formatDateTime(autoCleanup.last_finished_at || autoCleanup.last_started_at, language)} />
                  <Stat label={copy.deleted} value={String(autoCleanup.last_summary?.deleted_count || 0)} />
                  <Stat label={copy.freed} value={formatBytes(autoCleanup.last_summary?.bytes_freed)} />
                </div>
                <div className="storageOpsNote">{copy.autoFreeNote}</div>
                <SummaryRow label={copy.blockersReasons} value={reasonText(autoCleanup.last_summary, copy)} />
                {autoCleanup.last_error ? <SummaryRow label={copy.lastError} value={autoCleanup.last_error} /> : null}
              </Section>

              <Section title={copy.kmArchive}>
                <div className="storageOpsStats">
                  <Stat label={copy.archiveSize} value={formatBytes(owned.size_bytes)} />
                  <Stat label={copy.segments} value={String(owned.segments_count || 0)} />
                  <Stat label={copy.filesPresent} value={String(owned.existing_file_count || 0)} />
                  <Stat label={copy.problems} value={String(owned.problem_file_count || 0)} tone={owned.problem_file_count ? "warning" : "neutral"} />
                </div>
                <SummaryRow label={copy.missingFiles} value={String(owned.missing_file_count || 0)} />
                <SummaryRow label={copy.foreignSkipped} value={String(owned.skipped_foreign_metadata_rows || 0)} />
                <SummaryRow label={copy.deletedExcluded} value={String(owned.deleted_metadata_rows_excluded || 0)} />
              </Section>

              <Section title={copy.retention} action={<Link className="button secondary small" href="/settings">{copy.openWorkflow}</Link>}>
                <div className="storageOpsStats">
                  <Stat label={copy.state} value={statusLabel(retention.last_status, language)} />
                  <Stat label={copy.lastRun} value={formatDateTime(retention.last_finished_at || retention.last_started_at, language)} />
                  <Stat label={copy.deleted} value={String(retention.last_summary?.deleted_count || 0)} />
                  <Stat label={copy.skippedErrors} value={`${retention.last_summary?.skipped_count || 0} / ${retention.last_summary?.failed_count || 0}`} />
                </div>
                <SummaryRow label={copy.reasonsBlockers} value={reasonText(retention.last_summary, copy)} />
                <SummaryRow label={copy.freed} value={formatBytes(retention.last_summary?.bytes_freed)} />
              </Section>

              <Section title={copy.integrity} action={<Link className="button secondary small" href="/settings">{copy.openCheck}</Link>}>
                <div className="storageOpsStats">
                  <Stat label={copy.state} value={statusLabel(reconciliation.status, language)} tone={reconciliation.problem_file_count ? "warning" : "neutral"} />
                  <Stat label={copy.problems} value={String(reconciliation.problem_file_count || 0)} />
                  <Stat label={copy.candidates} value={String(reconciliation.cleanup_candidate_count || 0)} />
                  <Stat label={copy.lastCheck} value={formatDateTime(reconciliation.last_checked_at, language)} />
                </div>
                <SummaryRow label={copy.missingNoMetadata} value={`${reconciliation.missing_file_count || 0} / ${reconciliation.orphan_file_count || 0}`} />
                <SummaryRow label={copy.invalidOutside} value={`${reconciliation.invalid_path_count || 0} / ${reconciliation.path_outside_storage_count || 0}`} />
                <div className="storageOpsNote">{copy.integrityNote}</div>
              </Section>

              <Section title={copy.namespaceState}>
                <div className="storageOpsStats">
                  <Stat label="Namespace" value={namespace.storage_namespace || "-"} />
                  <Stat label={copy.namespaceExists} value={boolLabel(namespace.namespace_exists, language)} />
                  <Stat label={copy.scanMode} value={namespace.scan_mode || "-"} />
                  <Stat label={copy.partialLimited} value={`${boolLabel(namespace.partial, language)} / ${boolLabel(namespace.scan_limited, language)}`} />
                </div>
                {namespace.partial_reason ? <SummaryRow label={copy.partialScanReason} value={namespace.partial_reason} /> : null}
              </Section>

              <Section title={copy.archiveRoots}>
                <div className="storageOpsNote">{copy.rootsNote}</div>
                <div className="storageOpsStats">
                  <Stat label={copy.rootsCount} value={String(archiveRoots.length || 0)} />
                  <Stat label={copy.active} value={(archiveRoots.find((root) => root.is_active)?.label || archiveRoots.find((root) => root.is_active)?.id || "-")} />
                  <Stat label={copy.previewOnlyMigration} value={migrationPreview.apply_available === false ? copy.yes : copy.no} />
                </div>
                <div className="storageOpsTableWrap">
                  <table className="storageOpsTable">
                    <thead>
                      <tr><th>{copy.root}</th><th>{copy.state}</th><th>{copy.records}</th><th>{copy.size}</th><th>{copy.action}</th></tr>
                    </thead>
                    <tbody>
                      {archiveRoots.map((root) => (
                        <tr key={root.id}>
                          <td><strong>{root.label || root.id}</strong><span>{root.is_active ? copy.activeRoot : copy.oldInactive}</span></td>
                          <td>{root.is_available ? copy.available : (root.problem || copy.unavailable)}</td>
                          <td>{root.segments_count || 0}</td>
                          <td>{formatBytes(root.size_bytes)}</td>
                          <td><button className="button secondary small" type="button" disabled={root.is_active || rootAction === `activate-${root.id}`} onClick={() => activateRoot(root.id)}>{rootAction === `activate-${root.id}` ? copy.switching : copy.makeActive}</button></td>
                        </tr>
                      ))}
                      {!archiveRoots.length ? <tr><td colSpan="5">{copy.noRoots}</td></tr> : null}
                    </tbody>
                  </table>
                </div>
                <div className="storageOpsRootForm">
                  <input className="input" value={rootPath} onChange={(event) => setRootPath(event.target.value)} placeholder="/storage/archive2" />
                  <button className="button secondary small" type="button" onClick={validateRoot} disabled={!!rootAction || !rootPath.trim()}>{rootAction === "validate" ? copy.validating : copy.validate}</button>
                  <button className="button small" type="button" onClick={addRoot} disabled={!!rootAction || !rootPath.trim()}>{rootAction === "add" ? copy.adding : copy.add}</button>
                </div>
                {rootMessage ? <div className="storageOpsNote">{rootMessage}</div> : null}
              </Section>

              <Section title={copy.migrationPreview}>
                <div className="storageOpsStats">
                  <Stat label={copy.move} value={`${migrationPreview.total_would_move_count || 0} / ${formatBytes(migrationPreview.total_would_move_bytes)}`} />
                  <Stat label={copy.willStay} value={String(migrationPreview.total_would_stay_count || 0)} />
                  <Stat label={copy.blockers} value={String((migrationPreview.blockers || []).length)} tone={(migrationPreview.blockers || []).length ? "warning" : "neutral"} />
                  <Stat label={copy.applyState} value={migrationPreview.apply_available ? copy.available : copy.unavailable} tone={migrationPreview.apply_available ? "ok" : "warning"} />
                </div>
                <div className="storageOpsNote">{copy.migrationNote}</div>
                <div className="storageOpsActions">
                  <button className="button secondary small" type="button" onClick={() => refreshMigrationPreview()} disabled={!!rootAction}>{rootAction === "preview" ? copy.calculating : copy.refreshPreview}</button>
                  <button className="button small" type="button" onClick={() => applyMigration()} disabled={!!rootAction || !migrationPreview.apply_available}>{rootAction === "apply-migration" ? copy.applying : copy.applyMigration}</button>
                </div>
                {migrationPreview.blockers?.length ? (
                  <SummaryRow label={copy.blockers} value={migrationPreview.blockers.map((item) => item.reason || copy.reasonUnknown).join(", ")} />
                ) : null}
                {migrationResult ? (
                  <div className="storageOpsNote">
                    {copy.applyReport}: {statusLabel(migrationResult.status, language)}; {copy.executed}: {(migrationResult.executed || []).length}; {copy.sourcePreserved}: {boolLabel(migrationResult.source_preserved, language)}; {copy.cleanupPending}: {boolLabel(migrationResult.cleanup_pending, language)}
                  </div>
                ) : null}
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

            <Section title={copy.recentOperations}>
              {recent.available && recent.items?.length ? (
                <div className="storageOpsRecent">
                  {recent.items.map((item, index) => (
                    <div className="storageOpsRecentItem" key={`${item.type || "operation"}-${index}`}>
                      <strong>{item.title || item.type}</strong>
                      <span>{item.summary || item.status}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="storageOpsEmpty">{copy.noRecentOperations}</div>
              )}
            </Section>
          </>
        )}
      </div>
    </Layout>
  );
}
