import assert from "node:assert/strict";
import fs from "node:fs";
import { webcrypto } from "node:crypto";
import { supportAttemptKey, supportErrorKey, supportSubmissionId } from "../lib/supportReports.js";

const ids = new Set(Array.from({ length: 1000 }, () => supportSubmissionId(webcrypto)));
assert.equal(ids.size, 1000);
for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
assert.equal(supportAttemptKey(" x ", "normal"), supportAttemptKey("x", "normal"));
assert.notEqual(supportAttemptKey("x", "normal"), supportAttemptKey("x", "extended"));
assert.equal(supportErrorKey({ code: "support_storage_full" }), "storageFull");
assert.equal(supportErrorKey({ detail: { code: "support_archive_too_large" } }), "tooLarge");
assert.equal(supportErrorKey({ message: "private upstream error" }), "deliveryFailed");

const read = name => fs.readFileSync(new URL(`../${name}`, import.meta.url), "utf8");
const source = read("components/SupportReportForm.js");
const actSource = source.slice(source.indexOf("  async function act("), source.indexOf("\n  return ("));
assert.ok(actSource.includes("finally"));
function harness(api, changes={}) {
  const inputs = { disabled:false, text:"camera stopped", mode:"normal", consent:true, available:true, accepted:false, ...changes };
  const attempt = { current:null }, busyRef = { current:false }, receipts=[], toasts=[], busy=[];
  const args = { ...inputs, busyRef, attempt, actionKey:supportAttemptKey(inputs.text, inputs.mode),
    setBusy: value=>busy.push(value), onBusyChange: ()=>{}, setReceipt: value=>receipts.push(value), onToast:value=>toasts.push(value),
    t:key=>key, apiFetch:api, apiFetchBlob:()=>{throw new Error("unexpected download");},
    supportSubmissionId:()=>supportSubmissionId(webcrypto), supportErrorKey };
  const act = new Function(...Object.keys(args), `${actSource}; return act;`)(...Object.values(args));
  return {act,attempt,busyRef,receipts,toasts,busy};
}
const attempts=[];let failFirst=true;
const h=harness(async (path,options)=>{const body=JSON.parse(options.body);attempts.push({path,...body});if(failFirst){failFirst=false;throw new Error("network failure");}return {accepted:true,report_id:body.submission_id};});
await h.act("send");
assert.equal(h.receipts.length,0,"network failure is not success");
assert.equal(h.toasts.at(-1).message,"supportReport.deliveryFailed");
await h.act("send");
assert.equal(attempts.length,2);
assert.equal(attempts[0].submission_id,attempts[1].submission_id,"retry uses the same identity");
assert.equal(attempts[0].consent,true);
assert.equal(h.receipts.length,1);
assert.equal(h.busyRef.current,false);
for (const change of [{consent:false},{disabled:true},{accepted:true},{available:false},{text:"   "}]) {
  let calls=0;const blocked=harness(async()=>{calls++;},change);await blocked.act("send");assert.equal(calls,0);
}
let resolve;let calls=0;
const concurrent=harness(async(path,options)=>{calls++;const body=JSON.parse(options.body);await new Promise(done=>{resolve=done;});return {accepted:true,report_id:body.submission_id};});
const first=concurrent.act("send");await concurrent.act("send");assert.equal(calls,1);resolve();await first;
assert.match(source,/useId\(\)/,"stable SSR/hydration ids");
assert.match(source,/include_logs: true/);
assert.match(source,/maxLength=\{10000\}/);
assert.doesNotMatch(source,/setInterval|dangerouslySetInnerHTML/);
assert.match(read("app/settings/page.js"),/<SupportReportForm /);
assert.match(read("components/AuditDiagnosticsEntries.js"),/<SupportReportForm /);
assert.doesNotMatch(read("app/settings/page.js"),/\{t\.reportSendingPending\}|setDiagnosticArchive/);
for (const lang of ["ru","en","zhCN"]) assert.match(read(`lib/i18n/${lang}.js`),/supportReport:\s*\{/);
console.log("Support reports: explicit consent, retry identity, double-click gate, safe errors, shared form and three languages passed");
