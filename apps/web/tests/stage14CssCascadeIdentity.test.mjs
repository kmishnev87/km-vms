import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { CSS_OWNERS, readProductSourceFile } from "./helpers/readProductSources.mjs";

const root = path.resolve(import.meta.dirname, "..");
const baseline = {
  // Reconciled with the already published compact camera list/clip workbench
  // and the visually checked, scoped 0.9.26 archive-rules dialog. Management
  // remains full width; stylesheet identity and import order stay enforced.
  "40-storage-records-shared.css": "7bdea13e15119de6e726ab78ce26d45f9285ca261af781133f83ce1f816216ef",
  // Existing published Settings layout: no Settings styles changed in 0.9.26.
  "20-settings-maintenance.css": "9f5a510c7e61bc71dd11199ba384af0a9123b503f6dcdfce66a96f3b72b6fabc"
};
for (const [logical, files] of Object.entries(CSS_OWNERS)) {
  test(`CSS: ${logical} matches its approved baseline and preserves import order`, () => {
    const css = readProductSourceFile(path.join(root, logical), "utf8");
    // The approved archive-review workbench is scoped to its own component.
    // Preserve the exact earlier cascade outside that explicit addition.
    const protectedCss = css.replace(/\n\/\* Archive review is a fixed workbench:[\s\S]*?\/\* End archive review workbench\. \*\/\n/, "");
    assert.equal(crypto.createHash("sha256").update(protectedCss).digest("hex"), baseline[path.basename(logical)]);
    const globals = fs.readFileSync(path.join(root,"app/globals.css"),"utf8");
    const imports = [...globals.matchAll(/@import\s+"\.\/([^"]+)";/g)].map(match=>match[1]);
    const indices = files.map(file=>imports.indexOf(file.slice(4)));
    assert.ok(indices.every(index=>index>=0));
    assert.deepEqual(indices,indices.slice().sort((a,b)=>a-b));
    for (const file of files) assert.equal(imports.filter(x=>x===file.slice(4)).length,1);
    assert.equal(fs.existsSync(path.join(root,logical)),false);
  });
}
