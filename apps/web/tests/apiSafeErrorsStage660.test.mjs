import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = fs
  .readFileSync(resolve(__dirname, "../lib/api.js"), "utf8")
  .replace(/import \{ canUserAccessRoute \} from "\.\/routePermissions";\r?\n/, "function canUserAccessRoute() { return true; }\n")
  .replaceAll("export function ", "function ")
  .replaceAll("export async function ", "async function ");

function headers(values = {}) {
  const normalized = Object.fromEntries(Object.entries(values).map(([key, value]) => [key.toLowerCase(), value]));
  return { get: (name) => normalized[String(name).toLowerCase()] || "" };
}

const context = {
  process: { env: {} },
  localStorage: { getItem: () => "ru", removeItem: () => null },
  sessionStorage: { getItem: () => null, removeItem: () => null },
  fetch: null,
};
vm.runInNewContext(`${source}\nthis.apiFetch = apiFetch; this.apiFetchBlob = apiFetchBlob;`, context);

context.fetch = async () => ({
  ok: true,
  status: 200,
  statusText: "OK",
  headers: headers({ "content-type": "application/json" }),
  json: async () => ({ ok: true }),
});
assert.equal((await context.apiFetch("/health")).ok, true);

context.fetch = async () => ({
  ok: true,
  status: 200,
  statusText: "OK",
  headers: headers({ "content-type": "text/plain" }),
  text: async () => "ready",
});
assert.equal(await context.apiFetch("/text"), "ready");

const blobValue = { size: 12 };
context.fetch = async () => ({
  ok: true,
  status: 200,
  statusText: "OK",
  headers: headers({ "content-type": "application/zip", "content-disposition": 'attachment; filename="report.zip"' }),
  blob: async () => blobValue,
});
const blobResult = await context.apiFetchBlob("/report");
assert.equal(blobResult.blob, blobValue);
assert.equal(blobResult.filename, "report.zip");

context.fetch = async () => ({
  ok: false,
  status: 409,
  statusText: "Conflict",
  headers: headers({ "content-type": "application/json" }),
  json: async () => ({ detail: { status: "blocked", blockers: [{ reason: "active_recording_jobs" }] } }),
});
await assert.rejects(() => context.apiFetch("/typed"), (error) => {
  assert.equal(error.category, "typed_backend_error");
  assert.equal(error.status, 409);
  assert.equal(error.detail.status, "blocked");
  assert.match(error.message, /blocked|active_recording_jobs/i);
  assert.ok(error.message.length <= 240);
  return true;
});

for (const status of [401, 403]) {
  context.fetch = async () => ({
    ok: false,
    status,
    statusText: status === 401 ? "Unauthorized" : "Forbidden",
    headers: headers({ "content-type": "application/json" }),
    json: async () => ({ detail: status === 403 ? "Insufficient permissions" : "session_expired" }),
  });
  await assert.rejects(() => context.apiFetch("/auth"), (error) => {
    assert.equal(error.category, status === 401 ? "unauthorized" : "permission_denied");
    assert.equal(error.status, status);
    if (status === 403) assert.equal(error.message, "Раздел недоступен. Права пользователя ограничены.");
    return true;
  });
}

const html = "<!doctype html><html><body><h1>502 Bad Gateway</h1>" + "x".repeat(1000) + "</body></html>";
let responseTextRead = false;
context.fetch = async () => ({
  ok: false,
  status: 502,
  statusText: "Bad Gateway",
  headers: headers({ "content-type": "text/html" }),
  text: async () => {
    responseTextRead = true;
    return html;
  },
});
await assert.rejects(() => context.apiFetch("/proxy"), (error) => {
  assert.equal(error.category, "temporarily_unavailable");
  assert.equal(error.status, 502);
  assert.equal(error.message.includes("<html"), false);
  assert.equal(error.message.includes("Bad Gateway"), false);
  assert.ok(error.message.length <= 240);
  return true;
});
assert.equal(responseTextRead, false, "non-JSON error body must never be read into frontend state");

context.fetch = async () => ({
  ok: false,
  status: 503,
  statusText: "Unavailable",
  headers: headers({ "content-type": "text/html" }),
  text: async () => html,
});
await assert.rejects(() => context.apiFetchBlob("/proxy-report"), (error) => {
  assert.equal(error.category, "temporarily_unavailable");
  assert.equal(error.message.includes("<html"), false);
  return true;
});

context.fetch = async () => {
  throw new TypeError("Failed to fetch https://internal.example/token/secret");
};
await assert.rejects(() => context.apiFetch("/network"), (error) => {
  assert.equal(error.category, "network_unavailable");
  assert.equal(error.status, 0);
  assert.equal(error.message.includes("internal.example"), false);
  assert.ok(error.message.length <= 240);
  return true;
});

context.fetch = async () => ({
  ok: false,
  status: 422,
  statusText: "Invalid",
  headers: headers({ "content-type": "application/json" }),
  json: async () => ({ detail: "z".repeat(2000) }),
});
await assert.rejects(() => context.apiFetch("/bounded"), (error) => {
  assert.ok(error.message.length <= 240);
  return true;
});
