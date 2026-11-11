import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

for (const workspace of ["live", "chronology"]) {
  const page = fs.readFileSync(new URL(`../app/${workspace}/page.js`, import.meta.url), "utf8");
  test(`${workspace}: initial backend layout finishes before toolbar or sidebar can overwrite it`, () => {
    assert.match(page, /\[workspaceLayoutReady, setWorkspaceLayoutReady\] = useState\(false\)/);
    const hydration = page.slice(page.indexOf("async function loadWorkspaceLayout"), page.indexOf("const cameraMap"));
    assert.match(hydration, /finally \{\s*if \(isWorkspaceCurrent\(ownerSession, generation\)\) \{\s*hydratedRef.current = true;\s*setWorkspaceLayoutReady\(true\)/);
    assert.match(hydration, /setWorkspaceLayoutReady\(false\)/);
    assert.match(page, /function addTile\([^)]*\) \{\s*if \(!hydratedRef.current\) return/);
    assert.match(page, /function reorderSidebarCamera\([^)]*\) \{\s*if \(!hydratedRef.current\) return/);
    assert.equal((page.match(/disabled=\{!workspaceLayoutReady \|\| !/g) || []).length, 4);
  });
}
