"use client";

import { useEffect, useRef } from "react";
import { formatBytes } from "../lib/archiveExports";
import { useI18n } from "../lib/i18n";

export default function ArchiveExportResult({ controller, onClose }) {
  const resultRef = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const { text } = useI18n();
  const { job, status, phase, busy, transferBusy, recent, recentState, loadRecent, download, selectReady, stopWaiting, resume } = controller;
  useEffect(() => { loadRecent(); }, []);
  useEffect(() => {
    const modal = resultRef.current?.closest(".archiveExportModal");
    const previous = document.activeElement;
    modal?.querySelector("select")?.focus();
    const handleKey = (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeRef.current?.(); return; }
      if (event.key !== "Tab" || !modal) return;
      const elements = Array.from(modal.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled])'));
      const first = elements[0], last = elements[elements.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", handleKey, true);
    return () => { document.removeEventListener("keydown", handleKey, true); if (previous?.isConnected) previous.focus?.(); };
  }, []);
  return (
    <div className="archiveExportResult" data-export-phase={phase} ref={resultRef}>
      <div className={`archiveExportStatus ${phase === "error" ? "error" : ""}`} role="status" aria-live="polite">
        <span>{text(status || "Подготовьте новый клип или выберите готовый. Скачивание начнётся только по вашей кнопке.")}</span>
        <span className="archiveExportFile" title={job?.filename || ""}>
          {job?.download_ready ? `${job.filename || text("Клип")} — ${formatBytes(job.output_size_bytes)}` : job?.camera ? `${job.camera.label} — ${job.start_ts?.replace("T", " ")} — ${job.end_ts?.replace("T", " ")}` : "\u00a0"}
        </span>
      </div>
      <div className="archiveExportResultActions">
        <select className="select" aria-label={text("Сохранённые клипы")} value={recent.some((item) => item.id === job?.id) ? job.id : ""} onChange={(event) => selectReady(event.target.value)} disabled={busy || !recent.length}>
          <option value="">{recentState === "loading" ? text("Загружаем список клипов…") : recentState === "error" ? text("Список клипов недоступен") : text("Сохранённые клипы")}{recentState === "ready" ? ` (${recent.length})` : ""}</option>
          {recent.map((item) => <option key={item.id} value={item.id}>{item.camera?.label || text("Камера")} — {item.start_ts?.replace("T", " ")} — {item.download_ready ? formatBytes(item.output_size_bytes) : text("Подготовка")}</option>)}
        </select>
        {busy ? <button type="button" className="button secondary" onClick={stopWaiting}>{text("Остановить ожидание")}</button> : job && ["queued", "running"].includes(job.status) ? <button type="button" className="button primary" onClick={resume}>{text("Продолжить подготовку")}</button> : <button type="button" className="button primary" onClick={() => download()} disabled={!job?.download_ready || transferBusy}>{text("Скачать клип")}</button>}
        <button type="button" className="button secondary" onClick={() => download(true)} disabled={!job?.download_ready || transferBusy}>{text("Паспорт клипа")}</button>
      </div>
    </div>
  );
}
