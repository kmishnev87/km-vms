"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import Layout from "../../components/Layout";
import { apiFetch, canAccessPath, forbiddenMessage } from "../../lib/api";
import { useCurrentUser } from "../../lib/currentUser";
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
  return message.includes("401") || message.includes("403") || message.includes("permission") || message.includes("доступ");
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

function reasonText(summary) {
  const entries = topReasonEntries(summary);
  if (!entries.length) return "Нет активных причин или блокеров";
  return entries.map(([key, value]) => `${key}: ${value}`).join(", ");
}

export default function StorageOperationsPage() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [accessDenied, setAccessDenied] = useState(false);
  const [language, setLanguage] = useState("ru");
  const { currentUser, status: currentUserStatus } = useCurrentUser();

  useEffect(() => {
    if (typeof window !== "undefined") {
      setLanguage(localStorage.getItem("km_vms_language") === "en" ? "en" : "ru");
    }
  }, []);

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
      setError(err?.message || "Не удалось получить состояние хранилища");
      return false;
    } finally {
      setLoading(false);
      if (!silent) setRefreshing(false);
    }
  }, [language]);

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
  const capacity = operations.capacity || {};
  const pathHealth = operations.path_health || {};
  const namespace = operations.namespace_health || {};
  const owned = operations.owned_archive || {};
  const policy = operations.low_disk_policy || {};
  const autoCleanup = operations.auto_free_space_cleanup || {};
  const retention = operations.retention || {};
  const reconciliation = operations.reconciliation || {};
  const recent = operations.recent_operations || {};
  const cameraRows = useMemo(() => cameraStorageRows(operations.per_camera_usage), [operations.per_camera_usage]);
  const usagePercent = Number(capacity.usage_percent || 0);

  return (
    <Layout>
      <div className="storageOpsPage">
        <header className="pageHeader storageOpsHeader">
          <div>
            <h1 className="pageTitle">Хранилище</h1>
            <div className="pageSubtitle">Состояние архива, свободное место, хранение записей, проверка целостности и политика нехватки места.</div>
          </div>
          <button className="button secondary small" type="button" onClick={() => loadStatus()} disabled={refreshing || loading || accessDenied}>
            {refreshing ? "Обновление..." : "Обновить"}
          </button>
        </header>

        {loading ? (
          <div className="storageOpsState">Загрузка состояния хранилища...</div>
        ) : accessDenied ? (
          <div className="storageOpsState storageOpsState-error">{error || forbiddenMessage(language)}</div>
        ) : error ? (
          <div className="storageOpsState storageOpsState-error">{error}</div>
        ) : (
          <>
            <section className="storageOpsOverview">
              <div>
                <span className="storageOpsEyebrow">Последняя проверка: {formatDateTime(operations.checked_at, language)}</span>
                <strong>{statusLabel(operations.status, language)}</strong>
                <p>{lowDiskPolicyText(policy, language)}</p>
              </div>
              <div className="storageOpsBadges">
                <Badge label={`Чтение: ${boolLabel(pathHealth.readable, language)}`} tone={pathHealth.readable ? "ok" : "error"} />
                <Badge label={`Запись: ${boolLabel(pathHealth.writable, language)}`} tone={pathHealth.writable ? "ok" : "error"} />
                <Badge label={`Доступность: ${boolLabel(pathHealth.available, language)}`} tone={pathHealth.available ? "ok" : "error"} />
              </div>
            </section>

            <div className="storageOpsGrid">
              <Section title="Ёмкость">
                <div className="storageOpsCapacityBar" aria-label="Использование хранилища">
                  <span style={{ width: `${Math.max(0, Math.min(100, usagePercent))}%` }} />
                </div>
                <div className="storageOpsStats">
                  <Stat label="Всего" value={formatBytes(capacity.total_bytes)} />
                  <Stat label="Использовано" value={`${formatBytes(capacity.used_bytes)} / ${formatPercent(capacity.usage_percent)}`} />
                  <Stat label="Свободно" value={`${formatBytes(capacity.free_bytes)} / ${formatPercent(capacity.free_percent)}`} tone={policy.state === "critical" ? "error" : policy.state === "warning" || policy.state === "cleanup_threshold" ? "warning" : "neutral"} />
                </div>
              </Section>

              <Section title="Политика нехватки места">
                <div className="storageOpsStats">
                  <Stat label="Политика" value={policyStateLabel(policy, language)} tone={policy.auto_free_space_cleanup_enabled ? "ok" : "neutral"} />
                  <Stat label="Предупреждение" value={`<${policy.warning_threshold_percent ?? 10}% свободно`} />
                  <Stat label="Автоосвобождение" value={policy.auto_free_space_cleanup_enabled ? `<${policy.cleanup_threshold_percent ?? 5}% свободно` : "Выключено"} />
                  <Stat label="Критично" value={`<${policy.critical_threshold_percent ?? 1}% свободно`} tone={policy.recording_suspended_by_low_disk ? "error" : "neutral"} />
                </div>
                <div className="storageOpsNote">
                  Ниже 10% система предупреждает. Ниже 5% она может удалять старые owned-записи только если автоосвобождение включено. Ниже 1% запись может быть приостановлена для защиты диска; критический режим не разрешает удаление без opt-in.
                </div>
                <SummaryRow label="Запись приостановлена из-за критически малого места" value={boolLabel(policy.recording_suspended_by_low_disk, language)} />
              </Section>

              <Section title="Автоосвобождение места">
                <div className="storageOpsStats">
                  <Stat label="Состояние" value={autoCleanup.enabled ? "Включено" : "Выключено"} />
                  <Stat label="Последний запуск" value={formatDateTime(autoCleanup.last_finished_at || autoCleanup.last_started_at, language)} />
                  <Stat label="Удалено" value={String(autoCleanup.last_summary?.deleted_count || 0)} />
                  <Stat label="Освобождено" value={formatBytes(autoCleanup.last_summary?.bytes_freed)} />
                </div>
                <div className="storageOpsNote">Автоосвобождение удаляет только owned metadata-safe записи и только когда opt-in включён.</div>
                <SummaryRow label="Блокеры / причины" value={reasonText(autoCleanup.last_summary)} />
                {autoCleanup.last_error ? <SummaryRow label="Последняя ошибка" value={autoCleanup.last_error} /> : null}
              </Section>

              <Section title="Архив KM VMS">
                <div className="storageOpsStats">
                  <Stat label="Размер архива KM VMS" value={formatBytes(owned.size_bytes)} />
                  <Stat label="Сегменты" value={String(owned.segments_count || 0)} />
                  <Stat label="Файлы на месте" value={String(owned.existing_file_count || 0)} />
                  <Stat label="Проблемы" value={String(owned.problem_file_count || 0)} tone={owned.problem_file_count ? "warning" : "neutral"} />
                </div>
                <SummaryRow label="Отсутствующие файлы" value={String(owned.missing_file_count || 0)} />
                <SummaryRow label="Чужие metadata-строки пропущены" value={String(owned.skipped_foreign_metadata_rows || 0)} />
                <SummaryRow label="Удалённые metadata-строки исключены" value={String(owned.deleted_metadata_rows_excluded || 0)} />
              </Section>

              <Section title="Хранение записей" action={<Link className="button secondary small" href="/settings">Открыть workflow</Link>}>
                <div className="storageOpsStats">
                  <Stat label="Состояние" value={statusLabel(retention.last_status, language)} />
                  <Stat label="Последний запуск" value={formatDateTime(retention.last_finished_at || retention.last_started_at, language)} />
                  <Stat label="Удалено" value={String(retention.last_summary?.deleted_count || 0)} />
                  <Stat label="Пропущено / ошибки" value={`${retention.last_summary?.skipped_count || 0} / ${retention.last_summary?.failed_count || 0}`} />
                </div>
                <SummaryRow label="Причины / блокеры" value={reasonText(retention.last_summary)} />
                <SummaryRow label="Освобождено" value={formatBytes(retention.last_summary?.bytes_freed)} />
              </Section>

              <Section title="Проверка архива и целостности" action={<Link className="button secondary small" href="/settings">Открыть проверку</Link>}>
                <div className="storageOpsStats">
                  <Stat label="Состояние" value={statusLabel(reconciliation.status, language)} tone={reconciliation.problem_file_count ? "warning" : "neutral"} />
                  <Stat label="Проблемы" value={String(reconciliation.problem_file_count || 0)} />
                  <Stat label="Кандидаты на разбор" value={String(reconciliation.cleanup_candidate_count || 0)} />
                  <Stat label="Последняя проверка" value={formatDateTime(reconciliation.last_checked_at, language)} />
                </div>
                <SummaryRow label="Отсутствуют / без metadata" value={`${reconciliation.missing_file_count || 0} / ${reconciliation.orphan_file_count || 0}`} />
                <SummaryRow label="Некорректный путь / вне хранилища" value={`${reconciliation.invalid_path_count || 0} / ${reconciliation.path_outside_storage_count || 0}`} />
                <div className="storageOpsNote">Кандидаты показаны без удаления, только для разбора; этот экран не удаляет и не импортирует файлы.</div>
              </Section>

              <Section title="Состояние пространства архива">
                <div className="storageOpsStats">
                  <Stat label="Namespace" value={namespace.storage_namespace || "-"} />
                  <Stat label="Namespace существует" value={boolLabel(namespace.namespace_exists, language)} />
                  <Stat label="Режим сканирования" value={namespace.scan_mode || "-"} />
                  <Stat label="Частично / ограничено" value={`${boolLabel(namespace.partial, language)} / ${boolLabel(namespace.scan_limited, language)}`} />
                </div>
                {namespace.partial_reason ? <SummaryRow label="Причина частичного сканирования" value={namespace.partial_reason} /> : null}
              </Section>
            </div>

            <Section title="По камерам">
              {cameraRows.length ? (
                <div className="storageOpsTableWrap">
                  <table className="storageOpsTable">
                    <thead>
                      <tr>
                        <th>Камера</th>
                        <th>Размер</th>
                        <th>Сегменты</th>
                        <th>Отсутствуют / проблемы</th>
                        <th>Диапазон</th>
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
                <div className="storageOpsEmpty">Нет owned-записей по камерам.</div>
              )}
            </Section>

            <Section title="Последние операции">
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
                <div className="storageOpsEmpty">Нет безопасного bounded-источника истории операций; используйте последние summary хранения записей и проверки целостности.</div>
              )}
            </Section>
          </>
        )}
      </div>
    </Layout>
  );
}
