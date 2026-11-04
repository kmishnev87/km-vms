import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { CSS_OWNERS, readProductSourceFile } from "./helpers/readProductSources.mjs";

const root = path.resolve(import.meta.dirname, "..");
const baseline = {
  "40-storage-records-shared.css": "cac43eb452602b35b4b134aad66e3b79ca391e21554982abe83d846f2c390d82",
  "20-settings-maintenance.css": "595f8108c380d958ad11e27e24cb4a8fa0531f8e9343ccdd43e23fc616cff1de"
};
for (const [logical, files] of Object.entries(CSS_OWNERS)) {
  test(`Stage 14 CSS: ${logical} preserves every rule byte and cascade position`, () => {
    const css = readProductSourceFile(path.join(root, logical), "utf8");
    assert.equal(crypto.createHash("sha256").update(css).digest("hex"), baseline[path.basename(logical)]);
    const globals = fs.readFileSync(path.join(root,"app/globals.css"),"utf8");
    const imports = [...globals.matchAll(/@import\s+"\.\/([^"]+)";/g)].map(match=>match[1]);
    const indices = files.map(file=>imports.indexOf(file.slice(4)));
    assert.ok(indices.every(index=>index>=0));
    assert.deepEqual(indices,indices.slice().sort((a,b)=>a-b));
    for (const file of files) assert.equal(imports.filter(x=>x===file.slice(4)).length,1);
    assert.equal(fs.existsSync(path.join(root,logical)),false);
  });
}
