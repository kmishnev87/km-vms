import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webRoot = fileURLToPath(new URL("../../", import.meta.url));

export const SETTINGS_PAGE_HELPER_SOURCE_FILES = Object.freeze([
  "lib/settingsPageHelpers.js",
  "lib/settingsPageSharedHelpers.js",
  "lib/settingsUpdateApplyHelpers.js",
]);

export function readSettingsPageHelperSource() {
  return SETTINGS_PAGE_HELPER_SOURCE_FILES
    .map((relativePath) => fs.readFileSync(path.join(webRoot, relativePath), "utf8"))
    .join("\n");
}
