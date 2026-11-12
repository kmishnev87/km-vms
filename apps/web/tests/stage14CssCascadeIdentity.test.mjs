import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { CSS_OWNERS, readProductSourceFile } from "./helpers/readProductSources.mjs";

const root = path.resolve(import.meta.dirname, "..");
const baseline = {
  // Approved left-column roots, shared clips, one-line deleted labels and fixed history workbench.
  // Management remains full width; stylesheet identity and import order stay enforced.
  "40-storage-records-shared.css": "29d72a234a3da9958d35929ec8645eb996d1c14d9d84c69a85118adddec64133",
  // Approved compact apply action and decorative Settings clipping; forms stay 920px.
  "20-settings-maintenance.css": "28c256682087cac092a9d73ed805ac0d04dfc979fe00d4ab7f604a33c65f2d47"
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
