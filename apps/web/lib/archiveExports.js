import { apiFetch, apiFetchBlob } from "./api";

export const EXPORT_PERMISSION = "export_recordings";

export const DEFAULT_ARCHIVE_EXPORT_LIMITS = {
  max_duration_seconds: 3 * 60 * 60,
  max_source_segments: 120,
  max_estimated_source_bytes: 4 * 1024 * 1024 * 1024,
  format_hints: ["mkv", "mp4"],
};

const STATUS_MESSAGES = {
  queued: "Заявка на создание клипа создана.",
  running: "Клип подготавливается.",
  done: "Клип готов.",
  failed: "Не удалось подготовить клип.",
  manifest_ready: "Паспорт клипа подготовлен.",
  download_ready: "Скачивание клипа начато.",
};

export function canExportRecordings(user) {
  return Array.isArray(user?.permissions) && user.permissions.includes(EXPORT_PERMISSION);
}

export function archiveExportStatusMessage(status) {
  return STATUS_MESSAGES[status] || STATUS_MESSAGES.running;
}

export function normalizeArchiveExportError(message) {
  const text = String(message || "").toLowerCase();
  if (!text) return "Не удалось подготовить клип.";
  if (text.includes("no exportable source")) return "В выбранном диапазоне нет готовых записей.";
  if (text.includes("source_missing")) return "Исходная запись недоступна.";
  if (text.includes("source_gap_detected")) return "В выбранном диапазоне есть разрыв архива.";
  if (text.includes("incompatible_segments")) return "Фрагменты нельзя безопасно объединить.";
  if (text.includes("generation_failed")) return "Не удалось сгенерировать клип.";
  if (text.includes("manifest_not_ready") || text.includes("manifest_missing")) return "Паспорт клипа еще не готов.";
  if (text.includes("checksum_mismatch")) return "Проверка целостности клипа не прошла.";
  if (text.includes("expired_job") || text.includes("expired")) return "Срок хранения клипа истек.";
  if (text.includes("too many source segments")) return "Диапазон содержит слишком много исходных фрагментов.";
  if (text.includes("range is too long")) return "Диапазон превышает допустимый лимит создания клипа.";
  if (text.includes("estimated export source size")) return "Оценочный объем исходных данных превышает лимит.";
  if (text.includes("forbidden") || text.includes("permissions") || text.includes("доступ")) return "Недостаточно прав для создания клипа.";
  if (text.includes("/volume") || text.includes("internal_") || text.includes("traceback")) return "Сервер вернул внутреннюю ошибку создания клипа.";
  if (text.startsWith("http ")) return "Не удалось подготовить клип.";
  return message || "Не удалось подготовить клип.";
}

export function normalizeChronologyDownloadError(message) {
  const text = String(message || "").toLowerCase();
  if (!text) return "Не удалось скачать текущую запись.";
  if (text.includes("recording is unavailable") || text.includes("not found")) return "На выбранное время нет готовой записи.";
  if (text.includes("source is unavailable") || text.includes("file not found")) return "Исходный файл записи недоступен.";
  if (text.includes("invalid timestamp")) return "Выбрано некорректное время.";
  if (text.includes("forbidden") || text.includes("доступ")) return "Недостаточно прав для скачивания записи.";
  if (text.includes("/volume") || text.includes("relative_path") || text.includes("traceback")) return "Сервер вернул внутреннюю ошибку скачивания.";
  return message || "Не удалось скачать текущую запись.";
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

export async function getArchiveExportLimits() {
  try {
    return { ...DEFAULT_ARCHIVE_EXPORT_LIMITS, ...(await apiFetch("/archive/exports/limits")) };
  } catch (_) {
    return DEFAULT_ARCHIVE_EXPORT_LIMITS;
  }
}

export function formatDuration(seconds) {
  const total = Math.max(0, Number(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = Math.floor(total % 60);
  if (hours && minutes) return `${hours} ч ${minutes} мин`;
  if (hours) {
    if (hours === 1) return "1 час";
    if (hours >= 2 && hours <= 4) return `${hours} часа`;
    return `${hours} часов`;
  }
  if (minutes && rest) return `${minutes} мин ${rest} сек`;
  if (minutes) return `${minutes} мин`;
  return `${rest} сек`;
}

export function formatBytes(sizeBytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(sizeBytes || 0);
  let unit = units[0];
  for (const current of units) {
    unit = current;
    if (value < 1024 || current === units[units.length - 1]) break;
    value /= 1024;
  }
  if (unit === "GB" || unit === "TB") return `${value.toFixed(2)} ${unit}`;
  if (unit === "MB") return `${value < 100 ? value.toFixed(1) : value.toFixed(0)} ${unit}`;
  if (unit === "KB") return `${value.toFixed(0)} ${unit}`;
  return `${Math.round(value)} ${unit}`;
}

export function describeArchiveExportLimits(limits = DEFAULT_ARCHIVE_EXPORT_LIMITS) {
  return [
    `Максимальный диапазон: ${formatDuration(limits.max_duration_seconds)}.`,
    `Максимум исходных фрагментов: ${limits.max_source_segments}.`,
    `Оценочный объем исходников: до ${formatBytes(limits.max_estimated_source_bytes)}.`,
  ].join(" ");
}

export function validateArchiveExportSelection({ startTs, endTs, estimatedSourceBytes = 0 }, limits = DEFAULT_ARCHIVE_EXPORT_LIMITS) {
  const start = new Date(startTs);
  const end = new Date(endTs);
  if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) {
    return "Выберите корректные начало и конец диапазона.";
  }
  if (end <= start) return "Конец диапазона должен быть позже начала.";
  const duration = Math.round((end.getTime() - start.getTime()) / 1000);
  if (duration > Number(limits.max_duration_seconds || DEFAULT_ARCHIVE_EXPORT_LIMITS.max_duration_seconds)) {
    return "Диапазон слишком большой для создания клипа. Максимум 3 часа; для больших периодов скачайте обычные исходные записи.";
  }
  if (estimatedSourceBytes && Number(limits.max_estimated_source_bytes) && estimatedSourceBytes > Number(limits.max_estimated_source_bytes)) {
    return "Оценочный объем исходников превышает лимит создания клипа.";
  }
  return "";
}

export async function runArchiveExportWorkflow(payload, { onStatus } = {}) {
  onStatus?.(archiveExportStatusMessage("queued"), null, "queued");
  const created = await apiFetch("/archive/exports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  onStatus?.(archiveExportStatusMessage(created?.status || "queued"), created, created?.status || "queued");

  const generated = await apiFetch(`/archive/exports/${encodeURIComponent(created.id)}/generate`, {
    method: "POST",
  });
  onStatus?.(archiveExportStatusMessage(generated?.status || "running"), generated, generated?.status || "running");
  if (generated?.status !== "done") {
    throw new Error(generated?.error_message || generated?.error_code || "generation_failed");
  }

  const manifest = await apiFetch(`/archive/exports/${encodeURIComponent(created.id)}/manifest`, {
    method: "POST",
  });
  onStatus?.(archiveExportStatusMessage("manifest_ready"), manifest, "manifest_ready");

  const clip = await apiFetchBlob(`/archive/exports/${encodeURIComponent(created.id)}/download`);
  onStatus?.(archiveExportStatusMessage("download_ready"), generated, "download_ready");
  return { job: generated, manifest, clip };
}

export async function downloadArchiveManifest(exportId) {
  return apiFetchBlob(`/archive/exports/${encodeURIComponent(exportId)}/manifest/download`);
}

export async function issueChronologyDownloadToken(cameraId, timestamp) {
  return apiFetch(
    `/chronology/download-token?camera_id=${encodeURIComponent(cameraId)}&ts=${encodeURIComponent(timestamp)}`,
    { method: "POST" }
  );
}

export async function startChronologyCurrentRecordingDownload(cameraId, timestamp) {
  const tokenInfo = await issueChronologyDownloadToken(cameraId, timestamp);
  const mediaToken = tokenInfo?.media_token || "";
  if (!mediaToken) throw new Error("Recording download token is unavailable");
  const url = `/api/chronology/download?camera_id=${encodeURIComponent(cameraId)}&ts=${encodeURIComponent(timestamp)}&media_token=${encodeURIComponent(mediaToken)}`;
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = tokenInfo?.filename || "km-vms-recording.mkv";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  return tokenInfo;
}

export function saveBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename || "km-vms-download";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
