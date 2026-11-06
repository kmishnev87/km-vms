export function supportSubmissionId(cryptoSource = globalThis.crypto) {
  const data = cryptoSource.getRandomValues(new Uint8Array(16));
  data[6] = (data[6] & 15) | 64;
  data[8] = (data[8] & 63) | 128;
  const hex = [...data].map(value => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

export function supportErrorKey(error) {
  const code = error?.code || error?.detail?.code || "";
  const keys = {
    support_not_configured: "notConfigured",
    support_busy: "busyError",
    support_storage_full: "storageFull",
    support_archive_too_large: "tooLarge",
    support_archive_failed: "archiveFailed",
  };
  return keys[code] || "deliveryFailed";
}

export function supportAttemptKey(text, mode) {
  return JSON.stringify([text.trim(), mode]);
}
