import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (file) => fs.readFileSync(resolve(__dirname, "..", file), "utf8");

const page = read("app/storage/page.js");
const i18n = read("lib/i18n.js");
const css = read("app/styles/40-storage-records-shared.css");

assert.match(page, /\/storage\/archive-roots\/discovery/, "archive root add flow must use NAS/root discovery");
assert.doesNotMatch(page, /\/storage\/archive-roots\/preview|\/storage\/archive-roots\/validate/, "archive root add flow must not expose separate preview/validate actions");
assert.match(page, /archiveRootChoiceId/, "archive root volume/root selection must be controlled UI state");
assert.match(page, /archiveRootFolderName/, "archive root folder name must be controlled UI state");
assert.doesNotMatch(page, /archiveRootManualPath|\brootPath\b|storageManualOption/, "archive root add flow must not expose manual path fallback");
assert.match(page, /archiveRootDialogText/, "archive root add errors must be mapped to user-facing dialog text");

assert.match(page, /migrationTargetRootId/, "migration target must be explicitly selected by the operator");
assert.match(page, /target_root_id: migrationTargetRootId/, "migration preview/apply must send the selected target_root_id");
assert.doesNotMatch(page, /target_root_id: null/, "migration preview must not silently ask backend to choose target");

const operations = page.slice(
  page.indexOf("<Section title={copy.archiveOperations}"),
  page.indexOf("<Section title={copy.archiveRoots}")
);
assert.match(operations, /title=\{copy\.retentionRules\}/, "retention policy/status remains visible");
const retentionRow = operations.slice(operations.indexOf("title={copy.retentionRules}"), operations.indexOf("title={copy.autoFreeSpace}"));
assert.doesNotMatch(retentionRow.split("<details")[0], /retentionPlanShort|retentionConfirmed|retentionDeleteShort/, "manual retention controls must not be primary operations");

assert.match(operations, /title=\{copy\.archiveProblems\}/, "archive problems must be a visible product row");
assert.match(operations, /visibleProblemCategories/, "problem categories must be visible, not only hidden diagnostics");
assert.match(operations, /visibleProblemSamples/, "problem samples must be visible with redacted display fields");
assert.match(operations, /problemActionStatusText\(item, copy\)/, "problem categories must explain the current action boundary");
assert.match(operations, /item\.reason_no_action_available/, "problem viewer must show why cleanup is not performed in this stage");
assert.doesNotMatch(operations, /status=\{statusLabel\(reconciliationScenario\.status, language\)\}/, "archive problems must not use generic preview-ready status");
assert.doesNotMatch(operations, /status=\{statusLabel\(retentionScenario\.status, language\)\}/, "retention must not use generic preview-ready status");
assert.doesNotMatch(operations, /status=\{statusLabel\(migrationScenario\.status, language\)\}/, "migration must not use generic preview-ready status");

assert.match(page, /rootProblemLabel\(root, copy, language\)/);
assert.match(page, /rootHasProblems\(root\) \? copy\.yes : copy\.no/, "root problem column must show short yes/no badges");
assert.match(page, /showRootProblems\(root\)/, "root problem yes badge must open a details dialog");
assert.doesNotMatch(page, /if \(root\.is_available === true\) return copy\.available/, "problem column must not show green availability as no-problem state");
assert.match(page, /storageOpsRootActivateButton/, "archive root activation must live on the visible root row");
assert.match(page, /<CheckIcon \/>/, "archive root activation must use a compact check icon action");
assert.match(page, /storageOpsRootDeleteButton/, "inactive archive roots must expose a compact delete action");
assert.match(page, /deleteRoot\(root\.id\)/, "archive root delete confirmation must call the delete flow");
assert.match(page, /archiveRootStateText\(root, copy\)/, "root row must show an explicit active/inactive state");
assert.doesNotMatch(page, /<span>\{copy\.active\}<\/span>/, "action column must not duplicate active state text above the check icon");
const rootActionFlow = page.slice(page.indexOf("function requestActivateRoot"), page.indexOf("async function runRetentionPreview"));
assert.doesNotMatch(rootActionFlow, /window\.confirm/, "storage root destructive/switch actions must use product dialogs, not browser confirm");
assert.doesNotMatch(page, /storageOpsRootManageList|storageOpsRootManageRow/, "duplicate archive root management list must not remain under the add section");
assert.match(page, /<summary>\{copy\.addArchiveRoot\}<\/summary>/, "advanced section must be only for adding a root");
assert.doesNotMatch(page, /<table className="storageOpsTable storageOpsTable-compact">[\s\S]*copy\.archiveLocation/, "archive root management must not render the old cramped table");

const archiveSummary = page.slice(page.indexOf("storageOpsArchiveSummary"), page.indexOf("</Section>", page.indexOf("storageOpsArchiveSummary")));
assert.doesNotMatch(archiveSummary, /copy\.availability/, "primary archive summary must not have a separate availability row");

assert.match(i18n, /segments: "Файлы"/, "RU storage label must be Files, not Segments");
assert.match(i18n, /segments: "Files"/, "EN storage label must be Files");
assert.doesNotMatch(i18n, /segments: "Сегменты"/, "RU UI must not expose Segments on storage page");
assert.match(i18n, /noReasons: "Нет"/, "empty root problem state must be short");
assert.doesNotMatch(i18n, /Нет активных причин или блокеров/, "empty problem state must not expose blocker jargon");
assert.match(i18n, /может перезапустить сервисы записи/, "root switch copy must warn about service restart");
assert.match(i18n, /Вы точно желаете удалить пустой корень архива/, "empty root delete confirmation must be explicit");
assert.match(i18n, /Вы точно желаете удалить корень архива и все его записи/, "non-empty root delete confirmation must mention recordings");
assert.match(i18n, /archiveProblemsFound: "Найдено: \{count\}"/);
assert.match(i18n, /problemManualReview: "Требуется ручная проверка"/);
assert.match(i18n, /retentionAutomaticStatus: "Применяется автоматически"/);

assert.match(css, /storageOpsRootForm-product/, "root selection form must have compact product styling");
assert.match(css, /storageOpsRootActionsCell/, "root actions must be a compact icon cell");
assert.match(css, /storageOpsBadgeButton/, "problem yes badge must be clickable without becoming a large button");
assert.match(css, /storageOpsProblemList/, "problem categories must have dedicated compact styling");
