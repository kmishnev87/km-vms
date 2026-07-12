import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");

const storagePage = read("app/storage/page.js");
const recordingsPage = read("app/recordings/page.js");
const chronologyPage = read("app/chronology/page.js");
const archiveTilePlayer = read("components/ArchiveTilePlayer.js");
const api = read("lib/api.js");
const i18n = read("lib/i18n.js");

const rootsSection = storagePage.slice(
  storagePage.indexOf("<Section title={copy.archiveRoots}"),
  storagePage.indexOf("<Section title={copy.recentOperations}", storagePage.indexOf("<Section title={copy.archiveRoots}"))
);
const addRootDetails = storagePage.slice(
  storagePage.indexOf("<details className=\"storageOpsDetails storageOpsAdvancedRoot\""),
  storagePage.indexOf("</details>", storagePage.indexOf("<details className=\"storageOpsDetails storageOpsAdvancedRoot\""))
);

assert.doesNotMatch(storagePage, /showArchiveRootActivation/, "activation progress must not be duplicated inline");
assert.match(storagePage, /activationOperationId/, "activation modal acknowledgement must be bound to the backend operation id");
assert.match(storagePage, /setArchiveRootDialog\(/, "activation progress must use the application modal");
assert.match(storagePage, /setInterval\(\(\) => loadStatus\(\{ silent: true \}\), 1500\)/, "activation progress must poll backend status");
assert.doesNotMatch(addRootDetails, /copy\.rootSwitched|activationProgressTitle|showArchiveRootActivation/, "switch progress must not live inside collapsed add-root details");
assert.doesNotMatch(storagePage, /setArchiveRootMessage/, "root operation feedback must not use persistent inline messages");

assert.match(api, /segment_id: pathOrItem\.segment_id/, "recording media token payload must include segment id");
assert.match(api, /archive_root_id: pathOrItem\.archive_root_id/, "recording media token payload must include archive root id");
assert.match(api, /recording_ref: pathOrItem\.recording_ref/, "recording media token payload must include stable recording ref");
assert.match(recordingsPage, /recordingIdentityPayload\(item\)/, "recordings actions must use stable identity payload");
assert.match(recordingsPage, /recordingIdentityQuery\(item\)/, "recordings stream/download/delete URLs must use stable identity query");
assert.match(recordingsPage, /availability_status === "root_unavailable"/, "records page must distinguish unavailable root from missing file");
assert.match(recordingsPage, /rootUnavailable/, "records page must render a user-facing unavailable-root label");

assert.match(chronologyPage, /segmentId/, "chronology playback state must keep segment identity");
assert.match(chronologyPage, /archiveRootId/, "chronology playback state must keep archive-root identity");
assert.match(chronologyPage, /playbackRef/, "chronology playback state must keep playback ref");
assert.match(api, /playback\?\.segment_id \|\| playback\?\.segmentId/, "chronology media token helper must accept frontend camelCase segment identity");
assert.match(api, /playback\?\.archive_root_id \|\| playback\?\.archiveRootId/, "chronology media token helper must accept frontend camelCase root identity");
assert.match(api, /playback\?\.playback_ref \|\| playback\?\.playbackRef/, "chronology media token helper must accept frontend camelCase playback ref");
assert.match(archiveTilePlayer, /segment_id/, "archive tile player media URL must use segment id");
assert.match(archiveTilePlayer, /archive_root_id/, "archive tile player media URL must use archive root id");
assert.match(archiveTilePlayer, /playback_ref/, "archive tile player media URL must use playback ref");
assert.doesNotMatch(archiveTilePlayer, /camera_id=.*rel_path=/, "archive tile player must not rely on camera+rel_path URL contract");

assert.match(i18n, /activationProgressTitle/, "activation progress text must be localized");
assert.match(i18n, /activationStepCheckAccess/, "activation progress must include archive access check step");
