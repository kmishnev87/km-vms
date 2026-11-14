// Public metadata only. NAS credentials must never be sent to the release host.
export const ANDROID_RELEASE_METADATA = "https://raw.githubusercontent.com/kmishnev87/km-vms/main/apps/android/release.json";
export function androidRelease(value) {
  if (value?.format !== 1 || value?.package_name !== "ru.kmvms.mobile" || typeof value?.published !== "boolean") throw new Error("invalid_release");
  if (!value.published) return null;
  const version = value.version_name;
  if (!/^\d{1,5}\.\d{1,5}\.\d{1,5}$/.test(version || "")
      || !/^(android-)?v\d{1,5}\.\d{1,5}\.\d{1,5}$/.test(value.release_tag || "")
      || !Number.isInteger(value.version_code) || value.version_code < 1 || value.version_code > 2100000000
      || !Number.isInteger(value.size_bytes) || value.size_bytes < 1 || value.size_bytes > 256 * 1024 * 1024
      || !/^[a-f0-9]{64}$/.test(value.sha256 || "") || !/^[a-f0-9]{64}$/.test(value.certificate_sha256 || "")
      || !Number.isInteger(value.min_android_sdk) || value.min_android_sdk < 26 || value.min_android_sdk > 100) throw new Error("invalid_release");
  return { version, bytes: value.size_bytes, sha256: value.sha256,
    url: `https://github.com/kmishnev87/km-vms/releases/download/${value.release_tag}/km-vms-android-${version}.apk` };
}

export async function fetchAndroidRelease(signal) {
  const response = await fetch(ANDROID_RELEASE_METADATA, { credentials: "omit", cache: "no-store", redirect: "error", signal });
  if (response.status === 404) return null; // The first Android publication may not exist yet.
  if (!response.ok) throw new Error("release_unavailable");
  if (Number(response.headers.get("content-length")) > 65536) throw new Error("invalid_release");
  const reader = response.body.getReader();
  let size = 0;
  const chunks = [];
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 65536) throw new Error("invalid_release");
      chunks.push(value);
    }
  } finally { await reader.cancel(); reader.releaseLock(); }
  const data = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  return androidRelease(JSON.parse(new TextDecoder().decode(data)));
}
