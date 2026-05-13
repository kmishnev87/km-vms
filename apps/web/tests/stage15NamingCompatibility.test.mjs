import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const read = (relative) => fs.readFileSync(resolve(root, relative), "utf8");

const routePermissions = await import(pathToFileURL(resolve(root, "lib/routePermissions.js")));

const layout = read("components/Layout.js");
const dashboard = read("app/page.js");
const chronologyPage = read("app/chronology/page.js");

assert.equal(fs.existsSync(resolve(root, "app/timeline/page.js")), false);
assert.equal(fs.existsSync(resolve(root, "app/chronology2/page.js")), false);
assert.equal(routePermissions.FRONTEND_ROUTE_ACCESS["/timeline"], undefined);
assert.equal(routePermissions.FRONTEND_ROUTE_ACCESS["/chronology2"], undefined);

const owner = { permissions: ["view_timeline"] };
const viewer = { permissions: ["view_live"] };
assert.equal(routePermissions.canUserAccessRoute(owner, "/chronology"), true);
assert.equal(routePermissions.canUserAccessRoute(owner, "/timeline"), false);
assert.equal(routePermissions.canUserAccessRoute(owner, "/chronology2"), false);
assert.equal(routePermissions.canUserAccessRoute(viewer, "/timeline"), false);
assert.equal(routePermissions.canUserAccessRoute(viewer, "/chronology2"), false);

for (const source of [layout, dashboard]) {
  assert.equal(source.includes('href: "/timeline"'), false);
  assert.equal(source.includes('href: "/chronology2"'), false);
  assert.equal(source.includes('href="/timeline"'), false);
  assert.equal(source.includes('href="/chronology2"'), false);
  assert.equal(source.includes('href: "/chronology"'), true);
}

const routePermissionsSource = read("lib/routePermissions.js");
assert.equal(routePermissionsSource.includes('"/timeline"'), false);
assert.equal(routePermissionsSource.includes('"/chronology2"'), false);
assert.equal(routePermissionsSource.includes("compatibilityAlias"), false);
assert.equal(routePermissionsSource.includes("canonicalRoute"), false);
assert.equal(routePermissionsSource.includes('permission: "view_timeline"'), true);

for (const source of [layout, dashboard, chronologyPage]) {
  assert.equal(/Chronology\s*2/i.test(source), false);
  assert.equal(/Хронология\s*2/i.test(source), false);
}
