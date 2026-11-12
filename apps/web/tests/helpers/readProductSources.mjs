// Static product contracts span explicit implementation owners after Stage 14.
// This does not execute concatenated JavaScript. Behavioral suites exercise the
// real controllers/core, and ownership tests read individual files directly.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(import.meta.dirname, "../..");
export const CSS_OWNERS = {
  "app/styles/40-storage-records-shared.css": [
    "app/styles/40-01-storage-base.css",
    "app/styles/40-02-page-layout-shared.css",
    "app/styles/40-03-recordings-toolbar.css",
    "app/styles/40-04-chronology-help.css",
    "app/styles/40-05-recordings-viewer.css",
    "app/styles/40-06-controls-modals-shared.css",
    "app/styles/40-07-storage-refinements.css",
    "app/styles/40-08-operation-feedback-shared.css",
    "app/styles/40-09-storage-migration-integrity.css"
  ],
  "app/styles/20-settings-maintenance.css": [
    "app/styles/20-01-settings-forms-users.css",
    "app/styles/20-02-maintenance-backup-update.css",
    "app/styles/20-03-maintenance-restore.css",
    "app/styles/20-04-maintenance-progress-details.css",
    "app/styles/20-05-settings-security-format.css",
    "app/styles/20-06-dashboard-card-tail.css",
    "app/styles/20-07-maintenance-final-layout.css",
    "app/styles/20-08-settings-recording-format.css"
  ]
};
const JS_OWNERS = {
  "app/chronology/page.js": [
    "lib/chronologyWorkspaceLayout.js",
    "lib/useChronologySources.js",
    "lib/useChronologyClock.js",
    "lib/chronologyPlaybackCoordinator.js",
    "lib/useChronologyPlaybackCoordinator.js"
  ],
  "app/storage/page.js": [
    "lib/storagePagePresentation.js",
    "lib/useStorageMigrationController.js",
    "lib/useStorageIntegrityController.js",
    "lib/useArchiveIntegrityReviewController.js",
    "components/storage/ArchiveIntegrityDialog.js",
    "lib/useStorageActivationController.js"
  ],
  "app/settings/page.js": [
    "lib/useSettingsFormLoader.js",
    "lib/useSettingsUsersController.js"
  ]
};

export function readProductSourceFile(filename, encoding) {
  const absolute = filename instanceof URL ? fileURLToPath(filename) : path.resolve(filename);
  const relative = path.relative(root, absolute).split(path.sep).join("/");
  let content;
  if (CSS_OWNERS[relative]) content = CSS_OWNERS[relative].map(file => fs.readFileSync(path.join(root,file),"utf8")).join("");
  else if (JS_OWNERS[relative]) content = [relative,...JS_OWNERS[relative]].map(file=>fs.readFileSync(path.join(root,file),"utf8")).join("\n");
  else return fs.readFileSync(filename, encoding);
  return encoding ? content : Buffer.from(content);
}
