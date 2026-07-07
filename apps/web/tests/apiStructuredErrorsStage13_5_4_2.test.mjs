import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = fs
  .readFileSync(resolve(__dirname, "../lib/api.js"), "utf8")
  .replace(/import \{ canUserAccessRoute \} from ".\/routePermissions";\r?\n/, "function canUserAccessRoute() { return true; }\n")
  .replaceAll("export function ", "function ")
  .replaceAll("export async function ", "async function ");

function makeHeaders(contentType = "application/json") {
  return { get: (name) => (String(name).toLowerCase() === "content-type" ? contentType : "") };
}

const context = {
  process: { env: {} },
  localStorage: { getItem: () => null, removeItem: () => null },
  sessionStorage: { getItem: () => null, removeItem: () => null },
  fetch: null,
};
vm.runInNewContext(`${source}\nthis.apiFetch = apiFetch;`, context);

context.fetch = async () => ({
  ok: false,
  status: 409,
  statusText: "Conflict",
  headers: makeHeaders(),
  json: async () => ({
    detail: {
      status: "blocked",
      blockers: [{ reason: "active_recording_jobs" }],
    },
  }),
});

await assert.rejects(
  () => context.apiFetch("/storage/migration/apply", { method: "POST" }),
  (error) => {
    assert.equal(error.status, 409);
    assert.equal(error.response.status, 409);
    assert.equal(error.detail.status, "blocked");
    assert.equal(error.detail.blockers[0].reason, "active_recording_jobs");
    assert.match(error.message, /blocked|active_recording_jobs|409/i);
    return true;
  }
);

context.fetch = async () => ({
  ok: false,
  status: 403,
  statusText: "Forbidden",
  headers: makeHeaders(),
  json: async () => ({ detail: { error: "delete_recordings_permission_missing" } }),
});

await assert.rejects(
  () => context.apiFetch("/recordings/retention/dry-run", { method: "POST" }),
  (error) => {
    assert.equal(error.status, 403);
    assert.equal(error.detail.error, "delete_recordings_permission_missing");
    assert.equal(error.message, "Раздел недоступен. Права пользователя ограничены.");
    return true;
  }
);
