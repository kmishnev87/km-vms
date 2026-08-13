import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(here, "..");
const controller = fs.readFileSync(
  resolve(webRoot, "lib/settingsMaintenanceController.js"),
  "utf8",
);
const surface = fs.readFileSync(
  resolve(webRoot, "components/SettingsMaintenanceSurface.js"),
  "utf8",
);

assert.match(controller, /const updatePeerCheckUnavailable = Boolean\(/);
assert.match(controller, /updatePeerCheckUnavailable,\s*maintenanceBackupOverview/);
assert.match(
  surface,
  /updateApplyErrors,\s*updatePeerCheckUnavailable,\s*maintenanceBackupOverview/,
);
assert.match(surface, /\{updatePeerCheckUnavailable \? \(/);
assert.doesNotMatch(surface, /updateTransportErrors/);

console.log("settings maintenance emergency V5 contract: PASS");
