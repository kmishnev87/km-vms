import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const source = fs
  .readFileSync(resolve(__dirname, "../lib/currentUserCore.js"), "utf8")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const context = {};
vm.runInNewContext(
  `${source}
this.shouldFetchCurrentUser = shouldFetchCurrentUser;
this.normalizeCurrentUser = normalizeCurrentUser;
this.isCurrentUserDeniedError = isCurrentUserDeniedError;
this.CURRENT_USER_NO_TOKEN = CURRENT_USER_NO_TOKEN;
this.CURRENT_USER_DENIED = CURRENT_USER_DENIED;`,
  context
);

const {
  shouldFetchCurrentUser,
  normalizeCurrentUser,
  isCurrentUserDeniedError,
  CURRENT_USER_NO_TOKEN,
  CURRENT_USER_DENIED,
} = context;

assert.equal(CURRENT_USER_NO_TOKEN, "no_token");
assert.equal(CURRENT_USER_DENIED, "denied");

assert.equal(shouldFetchCurrentUser(""), false);
assert.equal(shouldFetchCurrentUser(null), false);
assert.equal(shouldFetchCurrentUser("token"), true);

assert.equal(normalizeCurrentUser(null), null);
assert.equal(
  JSON.stringify(normalizeCurrentUser({ username: "viewer" })),
  JSON.stringify({ username: "viewer", permissions: [] })
);
assert.equal(
  JSON.stringify(normalizeCurrentUser({ username: "operator", permissions: ["run_diagnostics"] })),
  JSON.stringify({ username: "operator", permissions: ["run_diagnostics"] })
);

for (const message of [
  "HTTP 401",
  "HTTP 403",
  "Not authenticated",
  "Invalid token",
  "Forbidden",
  "Section unavailable. User permissions are limited.",
]) {
  assert.equal(isCurrentUserDeniedError(new Error(message)), true);
}

assert.equal(isCurrentUserDeniedError(new Error("Network failed")), false);
