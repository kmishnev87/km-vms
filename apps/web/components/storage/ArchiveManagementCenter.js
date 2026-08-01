"use client";

export function ArchivePolicySwitch({ checked, busy = false, disabled = false, label, onChange, title = "" }) {
  return (
    <button
      className={`archiveManagementSwitch ${checked ? "isChecked" : ""}`}
      type="button"
      role="switch"
      aria-checked={checked ? "true" : "false"}
      aria-label={label}
      title={title || label}
      disabled={disabled || busy}
      onClick={() => onChange?.(!checked)}
    >
      <span aria-hidden="true" />
    </button>
  );
}

function ArchiveManagementRow({ row }) {
  return (
    <article className={`archiveManagementRow archiveManagementRow-${row.tone || "neutral"}`}>
      <div className="archiveManagementRowBody">
        <div className="archiveManagementRowTitle">
          <h4>{row.title}</h4>
          <span className={`storageOpsStatusPill storageOpsStatusPill-${row.tone || "neutral"}`}>{row.status}</span>
        </div>
        <p>{row.description}</p>
        {row.facts?.length ? (
          <dl className="archiveManagementFacts">
            {row.facts.map((fact) => (
              <div key={`${row.id}-${fact.label}`}>
                <dt>{fact.label}</dt>
                <dd className={fact.tone ? `archiveManagementFact-${fact.tone}` : undefined}>{fact.value}</dd>
              </div>
            ))}
          </dl>
        ) : null}
      </div>
      <div className="archiveManagementRowAction">{row.action}</div>
    </article>
  );
}

export function ArchiveManagementCenter({ title, subtitle, historyLabel, onOpenHistory, groups }) {
  return (
    <section className="storageOpsSection storageOpsSection-archiveManagement">
      <header className="archiveManagementHeader">
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <button
          className="button secondary small appIllustratedAction archiveManagementHistoryButton"
          type="button"
          onClick={onOpenHistory}
          title={historyLabel}
          aria-label={historyLabel}
        >
          <img src="/assets/icons/ui/operation-history.png" alt="" aria-hidden="true" />
        </button>
      </header>
      <div className="archiveManagementGroups">
        {groups.map((group) => (
          <section className="archiveManagementGroup" key={group.id} aria-labelledby={`archive-management-group-${group.id}`}>
            <h3 id={`archive-management-group-${group.id}`}>{group.title}</h3>
            <div className="archiveManagementRows">
              {group.rows.map((row) => <ArchiveManagementRow key={row.id} row={row} />)}
            </div>
          </section>
        ))}
      </div>
    </section>
  );
}

export function ArchiveOperationHistoryContent({ history, loading, error, copy, language, formatDateTime, formatBytes, humanBlockerReason }) {
  if (loading) {
    return <div className="archiveManagementHistoryEmpty">{copy.loading}</div>;
  }
  if (error) {
    return <div className="archiveManagementHistoryEmpty" role="alert">{error}</div>;
  }
  if (!history?.available) {
    return <div className="archiveManagementHistoryEmpty">{copy.operationHistoryUnavailable}</div>;
  }
  const cameraRetention = history.summary?.camera_retention || {};
  const autoFree = history.summary?.auto_free_space || {};
  const dailyItems = Array.isArray(history.daily_items) ? history.daily_items : [];
  const attentionItems = Array.isArray(history.attention_items) ? history.attention_items : [];
  return (
    <div className="archiveManagementUsefulHistory">
      <section className="archiveManagementHistorySummary" aria-label={copy.operationHistory24Hours}>
        {[
          ["camera", copy.operationHistoryCameraRetention, cameraRetention],
          ["auto", copy.operationHistoryAutoFree, autoFree],
        ].map(([key, label, facts]) => (
          <article key={key}>
            <span>{label}</span>
            <strong>{facts.state === "deleted" ? copy.operationHistorySpaceFreed : facts.state === "not_triggered" ? copy.operationHistoryNotTriggered : copy.operationHistoryNoDeletionRequired}</strong>
            <small>{copy.operationHistoryDeletedFiles}: {Number(facts.deleted_count || 0)} · {formatBytes(facts.bytes_freed || 0)}</small>
          </article>
        ))}
      </section>
      {dailyItems.length ? (
        <section className="archiveManagementHistorySection">
          <h3>{copy.operationHistoryFreedByDay}</h3>
          <div className="archiveManagementHistoryList">
            {dailyItems.map((item) => (
              <article className="archiveManagementHistoryItem" key={`${item.day}-${item.source}`}>
                <div className="archiveManagementHistoryPrimary">
                  <strong>{item.source === "camera_retention" ? copy.operationHistoryCameraRetention : copy.operationHistoryAutoFree}</strong>
                  <time dateTime={item.day}>{formatDateTime(`${item.day}T00:00:00Z`, language)}</time>
                </div>
                <p>{copy.operationHistoryDeletedFiles}: <strong>{Number(item.deleted_count || 0)}</strong> · {formatBytes(item.bytes_freed || 0)}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {attentionItems.length ? (
        <section className="archiveManagementHistorySection">
          <h3>{copy.operationHistoryNeedsAttention}</h3>
          <div className="archiveManagementHistoryList">
            {attentionItems.map((item) => (
              <article className="archiveManagementHistoryItem" key={item.id}>
                <div className="archiveManagementHistoryPrimary">
                  <strong>{copy.operationHistoryOperationNeedsAttention}</strong>
                  <span className="storageOpsStatusPill storageOpsStatusPill-warning">{copy[`recentOperationStatus${String(item.status || "unknown").replace(/(^|_)([a-z])/g, (_match, _separator, letter) => letter.toUpperCase())}`] || copy.recentOperationStatusUnknown}</span>
                  {item.finished_at ? <time dateTime={item.finished_at}>{formatDateTime(item.finished_at, language)}</time> : null}
                </div>
                <p>{humanBlockerReason(item.reason_code, language)}</p>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {!dailyItems.length && !attentionItems.length ? <div className="archiveManagementHistoryEmpty">{copy.operationHistoryUsefulEmpty}</div> : null}
    </div>
  );
}
