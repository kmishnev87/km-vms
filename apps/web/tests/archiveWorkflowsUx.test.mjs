import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const read = (path) => fs.readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

test('large clips retain the duration bound, not the old four GiB cap', () => {
  const context = vm.createContext({ Date, productCalendarDate: (value) => new Date(value), productInstantInputValue: (value) => value });
  vm.runInContext(read('lib/archiveExports.js').replace(/^import .*;$/gm, '').replace(/\bexport /g, '') + '\nglobalThis.limits = DEFAULT_ARCHIVE_EXPORT_LIMITS;', context);
  assert.equal(context.limits.max_estimated_source_bytes, null);
  assert.ok(context.limits.max_source_segments >= 180);
  assert.equal(context.validateArchiveExportSelection({startTs:'2026-11-01T00:00:00',endTs:'2026-11-01T03:00:00',estimatedSourceBytes:12*1024**3}), '');
  assert.ok(context.validateArchiveExportSelection({startTs:'2026-11-01T00:00:00',endTs:'2026-11-01T03:00:01'}));
});

test('both screens use the common dialog with their own context and clock adapter', () => {
  const recordings = read('app/recordings/page.js'), chronology = read('app/chronology/page.js');
  assert.match(recordings, /<ArchiveExportDialog[\s\S]*formatInput=\{toDateTimeLocal\} source="recordings"/);
  assert.match(chronology, /<ArchiveExportDialog[\s\S]*formatInput=\{formatDateTimeLocalInput\} source="chronology"/);
  assert.match(recordings, /contextKey = JSON.stringify\(\[selectedCamera, selectedDate, selectedPaths/);
  assert.match(chronology, /contextKey = JSON.stringify\(\[selectedCameraIds/);
  assert.match(chronology, /selectedCameraIds.length === 1 \? selectedCameraIds\[0\] : ""/);
  for (const source of [recordings, chronology]) assert.doesNotMatch(source, /className="modal archiveExportModal"/);
});

test('saved selection clears; list paging and deletion cannot cross an auth session', () => {
  const hook = read('lib/useArchiveExport.js');
  assert.match(hook, /if \(!id && !busy && !deleteBusy\)/);
  assert.match(hook, /jobRef.current = null/);
  assert.match(hook, /saved_only=true&limit=20&offset=/);
  assert.match(hook, /requestId !== recentRequestRef.current/);
  assert.match(hook, /method: "DELETE", signal: operation.signal/);
  assert.match(hook, /if \(!operation.isCurrent\(\)\) return false/);
  assert.match(read('components/ArchiveExportResult.js'), /confirmDelete \? copy.deleteConfirm/);
});

test('history is filtered and paged, separates old results from current relevance, and has no timer', () => {
  const history = read('components/storage/ArchiveOperationHistory.js');
  for (const field of ['period_days', 'operation_type', 'result_status', 'offset', 'current_state']) assert.ok(history.includes(field));
  assert.match(history, /copy.result/); assert.match(history, /copy.now/);
  assert.match(history, /onOpenIntegrity/); assert.match(history, /item.detail_count/);
  assert.doesNotMatch(history, /setInterval|setTimeout/);
  assert.match(history, /timeZone: "UTC"/);
  assert.doesNotMatch(history, /T00:00:00Z/);
});

test('logout is confirmed and source camera name is centered without changing card view', () => {
  const layout = read('components/Layout.js');
  assert.match(layout, /onClick=\{\(\) => setLogoutConfirmOpen\(true\)\}/);
  assert.match(layout, /onConfirm: logout/);
  assert.match(layout, /onClose=\{\(\) => setLogoutConfirmOpen\(false\)\}/);
  assert.match(read('app/cameras/page.js'), /cameraNameIdentity/);
  const storage = read('app/storage/page.js');
  assert.match(storage, /storageOpsLeftColumn/);
  assert.match(storage, /row.camera_deleted/);
});

test('deleted camera label stays beside the name, and history scroll excludes fixed controls', () => {
  const css = read('app/styles/40-09-storage-migration-integrity.css');
  assert.match(css, /\.storageCameraIdentity\s*\{[^}]*display: flex;[^}]*white-space: nowrap;/);
  assert.match(css, /\.storageCameraName\s*\{[^}]*text-overflow: ellipsis;/);
  assert.match(css, /\.archiveHistoryPagination > span\s*\{[^}]*white-space: nowrap;/);
  assert.match(css, /\.archiveHistoryProduct \.archiveHistoryPagination \.button\s*\{[^}]*width: auto;/);
  const storage = read('app/storage/page.js');
  assert.match(storage, /className: "archiveOperationHistoryDialog",\s*showFooterClose: false/);
  const history = read('components/storage/ArchiveOperationHistory.js');
  assert.ok(history.indexOf('className="archiveHistorySummary"') < history.indexOf('className="archiveHistoryScroll"'));
  assert.ok(history.indexOf('className="archiveHistoryPagination"') > history.indexOf('className="archiveHistoryScroll"'));
  assert.match(read('components/ArchiveExportDialog.js'), /camera.deleted \? ` \(\$\{copy.cameraDeletedMark\}\)`/);
});
