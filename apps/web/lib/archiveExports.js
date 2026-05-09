import { apiFetch, apiFetchBlob } from "./api";

export const EXPORT_PERMISSION = "export_recordings";

export function canExportRecordings(user) {
  return Array.isArray(user?.permissions) && user.permissions.includes(EXPORT_PERMISSION);
}

export function normalizeArchiveExportError(message) {
  const text = String(message || "").toLowerCase();
  if (!text) return "Не удалось подготовить экспорт.";
  if (text.includes("no exportable source")) return "В выбранном диапазоне нет готовых записей.";
  if (text.includes("source_missing")) return "Исходная запись недоступна.";
  if (text.includes("source_gap_detected")) return "В выбранном диапазоне есть разрыв архива.";
  if (text.includes("incompatible_segments")) return "Фрагменты нельзя безопасно сшить.";
  if (text.includes("generation_failed")) return "Не удалось сгенерировать фрагмент.";
  if (text.includes("manifest_not_ready") || text.includes("manifest_missing")) return "Manifest ещё не готов.";
  if (text.includes("checksum_mismatch")) return "Проверка целостности export-файла не прошла.";
  if (text.includes("expired_job") || text.includes("expired")) return "Export job истёк.";
  if (text.includes("forbidden") || text.includes("permissions") || text.includes("доступ")) return "Недостаточно прав для export workflow.";
  if (text.includes("/volume") || text.includes("internal_") || text.includes("traceback")) return "Сервер вернул внутреннюю ошибку export workflow.";
  return message || "Не удалось подготовить экспорт.";
}

export function buildArchiveExportPayload({ cameraId, startTs, endTs, title = "", reason = "" }) {
  return {
    camera_id: Number(cameraId),
    start_ts: startTs,
    end_ts: endTs,
    title: title || undefined,
    reason: reason || undefined,
    format_hint: "mkv",
  };
}

export async function runArchiveExportWorkflow(payload, { onStatus } = {}) {
  onStatus?.("queued");
  const created = await apiFetch("/archive/exports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  onStatus?.(created?.status || "queued", created);

  const generated = await apiFetch(`/archive/exports/${encodeURIComponent(created.id)}/generate`, {
    method: "POST",
  });
  onStatus?.(generated?.status || "running", generated);
  if (generated?.status !== "done") {
    throw new Error(generated?.error_message || generated?.error_code || "generation_failed");
  }

  const manifest = await apiFetch(`/archive/exports/${encodeURIComponent(created.id)}/manifest`, {
    method: "POST",
  });
  onStatus?.("manifest_ready", manifest);

  const clip = await apiFetchBlob(`/archive/exports/${encodeURIComponent(created.id)}/download`);
  onStatus?.("download_ready", generated);
  return { job: generated, manifest, clip };
}

export async function downloadArchiveManifest(exportId) {
  return apiFetchBlob(`/archive/exports/${encodeURIComponent(exportId)}/manifest/download`);
}

export function saveBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename || "km-vms-evidence-export";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
