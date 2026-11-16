import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const read = path => readFileSync(new URL(path, import.meta.url), 'utf8');
test('Records toolbar scopes compact controls without changing shared forms', () => {
  const css = read('../app/styles/40-05-recordings-viewer.css');
  assert.match(css, /\.recordingsFilterCard \{[^}]*padding: 8px 12px/s);
  for (const selector of ['.recordingsFilterCard .recordingsFilterSelect', '.recordingsFilterCard .recordingsFilterDate']) {
    const body = css.slice(css.indexOf(selector)).split('}')[0];
    assert.match(body, /height: 36px/); assert.match(body, /box-sizing: border-box/);
  }
  assert.match(css, /@media \(pointer: coarse\)/);
  assert.match(css, /height: 44px; min-height: 44px/);
});
test('Camera select uses a centered fixed-size vector caret and retains keyboard/group semantics', () => {
  const selector = read('../components/ArchiveCameraSelect.js');
  assert.match(selector, /<svg className="archiveCameraChevron" aria-hidden="true" width="16" height="16"/);
  assert.doesNotMatch(selector, /⌄/);
  for (const marker of ['role="option"', 'role="separator"', 'ArrowUp', 'ArrowDown', 'Escape']) assert(selector.includes(marker));
});
