import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const webRoot = resolve(__dirname, "..");
const css = fs.readFileSync(resolve(webRoot, "app/styles/60-responsive-shared.css"), "utf8");

assert.equal(css.includes("@media (max-width: 640px)"), true);
assert.equal(/\.topNavInner\s*\{[^}]*width:\s*max-content;[^}]*min-width:\s*100%;/s.test(css), true);
assert.equal(css.includes("min-width: 100%;"), true);
assert.equal(/\.topNavItems,\s*\.topNavRight\s*\{[^}]*flex:\s*0 0 auto;[^}]*min-width:\s*max-content;/s.test(css), true);
assert.equal(/\.topNavRight\s*\{[^}]*margin-left:\s*0;/s.test(css), true);
assert.equal(css.includes("flex: 0 0 46px;"), true);
assert.equal(css.includes("-webkit-overflow-scrolling: touch;"), true);
