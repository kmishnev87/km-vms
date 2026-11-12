"use client";

import { useI18n, useLocaleText } from "../lib/i18n";

export default function ArchiveExportResult({ controller, mode, onCreate, confirmDelete, onRequestDelete, onCancelDelete, onDelete }) {
  const copy = useLocaleText("archiveClips");
  const { text } = useI18n();
  const { job, status, phase, busy, transferBusy, deleteBusy, download, stopWaiting, resume } = controller;
  return <div className="archiveExportResult" data-export-phase={phase}>
    <div className={`archiveClipNotice ${phase === "error" ? "isError" : ""}`} role="status" aria-live="polite">
      {confirmDelete ? copy.deleteConfirm : status ? text(status) : "\u00a0"}
    </div>
    <div className="archiveClipActions">
      {confirmDelete ? <>
        <button type="button" className="button secondary" onClick={onCancelDelete} disabled={deleteBusy}>{copy.cancel}</button><span />
        <button type="button" className="button secondary archiveClipDelete" onClick={onDelete} disabled={deleteBusy}>{deleteBusy ? copy.deleting : copy.delete}</button>
      </> : mode === "new" ? <>
        <span /><span />{busy ? <button type="button" className="button secondary" onClick={stopWaiting}>{copy.stopWaiting}</button> : <button type="button" className="button secondary archiveClipMainAction" onClick={onCreate}>{copy.create}</button>}
      </> : <>
        {job && ["queued", "running"].includes(job.status) ? <button type="button" className="button secondary archiveClipMainAction" onClick={resume} disabled={busy || deleteBusy}>{copy.resume}</button> : <button type="button" className="button secondary archiveClipMainAction" onClick={() => download()} disabled={!job?.download_ready || transferBusy || deleteBusy}>{copy.download}</button>}
        <button type="button" className="button secondary" onClick={() => download(true)} disabled={!job?.download_ready || transferBusy || deleteBusy}>{copy.passport}</button>
        <button type="button" className="button secondary archiveClipDelete" onClick={onRequestDelete} disabled={!job || busy || transferBusy || deleteBusy}>{copy.delete}</button>
      </>}
    </div>
  </div>;
}
