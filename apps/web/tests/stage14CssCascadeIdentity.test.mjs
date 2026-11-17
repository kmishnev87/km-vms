import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { CSS_OWNERS, readProductSourceFile } from "./helpers/readProductSources.mjs";

const root = path.resolve(import.meta.dirname, "..");
const baseline = {
  // Reconciled with the already published compact camera list/clip workbench
  // and the scoped, owner-requested compact archive-rules follow-up. Number
  // fields have no asymmetric native spinner; apply follows the values at
  // the right edge (Screenshot_27). Management stays full width.
  // Stylesheet identity and import order remain enforced.
  "40-storage-records-shared.css": "11ca83ba9ea3d59ee00ec05cf9d6632b3aead76733eb6a60d14c655604ebf007",
  // Owner-requested online-mode help: preserve paragraph breaks in this
  // tooltip only. Existing row dimensions, other tips and import order stay.
  "20-settings-maintenance.css": "e9f61a2e6d18c4a98e70741d47cc96ac2cfe658a144c6184fef795b43de87587"
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
