import assert from "node:assert/strict";
import fs from "node:fs";
import { execFileSync } from "node:child_process";
const script = `
import fs from "node:fs"; import vm from "node:vm";
const timezone=fs.readFileSync("lib/timezone.js","utf8").replaceAll("export ","");
const helper=fs.readFileSync("lib/archiveExports.js","utf8").replace(/^import[^\\n]+\\n/gm,"").replaceAll("export ","");
const ctx={}; vm.runInNewContext(timezone+"\\n"+helper+"\\nthis.validate=validateArchiveExportSelection;",ctx);
const limits={max_duration_seconds:10800,max_estimated_source_bytes:1024};
const cases=[
 {startTs:"2026-11-01T00:30:00",endTs:"2026-11-01T03:30:00"},
 {startTs:"2026-03-08T00:30:00",endTs:"2026-03-08T03:30:00"},
 {startTs:"2026-11-01T04:30:00Z",endTs:"2026-11-01T08:30:00Z",productTimezone:"America/New_York"},
 {startTs:"2026-03-08T06:30:00Z",endTs:"2026-03-08T09:30:00Z",productTimezone:"America/New_York"},
 {startTs:"2026-11-01T03:30:00",endTs:"2026-11-01T00:30:00"},
 {startTs:"2026-11-01T00:30:00",endTs:"2026-11-01T03:30:01"},
 {startTs:"",endTs:"invalid"},
 {startTs:"2026-11-01T00:30:00",endTs:"2026-11-01T01:30:00",estimatedSourceBytes:2048}
];
console.log(JSON.stringify(cases.map(value=>Boolean(ctx.validate(value,limits)))));
`;
for (const zone of ["UTC","Asia/Yekaterinburg","America/New_York","Europe/Berlin"]) {
  const result=JSON.parse(execFileSync(process.execPath,["--input-type=module","-e",script],{
    cwd:new URL("..",import.meta.url),env:{...process.env,TZ:zone},encoding:"utf8",stdio:["pipe","pipe","pipe"]}));
  assert.deepEqual(result,[false,false,false,true,true,true,true,true],`browser zone ${zone}`);
}
for (const page of ["recordings","chronology"]) {
  const source=fs.readFileSync(new URL(`../app/${page}/page.js`,import.meta.url),"utf8");
  const submit=source.slice(source.indexOf("  async function submitExport()"));
  assert.match(submit.slice(0,800),/productTimezone/,"actual export submit supplies the installation timezone");
}
console.log("Actual export validator agrees across browser DST zones and product-local/aware inputs PASS");
