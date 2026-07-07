import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");
const storageSource = read("lib/storageOperations.js")
  .replaceAll("export const ", "const ")
  .replaceAll("export function ", "function ");
const storagePage = read("app/storage/page.js");

const context = {};
vm.runInNewContext(`${storageSource}\nthis.isStorageAccessDeniedError = isStorageAccessDeniedError;`, context);

assert.equal(context.isStorageAccessDeniedError({ status: 401, message: "Unauthorized" }), true);
assert.equal(context.isStorageAccessDeniedError({ status: 403, message: "Forbidden" }), true);
assert.equal(context.isStorageAccessDeniedError({ response: { status: 403 }, message: "Request failed" }), true);
assert.equal(context.isStorageAccessDeniedError(new Error("Forbidden")), true);
assert.equal(context.isStorageAccessDeniedError(new Error("Not authenticated")), true);
assert.equal(context.isStorageAccessDeniedError(new Error("Invalid token")), true);
assert.equal(context.isStorageAccessDeniedError(new Error("Insufficient permissions")), true);
assert.equal(context.isStorageAccessDeniedError(new Error("Permission denied")), true);
assert.equal(context.isStorageAccessDeniedError(new Error("Права пользователя ограничены")), true);
assert.equal(context.isStorageAccessDeniedError(new Error("Недостаточно прав для действия")), true);

[
  "Состояние недоступно",
  "Корень архива недоступен",
  "Хранилище недоступно",
  "Доступность архива не подтверждена",
  "Archive root is unavailable",
  "State is unavailable",
].forEach((message) => {
  assert.equal(context.isStorageAccessDeniedError(new Error(message)), false, message);
});

assert.match(storagePage, /isStorageAccessDeniedError\(err\)/);
assert.doesNotMatch(storagePage, /message\.includes\("permission"\)/);
assert.doesNotMatch(storagePage, /message\.includes\("\\u0434\\u043e\\u0441\\u0442\\u0443\\u043f"\)/);
assert.doesNotMatch(storagePage, /message\.includes\("доступ"\)/);
assert.match(storagePage, /setRefreshWarning\(""\)/, "successful refresh clears stale-status warning");
assert.match(storagePage, /if \(silent && statusRef\.current\) \{[\s\S]*setRefreshWarning\(/, "silent refresh keeps previous status with warning");

const accessDeniedBranch = storagePage.slice(storagePage.indexOf("if (isStorageAccessDeniedError(err))"), storagePage.indexOf("if (silent && statusRef.current)"));
assert.match(accessDeniedBranch, /setAccessDenied\(true\)/, "real auth denial enters access denied state");
const silentBranch = storagePage.slice(storagePage.indexOf("if (silent && statusRef.current)"), storagePage.indexOf("setError(err?.message || copy.loadFailed)"));
assert.doesNotMatch(silentBranch, /setAccessDenied\(true\)/, "silent non-access errors do not replace the page with access denied");
