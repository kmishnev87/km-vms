import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const helper = fs.readFileSync(resolve(__dirname, "../lib/timezone.js"), "utf8");
const recordsPage = fs.readFileSync(resolve(__dirname, "../app/recordings/page.js"), "utf8");
const chronologyPage = fs.readFileSync(resolve(__dirname, "../app/chronology/page.js"), "utf8");
const settingsPage = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");
const settingsHelpers = fs.readFileSync(resolve(__dirname, "../lib/settingsPageHelpers.js"), "utf8");

assert.equal(helper.includes("formatProductDateTime"), true);
assert.equal(helper.includes("timeZone: normalizeProductTimezone(timezone)"), true);
assert.equal(helper.includes("productLocalInputToApi"), true);
assert.equal(helper.includes("productDateFilterParam"), true);

assert.equal(recordsPage.includes('params.set("date", productDateFilterParam(dateValue))'), true);
assert.equal(recordsPage.includes("data?.timezone?.id"), true);
assert.equal(recordsPage.includes("renderRecordingsTableDateTime(item.started_at_system, productTimezone)"), true);
assert.equal(recordsPage.includes("function formatRecordingsTableDateTime(value, timezone)"), true);
assert.equal(recordsPage.includes("formatProductDateTime(value, timezone)"), true);
assert.equal(recordsPage.includes("formatDateInputFromCreatedAt(item.created_at) === selectedDate"), false);

assert.equal(chronologyPage.includes("formatProductTimestampParam"), true);
assert.equal(chronologyPage.includes("productLocalInputToApi(formatLocalNaiveTs(dt))"), true);
assert.equal(chronologyPage.includes("formatProductTimestampParam(new Date(fromMs))"), true);

assert.equal(settingsHelpers.includes("return timezone || \"UTC\";"), true);
assert.equal(settingsPage.includes("<option value={draft.timezone}>{draft.timezone}</option>"), true);

const berlinFormatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Berlin",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const yekaFormatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Yekaterinburg",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

const instant = new Date("2026-03-29T00:30:00Z");
assert.notEqual(berlinFormatter.format(instant), yekaFormatter.format(instant));

const backendLocalNaiveSystemValue = new Date("2026-05-10T10:53:25+05:00");
const localNaiveDisplay = yekaFormatter.format(backendLocalNaiveSystemValue);
assert.equal(localNaiveDisplay.includes("10:53:25"), true);
assert.equal(localNaiveDisplay.includes("15:53:25"), false);
