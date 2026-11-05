import assert from 'node:assert/strict';
import fs from 'node:fs';
import { formatMaintenanceMessage, normalizeMaintenanceBackendText } from '../lib/settingsPageSharedHelpers.js';

const message = 'Required archive validation image is unavailable.';
const action = 'Check Docker registry connectivity and retry. The current release remains active.';
assert.equal(normalizeMaintenanceBackendText(message), 'archive_probe_image_unavailable');
assert.equal(normalizeMaintenanceBackendText(action), 'archive_probe_image_unavailable_action');
for (const [lang, labels] of Object.entries({
  ru: ['Служебный образ недоступен.', 'Проверьте подключение и повторите.'],
  en: ['Image unavailable.', 'Check connectivity and retry.'],
  'zh-CN': ['辅助镜像不可用。', '请检查连接并重试。'],
})) {
  const t = { maintenanceMessageLabels: { archive_probe_image_unavailable: labels[0], archive_probe_image_unavailable_action: labels[1] } };
  assert.equal(formatMaintenanceMessage(message, t, lang, 'error'), labels[0]);
  assert.equal(formatMaintenanceMessage(action, t, lang, 'action'), labels[1]);
}
const page = fs.readFileSync(new URL('../app/settings/page.js', import.meta.url), 'utf8');
assert.equal((page.match(/archive_probe_image_unavailable:/g) || []).length, 3);
assert.equal((page.match(/archive_probe_image_unavailable_action:/g) || []).length, 3);
console.log('Archive probe dependency localization PASS');
