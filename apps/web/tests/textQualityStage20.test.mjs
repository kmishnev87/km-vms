import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const repoRoot = path.resolve(path.dirname(__filename), "../../..");

const scanRoots = [
  "apps/web/app",
  "apps/web/components",
  "apps/web/lib",
  "apps/api/app",
];

const excludedDirs = new Set([
  ".git",
  ".next",
  "node_modules",
  "dist",
  "build",
  "coverage",
  "__pycache__",
  ".pytest_cache",
]);

const textExtensions = new Set([
  ".js",
  ".mjs",
  ".jsx",
  ".ts",
  ".tsx",
  ".py",
  ".css",
  ".json",
]);

const mojibakeFragments = [
  "\u0420\u045f",
  "\u0420\u045b",
  "\u0420\u2022",
  "\u0420\ufffd",
  "\u0420\u045c",
  "\u0420\u00b5",
  "\u0421\u0453",
  "\u0421\u201a",
  "\u0421\u0402",
  "\u0432\u0402",
  "\u0432\u201e",
  "\u0420\u045c\u0420\u00b5",
  "\u0420\u045f\u0420\u00b5\u0421\u0402",
  "\u0420\u045b\u0421",
  "\u0420\u00b0",
  "\u0420\u0451",
  "\u0420\u00bb",
  "\u0420\u0491",
  "\u0420\u00b7",
  "\u0420\u2116",
];

const requiredRuText = [
  {
    file: "apps/web/lib/i18n/ru.js",
    text: 'eyebrow: "СОСТОЯНИЕ СИСТЕМЫ"',
    reason: "RU System Status eyebrow must not render as System Health.",
  },
  {
    file: "apps/web/lib/i18n/ru.js",
    text: 'retention: "Хранение записей"',
    reason: "RU Storage retention heading must be Russian on the Storage page.",
  },
  {
    file: "apps/web/components/AuditDiagnosticsEntries.js",
    text: 'retention: { ru: "Хранение", en: "Retention"',
    reason: "Diagnostics/Security Journal RU retention category label must be Russian.",
  },
  {
    file: "apps/web/lib/settingsPageHelpers.js",
    text: 'retention: { ru: "Хранение", en: "Retention"',
    reason: "Settings Journal RU retention category label must be Russian.",
  },
];

const ruVisibleOffenders = [
  {
    file: "apps/web/lib/i18n/ru.js",
    segmentStart: "export const ruDictionary = {",
    segmentEnd: "ruDictionary.setup = {",
    pattern: /eyebrow:\s*"System Health"/,
    reason: "System Status RU eyebrow was the known visible offender.",
  },
  {
    file: "apps/web/lib/i18n/ru.js",
    segmentStart: "  storagePage: {",
    segmentEnd: "ruDictionary.setup = {",
    pattern: /retentionWorkflow:\s*"Retention workflow"|retention-кандидатов|owned записи|metadata-проверки|opt-in|metadata\/status|legacy archive file|Stage 2|orphan\/foreign\/unknown\/pre-metadata/,
    reason: "Storage RU retention copy must not expose known hybrid English remnants.",
  },
  {
    file: "apps/web/components/AuditDiagnosticsEntries.js",
    segmentStart: "const AUDIT_LABELS = {",
    segmentEnd: "function localizedLabel",
    pattern: /ru:\s*"(Retention|Live|Recorder|Reconciliation|Security)"/,
    reason: "AuditDiagnosticsEntries RU audit labels must not use English category/severity labels.",
  },
  {
    file: "apps/web/lib/settingsPageHelpers.js",
    segmentStart: "const AUDIT_LABELS = {",
    segmentEnd: "const BACKEND_LABELS",
    pattern: /ru:\s*"(Retention|Live|Recorder|Reconciliation|Security)"/,
    reason: "Settings helper RU audit labels must not use English category/severity labels.",
  },
];

const globalEnglishOffenders = [
  {
    pattern: /\bSYSTEM HEALTH\b/,
    reason: "Uppercase System Status label must not reappear in product source.",
  },
];

const allowedRuTechnicalFragments = [
  // Product and protocol/vendor identifiers are intentionally not translated.
  "KM VMS",
  "RTSP",
  "ONVIF",
  "HLS",
  "HTTP",
  "HTTPS",
  "FFmpeg",
  "Docker",
  "API",
  "NAS",
  "APK",
  "Android",
  "Intel",
  "QSV",
  "VAAPI",
  "NVIDIA",
  "AMF",
  "CPU",
  "PRO",
  // Existing explicit technical terms in RU UI that are useful for operators/admins.
  "backend",
  "runtime",
  "endpoint",
  "debug",
  "workspace",
  "canvas",
  "metadata",
  "Stage",
];

function walkFiles(root) {
  const absolute = path.join(repoRoot, root);
  const entries = fs.readdirSync(absolute, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    if (excludedDirs.has(entry.name)) continue;
    const fullPath = path.join(absolute, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkFiles(path.relative(repoRoot, fullPath)));
      continue;
    }
    if (entry.isFile() && textExtensions.has(path.extname(entry.name))) {
      files.push(path.relative(repoRoot, fullPath).replaceAll(path.sep, "/"));
    }
  }
  return files;
}

const files = scanRoots.flatMap(walkFiles);
const fileText = new Map(files.map((file) => [file, fs.readFileSync(path.join(repoRoot, file), "utf8")]));

function segment(text, startMarker, endMarker) {
  const start = text.indexOf(startMarker);
  assert.notEqual(start, -1, `segment start not found: ${startMarker}`);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1, `segment end not found: ${endMarker}`);
  return text.slice(start, end);
}

for (const { file, text, reason } of requiredRuText) {
  assert(fileText.get(file)?.includes(text), `${file}: ${reason}`);
}

const findings = [];

for (const [file, text] of fileText) {
  if (text.includes("\uFFFD")) {
    findings.push(`${file}: replacement character U+FFFD`);
  }
  if (/[?]{3,}/.test(text)) {
    findings.push(`${file}: suspicious repeated question marks`);
  }
  for (const fragment of mojibakeFragments) {
    if (text.includes(fragment)) {
      findings.push(`${file}: mojibake fragment ${JSON.stringify(fragment)}`);
    }
  }
}

for (const offender of ruVisibleOffenders) {
  const text = fileText.get(offender.file);
  if (text && offender.pattern.test(segment(text, offender.segmentStart, offender.segmentEnd))) {
    findings.push(`${offender.file}: ${offender.reason}`);
  }
}

for (const offender of globalEnglishOffenders) {
  for (const [file, text] of fileText) {
    if (offender.pattern.test(text)) {
      findings.push(`${file}: ${offender.reason}`);
    }
  }
}

assert.deepEqual(findings, []);
