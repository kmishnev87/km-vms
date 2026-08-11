import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "../..");

export const settingsMaintenanceSourcePaths = Object.freeze([
  "app/settings/page.js",
  "lib/settingsMaintenanceController.js",
  "components/SettingsMaintenanceSurface.js",
]);

function read(relative) {
  return fs.readFileSync(resolve(webRoot, relative), "utf8");
}

export function readSettingsMaintenanceSourceFiles() {
  return {
    pageSource: read(settingsMaintenanceSourcePaths[0]),
    controllerSource: read(settingsMaintenanceSourcePaths[1]),
    surfaceSource: read(settingsMaintenanceSourcePaths[2]),
  };
}

export function readSettingsMaintenanceSources() {
  const sources = readSettingsMaintenanceSourceFiles();
  return settingsMaintenanceSourcePaths
    .map((relative, index) => {
      const source = index === 0
        ? sources.pageSource
        : index === 1
          ? sources.controllerSource
          : sources.surfaceSource;
      return `\n/* KM VMS semantic source: ${relative} */\n${source}`;
    })
    .join("\n");
}
