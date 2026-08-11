import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = fileURLToPath(new URL("../../", import.meta.url));

export const I18N_SOURCE_FILES = Object.freeze([
  "lib/i18n.js",
  "lib/i18n/dictionaries.js",
  "lib/i18n/ru.js",
  "lib/i18n/en.js",
  "lib/i18n/zhCN.js",
  "lib/i18n/legacyTextTranslations.js",
]);

function assertCompleteModuleInventory() {
  const expected = I18N_SOURCE_FILES
    .filter((relativePath) => relativePath.startsWith("lib/i18n/"))
    .map((relativePath) => path.basename(relativePath))
    .sort();
  const actual = fs
    .readdirSync(path.join(webRoot, "lib/i18n"), { withFileTypes: true })
    .filter((entry) => entry.isFile() && entry.name.endsWith(".js"))
    .map((entry) => entry.name)
    .sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`aggregate i18n source inventory mismatch: expected ${expected.join(", ")}; actual ${actual.join(", ")}`);
  }
}

export function readI18nSource() {
  assertCompleteModuleInventory();
  return I18N_SOURCE_FILES
    .map((relativePath) => fs.readFileSync(path.join(webRoot, relativePath), "utf8"))
    .join("\n");
}
