"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";
import { useI18n } from "../../lib/i18n";
import { humanBlockerReason } from "../../lib/storageOperations";
import { OperationToast } from "../OperationFeedback";

export const archiveOperationFinished = "kmvms-archive-operation-finished";
const Queue = createContext(() => {});
export const useArchiveOperationNotice = () => useContext(Queue);
const terminal = new Set(["completed", "partial", "blocked", "failed", "cancelled", "expired"]);
const copyByLocale = {
  ru: { move: "Перенос записей завершён", moveFailed: "Перенос записей выполнен не полностью", removed: "Папка архива отключена", removeFailed: "Папку архива не удалось отключить" },
  en: { move: "Recordings moved", moveFailed: "Could not move all recordings", removed: "Archive folder removed", removeFailed: "Could not remove the archive folder" },
  "zh-CN": { move: "录像迁移完成", moveFailed: "未能迁移全部录像", removed: "归档文件夹已移除", removeFailed: "无法移除归档文件夹" },
};

async function readJob(job, signal) {
  if (job.rootId) {
    const url = `/storage/archive-roots/${encodeURIComponent(job.rootId)}/removal/${encodeURIComponent(job.id)}`;
    return [await apiFetch(url, { signal })];
  }
  const rows = [];
  // One request per 40 accepted plans, independent of how many cameras are visible.
  for (let i = 0; i < job.planIds.length; i += 40) {
    const ids = job.planIds.slice(i, i + 40);
    const data = await apiFetch(`/storage/camera-archive-moves/status?ids=${ids.join(",")}`, { signal });
    rows.push(...ids.map(id => data.items.find(item => item.id === id) || { status: "failed" }));
  }
  return rows;
}

export default function ArchiveOperationNotices({ children }) {
  const { locale } = useI18n();
  const copy = copyByLocale[locale] || copyByLocale.en;
  const [jobs, setJobs] = useState([]);
  const [toasts, setToasts] = useState([]);
  const register = useCallback(job => {
    if (!job?.id || !(job.rootId || job.planIds?.length)) return;
    setJobs(list => list.some(row => row.id === job.id) ? list : [...list, job]);
  }, []);

  useEffect(() => {
    if (!jobs.length) return undefined;
    const controller = new AbortController();
    let timer;
    async function check() {
      if (document.visibilityState === "hidden") {
        timer = window.setTimeout(check, 5000);
        return;
      }
      const finished = [];
      for (const job of jobs) {
        try {
          const rows = await readJob(job, controller.signal);
          if (rows.length && rows.every(row => terminal.has(row.status))) {
            const reason = rows.find(row => row.status !== "completed")?.reason;
            const ok = !job.skipped && rows.every(row => row.status === "completed");
            finished.push({ id: job.id, tone: ok ? "success" : "warning",
              title: job.rootId ? (ok ? copy.removed : copy.removeFailed) : (ok ? copy.move : copy.moveFailed),
              message: reason ? humanBlockerReason(reason, locale) : "" });
          }
        } catch (error) {
          if (!controller.signal.aborted && error?.status === 404) {
            finished.push({ id: job.id, tone: "warning", title: job.rootId ? copy.removeFailed : copy.moveFailed });
          }
        }
      }
      if (controller.signal.aborted) return;
      if (finished.length) {
        setToasts(current => [...current, ...finished]);
        setJobs(current => current.filter(job => !finished.some(item => item.id === job.id)));
        window.dispatchEvent(new Event(archiveOperationFinished));
      } else timer = window.setTimeout(check, 5000);
    }
    timer = window.setTimeout(check, 1200);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [jobs, copy, locale]);

  return <Queue.Provider value={register}>
    {children}
    <OperationToast toast={toasts[0]} onClose={() => setToasts(rows => rows.slice(1))} />
  </Queue.Provider>;
}
