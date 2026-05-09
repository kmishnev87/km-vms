import assert from "node:assert/strict";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const __dirname = dirname(fileURLToPath(import.meta.url));
const recordsPage = fs.readFileSync(resolve(__dirname, "../app/recordings/page.js"), "utf8");
const archivePlayer = fs.readFileSync(resolve(__dirname, "../components/ArchiveTilePlayer.js"), "utf8");

assert.equal(recordsPage.includes("function isDirectPlaybackUnsupported"), false);
assert.equal(archivePlayer.includes("isUnsupportedDirectPlayback"), false);
assert.equal(recordsPage.includes("mkv\") || value.includes(\"matroska"), false);
assert.equal(recordsPage.includes("await buildRecordingStreamUrl(item);"), true);
assert.equal(archivePlayer.includes("setStatus(\"unsupported\");"), true);

for (const source of [recordsPage, archivePlayer]) {
  assert.equal(source.includes("????????"), false);
  assert.equal(source.includes("Скачать запись") || source.includes("\\u0421\\u043a\\u0430\\u0447\\u0430\\u0442\\u044c"), true);
}

assert.equal(recordsPage.includes("Файл отсутствует / требуется проверка архива") || recordsPage.includes("\\u0424\\u0430\\u0439\\u043b"), true);
assert.equal(recordsPage.includes("disabled={!isRecordingAvailable(item)}"), true);
