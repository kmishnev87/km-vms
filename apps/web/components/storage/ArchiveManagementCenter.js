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

export function ArchiveManagementCenter({ title, subtitle, historyLabel, historyCount = 0, onOpenHistory, groups }) {
  return (
    <section className="storageOpsSection storageOpsSection-archiveManagement">
      <header className="archiveManagementHeader">
        <div>
          <h2>{title}</h2>
          <p>{subtitle}</p>
        </div>
        <button className="button secondary small archiveManagementHistoryButton" type="button" onClick={onOpenHistory}>
          <span>{historyLabel}</span>
          {historyCount > 0 ? <strong aria-label={String(historyCount)}>{historyCount}</strong> : null}
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

export function ArchiveOperationHistoryContent({ available, items, copy, language, formatDateTime, formatBytes, humanBlockerReason, onOpenItem }) {
  if (!available) {
    return <div className="archiveManagementHistoryEmpty">{copy.operationHistoryUnavailable}</div>;
  }
  if (!items.length) {
    return <div className="archiveManagementHistoryEmpty">{copy.operationHistoryEmpty}</div>;
  }
  return (
    <div className="archiveManagementHistoryList">
      {items.map((item) => (
        <article className="archiveManagementHistoryItem" key={item.key}>
          <div className="archiveManagementHistoryPrimary">
            <strong>{copy[item.typeKey] || copy.recentOperationGeneric}</strong>
            <span className={`storageOpsStatusPill storageOpsStatusPill-${item.tone}`}>
              {copy[item.statusKey] || copy.recentOperationStatusUnknown}
            </span>
            {item.timestamp ? <time dateTime={item.timestamp}>{formatDateTime(item.timestamp, language)}</time> : null}
          </div>
          {item.facts.length ? (
            <dl className="archiveManagementHistoryFacts">
              {item.facts.map((fact) => (
                <div key={fact.labelKey}>
                  <dt>{copy[fact.labelKey]}</dt>
                  <dd>{fact.format === "bytes" ? formatBytes(fact.value) : fact.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}
          {item.reasonCode ? (
            <p><strong>{copy.recentOperationReason}:</strong> {humanBlockerReason(item.reasonCode, language)}</p>
          ) : null}
          {item.nextActionKey ? (
            <p><strong>{copy.recentOperationNextAction}:</strong> {copy[item.nextActionKey]}</p>
          ) : null}
          {item.action ? (
            <div className="archiveManagementHistoryAction">
              <button
                className="button secondary small"
                type="button"
                onClick={() => onOpenItem?.(item)}
                disabled={item.action.disabled}
                title={item.action.title || ""}
              >
                {copy[item.action.labelKey]}
              </button>
            </div>
          ) : null}
        </article>
      ))}
    </div>
  );
}
