import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../app/login/page.js", import.meta.url), "utf8");

assert.match(source, /LAST_USERNAME_KEY\s*=\s*"km_vms_last_username"/);
assert.match(source, /const \[username,\s*setUsername\]\s*=\s*useState\(""\)/);
assert.doesNotMatch(source, /useState\("admin"\)/);
assert.match(source, /setUsername\(loadLastUsername\(\)\)/);
assert.match(source, /username:\s*normalizedUsername/);
assert.match(source, /saveLastUsername\(normalizedUsername\)/);
assert.match(source, /if \(!data\?\.access_token\) throw new Error\(text\.noToken\);[\s\S]*saveLastUsername\(normalizedUsername\)/);
assert.doesNotMatch(source, /localStorage\.setItem\([^)]*password/i);

console.log("loginLastUsername contract OK");
