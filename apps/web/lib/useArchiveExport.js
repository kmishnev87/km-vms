"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./api";
import { downloadArchiveManifest, normalizeArchiveExportError, runArchiveExportWorkflow, saveBlobDownload, startArchiveExportDownload } from "./archiveExports";

export default function useArchiveExport() {
  const [job, setJob] = useState(null);
  const [status, setStatus] = useState("");
  const [phase, setPhase] = useState("idle");
  const [busy, setBusy] = useState(false);
  const [transferBusy, setTransferBusy] = useState(false);
  const [recent, setRecent] = useState([]);
  const [recentState, setRecentState] = useState("loading");
  const activeRef = useRef(null);
  const selectionRef = useRef("");
  const jobRef = useRef(null);
  jobRef.current = job;

  useEffect(() => () => activeRef.current?.abort(), []);

  async function loadRecent() {
    try {
      const result = await apiFetch("/archive/exports?limit=200");
      setRecent((result?.items || []).filter((item) => item.download_ready || ["queued", "running"].includes(item.status)));
      setRecentState("ready");
    } catch (_) { setRecentState("error"); /* Keep any current prepared result. */ }
  }

  function stopWaiting() {
    const controller = activeRef.current;
    if (!controller) return;
    controller.abort();
    activeRef.current = null;
    setBusy(false);
    setPhase("waiting_stopped");
    setStatus("Ожидание остановлено. Подготовка на сервере может продолжаться; «Продолжить» проверит тот же клип.");
  }

  async function prepare(payload, { resumeId } = {}) {
    if (activeRef.current) return null;
    const controller = new AbortController();
    activeRef.current = controller;
    const key = JSON.stringify(payload);
    const existing = resumeId || selectionRef.current === key ? jobRef.current : null;
    selectionRef.current = resumeId ? "" : key;
    if (!existing) { jobRef.current = null; setJob(null); }
    setBusy(true);
    setPhase("preparing");
    try {
      const result = await runArchiveExportWorkflow(payload, {
        signal: controller.signal,
        exportId: resumeId || (existing && ["queued", "running", "done"].includes(existing.status) ? existing.id : undefined),
        onStatus: (message, item) => {
          if (controller.signal.aborted) return;
          setStatus(message);
          if (item?.id && item?.status) { jobRef.current = item; setJob(item); }
        },
      });
      if (controller.signal.aborted) return null;
      jobRef.current = result.job;
      setJob(result.job);
      setStatus("Клип готов. Нажмите «Скачать клип».");
      setPhase("ready");
      loadRecent();
      return result;
    } catch (error) {
      if (controller.signal.aborted || error?.name === "AbortError") return null;
      setStatus(normalizeArchiveExportError(error.message));
      setPhase("error");
      return null;
    } finally {
      if (activeRef.current === controller) { activeRef.current = null; setBusy(false); }
    }
  }

  async function download(manifest = false) {
    if (!jobRef.current?.download_ready || transferBusy) return;
    setTransferBusy(true);
    try {
      if (manifest) {
        const result = await downloadArchiveManifest(jobRef.current.id);
        saveBlobDownload(result.blob, result.filename || "km-vms-clip-passport.json");
      } else {
        await startArchiveExportDownload(jobRef.current.id);
        setPhase("downloading");
        setStatus("Загрузка передана браузеру. Ход, отмена и ошибки — в загрузках браузера. Готовый клип можно скачать повторно.");
      }
    } catch (error) {
      setPhase("error");
      setStatus(normalizeArchiveExportError(error.message));
    } finally { setTransferBusy(false); }
  }

  function selectReady(id) {
    const selected = recent.find((item) => item.id === id);
    if (!selected || busy) return;
    selectionRef.current = "";
    jobRef.current = selected;
    setJob(selected);
    setPhase(selected.download_ready ? "ready" : "waiting_stopped");
    setStatus(selected.download_ready ? "Готовый клип выбран. Повторная подготовка не требуется." : "Сохранённая подготовка выбрана. Нажмите «Продолжить подготовку», чтобы проверить или завершить тот же клип.");
  }

  return { job, status, phase, busy, transferBusy, recent, recentState, prepare, stopWaiting, loadRecent, download, selectReady,
           resume: () => jobRef.current?.id ? prepare({}, { resumeId: jobRef.current.id }) : null };
}
