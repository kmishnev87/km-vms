import fs from "node:fs";
import vm from "node:vm";

const sourcePath = new URL("../lib/i18n.js", import.meta.url);
const source = fs.readFileSync(sourcePath, "utf8");

function extractConstObject(name) {
  const marker = `export const ${name} = `;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`${name} export not found`);
  const objectStart = source.indexOf("{", start);
  let depth = 0;
  for (let index = objectStart; index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") depth += 1;
    if (char === "}") depth -= 1;
    if (depth === 0) return source.slice(objectStart, index + 1);
  }
  throw new Error(`${name} object end not found`);
}

function extractConstArray(name) {
  const marker = `export const ${name} = `;
  const start = source.indexOf(marker);
  if (start < 0) throw new Error(`${name} export not found`);
  const arrayStart = source.indexOf("[", start);
  let depth = 0;
  for (let index = arrayStart; index < source.length; index += 1) {
    const char = source[index];
    if (char === "[") depth += 1;
    if (char === "]") depth -= 1;
    if (depth === 0) return source.slice(arrayStart, index + 1);
  }
  throw new Error(`${name} array end not found`);
}

const context = {};
vm.createContext(context);
vm.runInContext(`SUPPORTED_LOCALES = ${extractConstArray("SUPPORTED_LOCALES")}; DICTIONARIES = ${extractConstObject("DICTIONARIES")};`, context);

const supported = context.SUPPORTED_LOCALES.map((item) => item.code);
const expected = ["ru", "en", "zh-CN"];
const errors = [];

for (const locale of expected) {
  if (!supported.includes(locale)) errors.push(`missing supported locale ${locale}`);
  if (!context.DICTIONARIES[locale]) errors.push(`missing dictionary ${locale}`);
}

function flatten(source, prefix = "") {
  return Object.entries(source || {}).flatMap(([key, value]) => {
    const next = prefix ? `${prefix}.${key}` : key;
    if (Array.isArray(value)) return value.map((item, index) => [`${next}.${index}`, item]);
    if (value && typeof value === "object") return flatten(value, next);
    return [[next, value]];
  });
}

const reference = new Map(flatten(context.DICTIONARIES.ru));
const placeholderPattern = new RegExp("TODO|TBD|FIXME|\\?\\?\\?|undefined|null|\\uFFFD", "i");
const placeholderValuePattern = /^(MISSING|TODO|TBD|FIXME)$/i;

for (const locale of expected) {
  const dictionary = new Map(flatten(context.DICTIONARIES[locale]));
  for (const [key, value] of reference.entries()) {
    if (!dictionary.has(key)) {
      errors.push(`${locale} missing ${key}`);
      continue;
    }
    const candidate = dictionary.get(key);
    const candidateText = String(candidate).trim();
    if (typeof value === "string" && (!candidateText || placeholderPattern.test(candidateText) || placeholderValuePattern.test(candidateText))) {
      errors.push(`${locale} invalid ${key}`);
    }
  }
  for (const key of dictionary.keys()) {
    if (!reference.has(key)) errors.push(`${locale} extra ${key}`);
  }
}

const stage4Files = [
  "app/cameras/page.js",
  "app/storage/page.js",
  "app/recordings/page.js",
  "app/setup/page.js",
  "app/chronology/page.js",
  "app/settings/page.js",
  "app/live/page.js",
  "app/page.js",
  "components/Layout.js",
  "components/OperatorProblemBanners.js",
];

const hardScopeNoCyrillicFiles = [
  "app/cameras/page.js",
  "app/storage/page.js",
];

const cyrillicPattern = /[А-Яа-яЁё]/;
const ruEnOnlyMapPattern = /(?:ru\s*:\s*["'`]|["']ru["']\s*:)[\s\S]{0,600}(?:en\s*:\s*["'`]|["']en["']\s*:)(?![\s\S]{0,600}(?:"zh-CN"|'zh-CN'|zh-CN)\s*:)/;
const camerasHardcodedEnglishLabels = [
  "ONVIF Host / IP",
  "IP / Host",
  "ONVIF Port",
  "RTSP Port",
  "RTSP Main Path / URL",
  "RTSP Sub Path / URL",
  "RTSP reachable host",
  "RTSP reachable port",
  "RTSP Transport",
  "ONVIF Path:",
  "Video parameters unavailable",
  "Audio: none",
  "ONVIF video config unavailable",
  "No writable ONVIF settings available for the selected profile.",
  "RTSP: Ready",
  "RTSP: Path missing",
  "System: Ready",
  "System: Check required",
  "Profile Token:",
  "Channel ID:",
];

for (const relativePath of hardScopeNoCyrillicFiles) {
  const content = fs.readFileSync(new URL(`../${relativePath}`, import.meta.url), "utf8");
  content.split(/\r?\n/).forEach((line, index) => {
    if (cyrillicPattern.test(line)) {
      errors.push(`${relativePath}:${index + 1} hardcoded Cyrillic outside i18n dictionary`);
    }
  });
}

for (const relativePath of stage4Files) {
  const content = fs.readFileSync(new URL(`../${relativePath}`, import.meta.url), "utf8");
  if (ruEnOnlyMapPattern.test(content)) {
    errors.push(`${relativePath} contains a local ru/en-only map without zh-CN`);
  }
}

const camerasSource = fs.readFileSync(new URL("../app/cameras/page.js", import.meta.url), "utf8");
for (const label of camerasHardcodedEnglishLabels) {
  if (camerasSource.includes(label)) {
    errors.push(`app/cameras/page.js contains hardcoded English UI label: ${label}`);
  }
}
if (/\$\{rec\}\s*rec\s*·\s*\$\{live\}\s*live/.test(camerasSource) || /rec\s*·\s*.*live/.test(camerasSource)) {
  errors.push("app/cameras/page.js contains hardcoded English stream-count summary");
}
if (/`token\s+\$\{/.test(camerasSource) || /parts\.push\(\s*["'`]token\s+/i.test(camerasSource)) {
  errors.push("app/cameras/page.js contains hardcoded token summary prefix");
}

const selfTestRussian = "const bad = <button>Удалить</button>;";
const selfTestRuEnOnly = "const COPY = { title: { ru: 'Камеры', en: 'Cameras' } };";
const selfTestEnglishLabel = "<div>ONVIF Host / IP</div>";
const selfTestStreamSummary = "return `${rec} rec · ${live} live`;";
const selfTestTokenSummary = "parts.push(`token ${token}`);";
if (!cyrillicPattern.test(selfTestRussian)) errors.push("self-test failed: hardcoded Russian pattern is inactive");
if (!ruEnOnlyMapPattern.test(selfTestRuEnOnly)) errors.push("self-test failed: ru/en-only map pattern is inactive");
if (!camerasHardcodedEnglishLabels.some((label) => selfTestEnglishLabel.includes(label))) {
  errors.push("self-test failed: hardcoded English Cameras label pattern is inactive");
}
if (!/\$\{rec\}\s*rec\s*·\s*\$\{live\}\s*live/.test(selfTestStreamSummary)) {
  errors.push("self-test failed: hardcoded stream summary pattern is inactive");
}
if (!/`token\s+\$\{/.test(selfTestTokenSummary)) {
  errors.push("self-test failed: hardcoded token summary pattern is inactive");
}

if (errors.length) {
  console.error(errors.join("\n"));
  process.exit(1);
}

console.log(`i18n validation PASS: ${expected.join(", ")}`);
