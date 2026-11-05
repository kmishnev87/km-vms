"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch, getAuthToken } from "./api";
import { useAuthSession } from "./useAuthSession";
import { downloadArchiveManifest, normalizeArchiveExportError, runArchiveExportWorkflow, saveBlobDownload, startArchiveExportDownload } from "./archiveExports";

export default function useArchiveExport() {
  const session = useAuthSession();
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
  const ownerRef = useRef({ session, generation: 0, mounted: true });
  const operationsRef = useRef(new Set());
  jobRef.current = job;

  useEffect(() => {
    const owner = ownerRef.current;
    owner.session = session; owner.generation += 1; owner.mounted = true;
    for (const controller of operationsRef.current) controller.abort();
    operationsRef.current.clear(); activeRef.current = null;
    selectionRef.current = ""; jobRef.current = null;
    setJob(null); setStatus(""); setPhase("idle"); setBusy(false); setTransferBusy(false);
    setRecent([]); setRecentState("loading");
    return () => {
      owner.mounted = false;
      for (const controller of operationsRef.current) controller.abort();
      operationsRef.current.clear(); activeRef.current = null;
    };
  }, [session]);

  function ownsSession() {
    return Boolean(session && ownerRef.current.mounted && ownerRef.current.session === session && getAuthToken() === session);
  }

  function beginOperation() {
    if (!ownsSession()) return null;
    const generation = ownerRef.current.generation;
    const controller = new AbortController();
    operationsRef.current.add(controller);
    return { controller, signal: controller.signal,
      isCurrent: () => !controller.signal.aborted && ownsSession() && ownerRef.current.generation === generation,
      finish: () => operationsRef.current.delete(controller) };
  }

  async function loadRecent() {
    const operation = beginOperation();
    if (!operation) return;
    try {
      const result = await apiFetch("/archive/exports?limit=200", { signal: operation.signal });
      if (!operation.isCurrent()) return;
      setRecent((result?.items || []).filter((item) => item.download_ready || ["queued", "running"].includes(item.status)));
      setRecentState("ready");
    } catch (_) { if (operation.isCurrent()) setRecentState("error"); }
    finally { operation.finish(); }
  }

  function stopWaiting() {
    if (!ownsSession()) return;
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
    const operation = beginOperation();
    if (!operation) return null;
    const { controller } = operation;
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
        isCurrent: operation.isCurrent,
        exportId: resumeId || (existing && ["queued", "running", "done"].includes(existing.status) ? existing.id : undefined),
        onStatus: (message, item) => {
          if (!operation.isCurrent()) return;
          setStatus(message);
          if (item?.id && item?.status) { jobRef.current = item; setJob(item); }
        },
      });
      if (!operation.isCurrent()) return null;
      jobRef.current = result.job;
      setJob(result.job);
      setStatus("Клип готов. Нажмите «Скачать клип».");
      setPhase("ready");
      loadRecent();
      return result;
    } catch (error) {
      if (!operation.isCurrent() || error?.name === "AbortError") return null;
      setStatus(normalizeArchiveExportError(error.message));
      setPhase("error");
      return null;
    } finally {
      operation.finish();
      if (activeRef.current === controller) {
        activeRef.current = null;
        if (operation.isCurrent()) setBusy(false);
      }
    }
  }

  async function download(manifest = false) {
    if (!jobRef.current?.download_ready || transferBusy) return;
    const operation = beginOperation();
    if (!operation) return;
    const exportId = jobRef.current.id;
    setTransferBusy(true);
    try {
      if (manifest) {
        const result = await downloadArchiveManifest(exportId, operation);
        if (!operation.isCurrent()) return;
        saveBlobDownload(result.blob, result.filename || "km-vms-clip-passport.json");
      } else {
        await startArchiveExportDownload(exportId, operation);
        if (!operation.isCurrent()) return;
        setPhase("downloading");
        setStatus("Загрузка передана браузеру. Ход, отмена и ошибки — в загрузках браузера. Готовый клип можно скачать повторно.");
      }
    } catch (error) {
      if (!operation.isCurrent() || error?.name === "AbortError") return;
      setPhase("error");
      setStatus(normalizeArchiveExportError(error.message));
    } finally { operation.finish(); if (operation.isCurrent()) setTransferBusy(false); }
  }

  function selectReady(id) {
    if (!ownsSession()) return;
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
