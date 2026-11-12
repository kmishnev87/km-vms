"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiFetch, getAuthToken } from "../../lib/api";
import { useAuthSession } from "../../lib/useAuthSession";
import { useLocaleText } from "../../lib/i18n";

export default function ArchiveOperationHistory({ copy: storageCopy, language, formatDateTime, formatBytes, humanBlockerReason, onOpenIntegrity }) {
  const copy = useLocaleText("archiveHistory");
  const session = useAuthSession();
  const [filters, setFilters] = useState({ period_days: "30", operation_type: "", result_status: "", offset: 0 });
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const query = new URLSearchParams(Object.entries(filters).filter(([_key, value]) => value !== "").map(([key, value]) => [key, String(value)])).toString();

  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    setLoading(true); setFailed(false);
    apiFetch(`/storage/operations/history?${query}`, { signal: controller.signal }).then((result) => {
      if (active && getAuthToken() === session) setData(result);
    }).catch(() => { if (active && !controller.signal.aborted) setFailed(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [session, query, revision]);

  function change(key, value) { setFilters((previous) => ({ ...previous, [key]: value, offset: 0 })); }
  function reason(item) {
    if (!item.reason_code) return "";
    if (copy.reasons[item.reason_code]) return copy.reasons[item.reason_code];
    const value = humanBlockerReason(item.reason_code, language);
    return !value || /^(unknown|неизвестно|未知)$/i.test(value.trim()) ? copy.reasonFallback : value;
  }
  function stateLabel(value) { return copy.current[value] || copy.current.unconfirmed; }
  function dateOnly(day) { return new Intl.DateTimeFormat(language === "zh-CN" ? "zh-CN" : language === "en" ? "en-US" : "ru-RU", { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(`${day}T12:00:00Z`)); }
  const items = data?.items || [];
  const statusOptions = Object.entries(copy.statuses || {});
  const operationTypes = [...new Set([...(data?.operation_types || []), filters.operation_type].filter(Boolean))];

  return <div className="archiveManagementUsefulHistory archiveHistoryProduct">
    <div className="archiveHistoryFilters">
      <label><span>{copy.period}</span><select className="select" aria-label={copy.period} value={filters.period_days} onChange={(event) => change("period_days", event.target.value)}><option value="1">{copy.day}</option><option value="7">{copy.week}</option><option value="30">{copy.month}</option></select></label>
      <label><span>{copy.action}</span><select className="select" aria-label={copy.action} value={filters.operation_type} onChange={(event) => change("operation_type", event.target.value)}><option value="">{copy.all}</option>{operationTypes.map((type) => <option key={type} value={type}>{copy.operations[type] || copy.genericAction}</option>)}</select></label>
      <label><span>{copy.result}</span><select className="select" aria-label={copy.result} value={filters.result_status} onChange={(event) => change("result_status", event.target.value)}><option value="">{copy.all}</option>{statusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      <button type="button" className="button secondary small" onClick={() => setRevision((value) => value + 1)} disabled={loading}>{copy.refresh}</button>
    </div>
    <div className="archiveHistoryFeedback" role="status" aria-live="polite">{failed ? copy.loadError : loading ? copy.loading : "\u00a0"}</div>
      <section className="archiveHistorySummary" aria-label={copy.summary}>
        <h3>{copy.summary}</h3>
        <div className="archiveManagementHistorySummary">
          {[["camera_retention", copy.retention], ["auto_free_space", copy.autoFree]].map(([key, label]) => {
            const facts = data?.summary?.[key] || {};
            const states = { not_run: copy.notRun, deleted: copy.spaceFreed, no_deletion_required: copy.notRequired, not_triggered: copy.notTriggered, needs_attention: copy.needsAttention };
            return <article key={key}><span>{label}</span><strong>{data ? states[facts.state] || copy.notRun : "—"}</strong><small>{copy.deletedFiles}: {Number(facts.deleted_count || 0)} · {formatBytes(facts.bytes_freed || 0)}</small></article>;
          })}
        </div>
      </section>
      <h3 className="archiveHistoryJournalTitle">{copy.journal}{data ? ` (${data.total})` : ""}</h3>
    <div className="archiveHistoryScroll" aria-busy={loading}>
      <section className="archiveManagementHistorySection">
        <div className={`archiveManagementHistoryList ${loading || failed ? "isStale" : ""}`}>
          {!items.length && !loading && !failed ? <div className="archiveManagementHistoryEmpty">{copy.empty}</div> : null}
          {items.map((item) => <article className="archiveManagementHistoryItem archiveHistoryAction" key={item.id}>
            <header><strong>{copy.operations[item.operation_type] || copy.genericAction}</strong><time dateTime={item.finished_at}>{formatDateTime(item.finished_at, language)}</time></header>
            {item.camera_name ? <div className="archiveHistoryObject">{item.camera_name}{item.file_name ? ` — ${item.file_name}` : ""}</div> : null}
            <dl className="archiveHistoryResult"><div><dt>{copy.result}</dt><dd>{copy.statuses[item.status] || copy.genericAction}</dd></div><div><dt>{copy.now}</dt><dd className={item.current_state === "attention" ? "needsAttention" : ""}>{stateLabel(item.current_state)}</dd></div></dl>
            {reason(item) ? <p>{reason(item)}</p> : null}
            {item.completed_count || item.failed_count || item.bytes_freed ? <p className="archiveHistoryCounts">{copy.completed}: {item.completed_count} · {copy.failed}: {item.failed_count}{item.bytes_freed ? ` · ${copy.freed}: ${formatBytes(item.bytes_freed)}` : ""}</p> : null}
            {item.detail_count ? <details className="archiveHistoryDetails"><summary>{copy.details} ({item.detail_count})</summary>
              {(item.details || []).map((child) => <div key={child.id}><strong>{child.camera_name || copy.camera}{child.file_name ? ` — ${child.file_name}` : ""}</strong><span>{copy.statuses[child.status] || copy.genericAction} · {copy.now}: {stateLabel(child.current_state)}</span>{reason(child) ? <p>{reason(child)}</p> : null}</div>)}
              {item.detail_count > (item.details || []).length ? <p>{copy.moreDetails}</p> : null}
            </details> : null}
            <div className="archiveHistoryActions">
              {item.can_open_integrity && onOpenIntegrity ? <button type="button" className="button secondary small" onClick={onOpenIntegrity} disabled={loading}>{copy.openCheck}</button> : null}
              {item.can_open_cameras ? <Link className="button secondary small" href="/cameras">{copy.openCameras}</Link> : null}
            </div>
          </article>)}
        </div>
      </section>
      {data?.daily_items?.length ? <details className="archiveHistoryDaily"><summary>{copy.daily}</summary>{data.daily_items.map((item) => <div key={`${item.day}-${item.source}`}><span>{dateOnly(item.day)}</span><span>{item.source === "camera_retention" ? copy.retention : copy.autoFree}</span><span>{copy.deletedFiles}: {item.deleted_count} · {formatBytes(item.bytes_freed)}</span></div>)}</details> : null}
    </div>
    <div className="archiveHistoryPagination"><button className="button secondary small" type="button" disabled={loading || !filters.offset} onClick={() => setFilters((previous) => ({ ...previous, offset: Math.max(0, previous.offset - 20) }))}>{copy.previous}</button><span>{data?.total ? `${data.offset + 1}–${data.offset + items.length} / ${data.total}` : "0"}</span><button className="button secondary small" type="button" disabled={loading || !data?.has_more} onClick={() => setFilters((previous) => ({ ...previous, offset: previous.offset + 20 }))}>{copy.next}</button></div>
  </div>;
}
