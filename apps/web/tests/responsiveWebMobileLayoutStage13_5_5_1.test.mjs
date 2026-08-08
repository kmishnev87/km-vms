import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const page = fs.readFileSync(resolve(__dirname, "../app/recordings/page.js"), "utf8");
const baseCss = fs.readFileSync(resolve(__dirname, "../app/styles/40-storage-records-shared.css"), "utf8");
const responsiveCss = fs.readFileSync(resolve(__dirname, "../app/styles/60-responsive-shared.css"), "utf8");

assert.match(page, /className="table recordingsTable"/);
assert.match(page, /className="recordingsMobileControls"/);
assert.match(page, /data-label=\{t\.camera\}/);
assert.match(page, /data-label=\{t\.file\}/);
assert.match(page, /data-label=\{t\.createdAt\}/);
assert.match(page, /data-label=\{t\.size\}/);
assert.match(page, /data-label=\{t\.actions\}/);

assert.match(baseCss, /\.recordingsMobileControls\s*\{\s*display:\s*none;/s);
assert.match(responsiveCss, /@media \(max-width:\s*640px\)[\s\S]*\.recordingsMobileControls\s*\{\s*display:\s*flex;/);
assert.match(responsiveCss, /\.recordingsTable\s*\{[^}]*display:\s*block;[^}]*min-width:\s*0;/s);
assert.match(responsiveCss, /\.recordingsTable tbody tr\s*\{[^}]*display:\s*grid;[^}]*border-radius:\s*14px;/s);
assert.match(responsiveCss, /\.recordingsTable \.recordingsIconButton\s*\{[^}]*width:\s*40px;[^}]*height:\s*40px;/s);
assert.equal(/@media \(max-width:\s*640px\)[\s\S]*\.recordingsTable\s*\{[^}]*min-width:\s*760px;/s.test(responsiveCss), false);
