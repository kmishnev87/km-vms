import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pageSource = fs.readFileSync(resolve(__dirname, "../app/settings/page.js"), "utf8");
const routerSource = fs.readFileSync(resolve(__dirname, "../../api/app/routers/settings.py"), "utf8");

const confirmStart = pageSource.indexOf("async function confirmUpdateApply");
const confirmEnd = pageSource.indexOf("async function downloadMaintenanceReport", confirmStart);
assert.ok(confirmStart >= 0 && confirmEnd > confirmStart);
const confirmSource = pageSource.slice(confirmStart, confirmEnd);

assert.equal((confirmSource.match(/apiFetch\("\/system\/update\/apply"/g) || []).length, 1);
assert.equal(confirmSource.includes("submission_proof: persisted.submissionProof"), true);
assert.equal(confirmSource.includes("X-KM-VMS-Update-Submission-Proof"), false);
assert.equal(confirmSource.includes("commitUpdateApplyReconciliation(reconciliation)"), true);
assert.ok(
  confirmSource.indexOf("commitUpdateApplyReconciliation(reconciliation)") <
    confirmSource.indexOf('apiFetch("/system/update/apply"'),
);
assert.equal(confirmSource.includes("setTimeout(() => confirmUpdateApply"), false);
assert.equal(confirmSource.includes("window.alert"), false);
assert.equal(confirmSource.includes("window.confirm"), false);
assert.equal(confirmSource.includes("window.prompt"), false);

const exactLookupStart = pageSource.indexOf("async function loadUpdateApplySurface");
const exactLookupEnd = pageSource.indexOf("async function runMaintenanceDryRun", exactLookupStart);
assert.ok(exactLookupStart >= 0 && exactLookupEnd > exactLookupStart);
const exactLookupSource = pageSource.slice(exactLookupStart, exactLookupEnd);
assert.equal(exactLookupSource.includes("/system/update/apply/reconciliation/${encodeURIComponent(pending.submissionId)}"), true);
assert.equal(exactLookupSource.includes('"X-KM-VMS-Update-Submission-Proof": pending.submissionProof'), true);
assert.equal(exactLookupSource.includes("submission_proof:"), false);
assert.equal(exactLookupSource.includes("?submission_proof="), false);

const modelStart = routerSource.indexOf("class UpdateApplyRequest");
const modelEnd = routerSource.indexOf("class UpdateApplySubmissionTicketRequest", modelStart);
const modelSource = routerSource.slice(modelStart, modelEnd);
assert.match(modelSource, /submission_proof:\s*str\s*=\s*Field\(min_length=1, max_length=2048\)/);
assert.match(modelSource, /model_config\s*=\s*ConfigDict\(extra="forbid"\)/);

const routeStart = routerSource.indexOf("def system_update_apply(");
const routeEnd = routerSource.indexOf("def system_update_apply_reconciliation(", routeStart);
const routeSource = routerSource.slice(routeStart, routeEnd);
assert.equal(routeSource.includes("submission_proof: str | None = Header"), false);
assert.ok(routeSource.indexOf('request_fields.pop("submission_proof")') < routeSource.indexOf("reject_forbidden_apply_fields(request_fields)"));
assert.equal(routeSource.includes("submission_proof=submission_proof"), true);

assert.equal(pageSource.includes("{pending.submissionProof}"), false);
assert.equal(pageSource.includes("{persisted.submissionProof}"), false);

console.log("Stage 6.6.0.1.2.2 typed Apply proof transport tests passed");
