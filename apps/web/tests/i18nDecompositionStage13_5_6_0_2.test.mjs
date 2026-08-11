import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { DICTIONARIES, TEXT_TRANSLATIONS } from "../lib/i18n/dictionaries.js";
import { I18N_SOURCE_FILES, readI18nSource } from "./helpers/readI18nSources.mjs";

const webRoot = fileURLToPath(new URL("../", import.meta.url));
const entrypoint = fs.readFileSync(path.join(webRoot, "lib/i18n.js"), "utf8");
const aggregateSource = readI18nSource();

assert.deepEqual(Object.keys(DICTIONARIES), ["ru", "en", "zh-CN"]);
assert.deepEqual(Object.keys(TEXT_TRANSLATIONS), ["en", "zh-CN"]);
assert.equal(I18N_SOURCE_FILES.length, 6);
assert.match(entrypoint, /import \{ DICTIONARIES, TEXT_TRANSLATIONS \} from "\.\/i18n\/dictionaries\.js";/);
assert.match(entrypoint, /export \{ DICTIONARIES \};/);
assert.doesNotMatch(entrypoint, /export const DICTIONARIES\s*=\s*\{/);
assert.doesNotMatch(entrypoint, /const TEXT_TRANSLATIONS\s*=\s*\{/);
assert.match(aggregateSource, /export const ruDictionary\s*=\s*\{/);
assert.match(aggregateSource, /ruDictionary\.setup\s*=\s*\{/);
assert.match(aggregateSource, /export const enDictionary\s*=\s*\{/);
assert.match(aggregateSource, /export const zhCNDictionary\s*=\s*\{/);
assert.match(aggregateSource, /export const TEXT_TRANSLATIONS\s*=\s*\{/);

for (const relativePath of I18N_SOURCE_FILES.filter((item) => item.startsWith("lib/i18n/"))) {
  const source = fs.readFileSync(path.join(webRoot, relativePath), "utf8");
  assert.doesNotMatch(source, /from "react"|createContext|useContext|useEffect|useMemo|useState/);
}

console.log("Stage 13.5.6.0.2 i18n decomposition contract PASS");
