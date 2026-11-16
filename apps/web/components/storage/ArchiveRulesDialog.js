"use client";

import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useI18n } from "../../lib/i18n";
import { ARCHIVE_RULE_COPY, ARCHIVE_RULE_FIELDS, ARCHIVE_RULE_LIMITS, archiveRuleChanges, archiveRuleDraft, archiveRuleProblems, reducedArchiveRules } from "../../lib/archiveRules";
import { OperationDialog } from "../OperationFeedback";

export default function ArchiveRulesDialog({ onClose, onSaved }) {
  const { locale } = useI18n();
  const copy = ARCHIVE_RULE_COPY[locale] || ARCHIVE_RULE_COPY.en;
  const [rows, setRows] = useState([]);
  const [draft, setDraft] = useState({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [generation, setGeneration] = useState(0);
  const [selected, setSelected] = useState([]);
  const [bulk, setBulk] = useState({});
  const [showProblems, setShowProblems] = useState(false);
  const [discard, setDiscard] = useState(false);
  const changes = useMemo(() => archiveRuleChanges(rows, draft), [rows, draft]);
  const problems = useMemo(() => archiveRuleProblems(rows, draft), [rows, draft]);
  const reduced = useMemo(() => reducedArchiveRules(rows, draft), [rows, draft]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    apiFetch("/cameras/archive-rules", { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setRows(result.cameras || []); setDraft(archiveRuleDraft(result.cameras || []));
      setSelected([]); setBulk({}); setShowProblems(false);
    }).catch(() => { if (!controller.signal.aborted) setError(copy.failed); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [generation]);

  function close() {
    if (busy) return;
    if (changes.length) setDiscard(true);
    else onClose();
  }
  function update(cameraId, key, value) {
    setDraft(previous => ({ ...previous, [cameraId]: { ...previous[cameraId], [key]: value } }));
    setError("");
  }
  function applyBulk() {
    const patch = Object.fromEntries(ARCHIVE_RULE_FIELDS.filter(key => String(bulk[key] || "") !== "").map(key => [key, bulk[key]]));
    setDraft(previous => Object.fromEntries(Object.entries(previous).map(([id, values]) => [id, selected.includes(Number(id)) ? { ...values, ...patch } : values])));
    setError("");
  }
  async function save() {
    setShowProblems(true); setError("");
    if (Object.keys(problems).length || !changes.length) return;
    setBusy(true);
    try {
      await apiFetch("/cameras/archive-rules", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ changes }) });
      onSaved?.(); onClose();
    } catch (failure) {
      setError(failure?.status === 409 ? copy.conflict : copy.saveFailed);
    } finally { setBusy(false); }
  }
  const content = <div className="archiveRulesEditor">
    <p className="archiveRulesHelp">{copy.help}</p>
    {loading ? <p role="status">{copy.loading}</p> : error && !rows.length ? <div role="alert"><p>{error}</p><button type="button" className="button secondary small" onClick={() => setGeneration(value => value + 1)}>{copy.retry}</button></div> : !rows.length ? <p>{copy.empty}</p> : <>
      <div className="archiveRulesGrid archiveRulesBulk">
        <span />
        <span>{copy.bulk}<small>{copy.selected}: {selected.length}</small></span>
        {ARCHIVE_RULE_FIELDS.map(key => <input key={key} className="input" type="number" inputMode="numeric" min="1" max={ARCHIVE_RULE_LIMITS[key]} value={bulk[key] || ""} placeholder={copy.keep} disabled={busy} aria-label={`${copy.bulk}: ${copy[key]}`} onChange={event => setBulk(previous => ({ ...previous, [key]: event.target.value }))} />)}
      </div>
      <div className="archiveRulesBulkAction"><button className="button secondary small" type="button" disabled={busy || !selected.length || !ARCHIVE_RULE_FIELDS.some(key => bulk[key])} onClick={applyBulk}>{copy.apply}</button></div>
      <div className="archiveRulesGrid archiveRulesLabels">
        <input type="checkbox" checked={rows.length > 0 && selected.length === rows.length} disabled={busy} aria-label={copy.selectAll} onChange={event => setSelected(event.target.checked ? rows.map(row => row.camera_id) : [])} />
        <strong>{copy.camera}</strong>{ARCHIVE_RULE_FIELDS.map(key => <span key={key}>{copy[key]}</span>)}
      </div>
      <div className="archiveRulesRows">
        {rows.map(row => <div className="archiveRulesGrid archiveRulesRow" key={row.camera_id}>
          <input type="checkbox" checked={selected.includes(row.camera_id)} disabled={busy} aria-label={`${copy.select}: ${row.name}`} onChange={event => setSelected(previous => event.target.checked ? [...previous, row.camera_id] : previous.filter(id => id !== row.camera_id))} />
          <span className="archiveRulesCameraName" title={row.name}>{!row.enabled ? <span className="archiveRulesDisabled" title={copy.disabled} aria-label={copy.disabled} /> : null}{row.name}</span>
          {ARCHIVE_RULE_FIELDS.map(key => <input key={key} type="number" className="input" inputMode="numeric" min="1" max={ARCHIVE_RULE_LIMITS[key]} value={draft[row.camera_id]?.[key] ?? ""} disabled={busy} aria-label={`${row.name}: ${copy[key]}`} aria-invalid={showProblems && problems[`${row.camera_id}:${key}`] || undefined} onChange={event => update(row.camera_id, key, event.target.value)} />)}
        </div>)}
      </div>
      <div className="archiveRulesFeedback" aria-live="polite">
        {showProblems && Object.keys(problems).length ? <p className="archiveRulesError">{copy.invalid}</p> : error ? <p className="archiveRulesError">{error} <button type="button" className="button secondary small" disabled={busy} onClick={() => setGeneration(value => value + 1)}>{copy.retry}</button></p> : reduced ? <p className="archiveRulesWarning">{copy.reduced}</p> : null}
      </div>
    </>}
  </div>;

  return <OperationDialog dialog={discard ? {
    id: "archive-rules-discard", title: copy.discard, closeLabel: copy.close, cancelLabel: copy.continue,
    confirmLabel: copy.discardYes, tone: "neutral", presentation: "compact-confirmation", onConfirm: onClose,
  } : {
    id: "archive-rules", title: copy.title, tone: "neutral", className: "archiveRulesDialog", content,
    closeLabel: copy.close, cancelLabel: copy.cancel, confirmLabel: busy ? copy.saving : copy.save,
    busy, confirmDisabled: loading || !changes.length, onConfirm: save,
  }} onClose={discard ? () => setDiscard(false) : close} />;
}
