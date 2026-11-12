import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { CSS_OWNERS, readProductSourceFile } from "./helpers/readProductSources.mjs";

const root = path.resolve(import.meta.dirname, "..");
const baseline = {
  "40-storage-records-shared.css": "cac43eb452602b35b4b134aad66e3b79ca391e21554982abe83d846f2c390d82",
  // Approved maintenance result/header repair advances only this stylesheet baseline.
  "20-settings-maintenance.css": "7123d700d919e3114ceecc9ebfa5022f821c2f9ff52ae4a74a5437681cc620db"
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
