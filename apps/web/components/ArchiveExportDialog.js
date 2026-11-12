"use client";

import { useEffect, useRef, useState } from "react";
import { useLocaleText } from "../lib/i18n";
import { formatBytes } from "../lib/archiveExports";
import ArchiveExportResult from "./ArchiveExportResult";

// Entry points own separate drafts. Presentation is shared, never their context.
export default function ArchiveExportDialog({ draft, onDraftChange, onClose, onCreate, cameras, controller, formatInput, source }) {
  const copy = useLocaleText("archiveClips");
  const [mode, setMode] = useState("new");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const modalRef = useRef(null);
  const closeRef = useRef(onClose);
  const wasBusy = useRef(false);
  closeRef.current = onClose;
  const { busy, deleteBusy, job, phase, recent, recentState, recentTotal, loadRecent, selectReady, remove } = controller;

  useEffect(() => {
    loadRecent();
    if (!busy) selectReady("");
    const previous = document.activeElement;
    modalRef.current?.querySelector("select")?.focus({ preventScroll: true });
    const handleKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeRef.current?.(); }
      if (event.key !== "Tab") return;
      const elements = Array.from(modalRef.current?.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), summary') || []).filter((element) => element.getClientRects().length);
      const first = elements[0], last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", handleKey, true);
    return () => { document.removeEventListener("keydown", handleKey, true); if (previous?.isConnected) previous.focus?.({ preventScroll: true }); };
  }, []);

  useEffect(() => {
    if (wasBusy.current && !busy && phase === "ready" && job?.download_ready) setMode("saved");
    wasBusy.current = busy;
  }, [busy, phase, job?.download_ready]);

  function switchMode(next) {
    if (busy || deleteBusy) return;
    setConfirmDelete(false);
    if (next === "new") selectReady("");
    setMode(next);
  }
  function change(field, value) { onDraftChange((previous) => ({ ...previous, [field]: value })); }

  return <div className="modalBackdrop">
    <div className="modal archiveExportModal archiveClipDialog" role="dialog" aria-modal="true" aria-labelledby="archive-clip-title" data-source={source} ref={modalRef}>
      <header className="modalHeader">
        <div><h2 className="archiveExportTitle" id="archive-clip-title">{copy.title}</h2><span className="archiveClipSource">{source === "chronology" ? copy.fromChronology : copy.fromRecordings}</span></div>
        <details className="archiveClipHelp"><summary aria-label={copy.help}>?</summary><div><strong>{copy.help}</strong><p>{copy.helpBody}</p><p>{copy.retention}</p></div></details>
        <button className="iconCloseButton" type="button" onClick={onClose} disabled={deleteBusy} aria-label={copy.close}>×</button>
      </header>
      <div className="archiveClipTabs" role="tablist" aria-label={copy.title}>
        <button type="button" role="tab" aria-selected={mode === "new"} aria-controls="archive-clip-panel" className={mode === "new" ? "isActive" : ""} onClick={() => switchMode("new")} disabled={busy || deleteBusy}>{copy.newClip}</button>
        <button type="button" role="tab" aria-selected={mode === "saved"} aria-controls="archive-clip-panel" className={mode === "saved" ? "isActive" : ""} onClick={() => switchMode("saved")} disabled={busy || deleteBusy}>{copy.saved} ({recentTotal})</button>
      </div>
      <div className="archiveClipPanel" id="archive-clip-panel" role="tabpanel">
        {mode === "new" ? <>
          <div className="archiveExportFields">
            <label className="archiveExportField"><span>{copy.camera}</span><select className="select" value={draft.cameraId} onChange={(event) => change("cameraId", event.target.value)} disabled={busy}>
              <option value="">{copy.pickCamera}</option>{cameras.map((camera) => <option key={camera.id} value={camera.id}>{camera.name}{camera.deleted ? ` (${copy.cameraDeletedMark})` : ""}</option>)}
            </select></label>
            <label className="archiveExportField"><span>{copy.reason}</span><input className="input" value={draft.reason} onChange={(event) => change("reason", event.target.value)} disabled={busy} maxLength={500} /></label>
            <label className="archiveExportField"><span>{copy.start}</span><input className="input" type="datetime-local" step="1" value={formatInput(draft.startTs)} onChange={(event) => change("startTs", event.target.value)} disabled={busy} /></label>
            <label className="archiveExportField"><span>{copy.end}</span><input className="input" type="datetime-local" step="1" value={formatInput(draft.endTs)} onChange={(event) => change("endTs", event.target.value)} disabled={busy} /></label>
          </div>
          <p className="archiveClipLimit">{copy.range}</p>
        </> : <>
          <label className="archiveExportField"><span>{copy.saved}</span><select className="select" aria-label={copy.saved} value={recent.some((item) => item.id === job?.id) ? job.id : ""} onChange={(event) => { setConfirmDelete(false); selectReady(event.target.value); }} disabled={deleteBusy}>
            <option value="">{copy.none}</option>{recent.map((item) => <option key={item.id} value={item.id}>{item.camera?.label || copy.camera} — {item.start_ts?.replace("T", " ")} — {item.end_ts?.replace("T", " ")}</option>)}
          </select></label>
          <div className="archiveClipSavedFacts">
            <dl><div><dt>{copy.camera}</dt><dd>{job?.camera?.label || "—"}</dd></div><div><dt>{copy.size}</dt><dd>{job?.output_size_bytes != null ? formatBytes(job.output_size_bytes) : "—"}</dd></div><div><dt>{copy.period}</dt><dd>{job ? `${job.start_ts?.replace("T", " ")} — ${job.end_ts?.replace("T", " ")}` : "—"}</dd></div></dl>
            <span className="archiveExportFile" title={job?.filename || ""}>{job?.filename || "\u00a0"}</span>
          </div>
          <div className="archiveClipListState">
            {recentState === "loading" ? copy.loading : recentState === "error" ? <button type="button" className="button secondary" onClick={() => loadRecent()}>{copy.retry}</button> : recent.length < recentTotal ? <button type="button" className="button secondary" onClick={() => loadRecent({ append: true })}>{copy.more} ({recent.length}/{recentTotal})</button> : !recent.length ? copy.empty : copy.retention}
          </div>
        </>}
      </div>
      <ArchiveExportResult controller={controller} mode={mode} onCreate={onCreate} confirmDelete={confirmDelete}
        onRequestDelete={() => setConfirmDelete(true)} onCancelDelete={() => setConfirmDelete(false)}
        onDelete={async () => { if (await remove(job.id)) setConfirmDelete(false); }} />
    </div>
  </div>;
}
