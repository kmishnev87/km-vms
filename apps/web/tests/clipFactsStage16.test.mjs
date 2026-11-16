import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const read = (path) => fs.readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const context = vm.createContext({ Date, productCalendarDate: (value) => new Date(value), productInstantInputValue: (value) => value });
vm.runInContext(read('lib/archiveExports.js').replace(/^import .*;$/gm, '').replace(/\bexport /g, ''), context);

test('saved clip duration is the measured media duration, not the requested range', () => {
  assert.equal(context.clipDurationSeconds({duration_seconds:960,output_duration_seconds:956.7}),956.7);
  assert.equal(context.clipDurationSeconds({duration_seconds:960,output_duration_seconds:null}),null);
  assert.equal(context.clipDurationSeconds({duration_seconds:960}),960);
  for (const value of [null, undefined, NaN, Infinity, -1, 0, '60']) assert.equal(context.formatClipDuration(value),'—');
  for (const [value,text] of [[0.2,'0:01'],[60,'1:00'],[956.7,'15:57'],[3723,'1:02:03']]) assert.equal(context.formatClipDuration(value),text);
});

test('shared saved clips dialog includes size and duration in all three languages', () => {
  const source=read('components/ArchiveExportDialog.js');
  assert.match(source,/copy\.duration/);
  assert.match(source,/formatClipDuration\(clipDurationSeconds\(job\)\)/);
  for (const [locale,label] of [['en','Duration'],['ru','Длительность'],['zhCN','时长']]) assert.ok(read(`lib/i18n/${locale}.js`).includes(`"duration": "${label}"`));
});
