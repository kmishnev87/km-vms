import assert from 'node:assert/strict';
import fs from 'node:fs';

let calls = [];
let handler;
globalThis.__archiveTestApi = {
  apiFetch: async (path, options = {}) => { calls.push([path, options.method || 'GET']); return handler(path, options); },
  apiFetchBlob: () => { throw new Error('No clip blob allowed'); },
};
const source = fs.readFileSync(new URL('../lib/archiveExports.js', import.meta.url), 'utf8')
  .replace('import { apiFetch, apiFetchBlob } from "./api";', 'const { apiFetch, apiFetchBlob } = globalThis.__archiveTestApi;');
const helper = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const done = { id: 'clip-one', status: 'done', output_size_bytes: 5_000_000_000, filename: 'large.mkv' };
handler = (path) => path === '/archive/exports' ? { id: done.id, status: 'queued' } : path.endsWith('/generate') ? done : { output: { size_bytes: done.output_size_bytes } };
let result = await helper.runArchiveExportWorkflow({ camera_id: 1 });
assert.equal(result.job.download_ready, true);
assert.equal(result.clip, undefined);
assert.equal(calls.length, 3);
assert.ok(calls.every(([path]) => !path.endsWith('/download')));

calls = [];
handler = (path) => path.endsWith('/clip-one') ? { ...done, manifest_ready: true } : { output: {} };
await helper.runArchiveExportWorkflow({}, { exportId: done.id });
assert.deepEqual(calls, [['/archive/exports/clip-one', 'GET'], ['/archive/exports/clip-one/manifest', 'GET']]);

calls = [];
let lastJobId;
handler = (path) => {
  if (path === '/archive/exports') return { id: done.id, status: 'queued' };
  if (path.endsWith('/generate')) throw new Error('Response lost after server completed');
  if (path.endsWith('/clip-one')) return done;
  return { output: {} };
};
result = await helper.runArchiveExportWorkflow({}, { onStatus: (_message, job) => { if (job?.id) lastJobId = job.id; } });
assert.equal(lastJobId, done.id);
assert.equal(result.job.status, 'done');
assert.equal(calls.filter(([path]) => path === '/archive/exports').length, 1);

calls = [];
handler = (path) => path.endsWith('/clip-one') ? { id: done.id, status: 'running', can_resume: true } : path.endsWith('/generate?resume=true') ? done : { output: {} };
result = await helper.runArchiveExportWorkflow({}, { exportId: done.id });
assert.equal(result.job.status, 'done');
assert.ok(calls.some(([path]) => path.endsWith('/generate?resume=true')));
assert.ok(calls.every(([path]) => path !== '/archive/exports'));

calls = [];
const abort = new AbortController();
abort.abort();
await assert.rejects(helper.runArchiveExportWorkflow({}, { signal: abort.signal }), { name: 'AbortError' });
assert.equal(calls.length, 0);

let anchor;
globalThis.document = {
  createElement: () => (anchor = { click() { this.clicked = true; }, remove() { this.removed = true; } }),
  body: { appendChild() {} },
};
handler = () => ({ media_token: 'synthetic-test-capability', filename: 'large.mkv', size_bytes: 5_000_000_000 });
const info = await helper.startArchiveExportDownload(done.id);
assert.equal(info.size_bytes, 5_000_000_000);
assert.equal(anchor.download, 'large.mkv');
assert.equal(anchor.clicked, true);
assert.equal(anchor.removed, true);
assert.ok(anchor.href.startsWith('/api/archive/exports/clip-one/file?'));
assert.ok(!('media_token' in info));

const resultComponent = fs.readFileSync(new URL('../components/ArchiveExportResult.js', import.meta.url), 'utf8');
assert.ok(resultComponent.includes('closeRef.current = onClose;'));
assert.ok(resultComponent.includes('closeRef.current?.();'));
assert.ok(!resultComponent.includes('stopPropagation(); onClose?.();'));
delete globalThis.__archiveTestApi;
delete globalThis.document;
console.log('Archive preparation, native download, interrupted response and job reuse PASS');
