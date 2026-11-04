import { readProductSourceFile } from "./helpers/readProductSources.mjs";
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

// Exercise the actual page helpers under different browser-like local zones,
// including the spring DST gap, not just Intl formatting in isolation.
const script = `
import fs from 'node:fs'; import vm from 'node:vm';
import { productInstantInputValue, productCalendarDate, productCalendarInputValue } from './lib/timezone.js';
const NativeDate=Date; class FixedDate extends NativeDate { constructor(...args){ super(...(args.length?args:['2026-03-08T07:30:00Z'])); } }
const records=fs.readFileSync('app/recordings/page.js','utf8');
const clip=records.slice(records.indexOf('function defaultClipRange('),records.indexOf('function formatRecordingsTableDateTime('));
const chrono=fs.readFileSync('app/chronology/page.js','utf8');
const now=chrono.slice(chrono.indexOf('function getNow('),chrono.indexOf('function formatPlaybackDateTime('));
const context={Date:FixedDate,productInstantInputValue,productCalendarDate,productCalendarInputValue};
vm.runInNewContext(clip+now+'; this.range=defaultClipRange; this.now=getNow;',context);
console.log(JSON.stringify({range:context.range('',{},'Asia/Yekaterinburg'),now:productCalendarInputValue(context.now('Asia/Yekaterinburg')),gap:productCalendarInputValue(productCalendarDate('2026-03-08T02:30:00'))}));
`;
for (const zone of ['UTC','Asia/Yekaterinburg','America/New_York','Europe/Berlin']) {
  const result=JSON.parse(execFileSync(process.execPath,['--input-type=module','-e',script],{cwd:new URL('..',import.meta.url),env:{...process.env,TZ:zone},encoding:'utf8',stdio:['pipe','pipe','pipe']}));
  assert.deepEqual(result,{range:{startTs:'2026-03-08T12:29:00',endTs:'2026-03-08T12:30:00'},now:'2026-03-08T12:30:00',gap:'2026-03-08T02:30:00'});
}
