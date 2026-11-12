"use client";
import ArchiveOperationHistory from "./ArchiveOperationHistory";

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

export function ArchiveOperationHistoryContent(props) {
  return <ArchiveOperationHistory {...props} />;
}
