import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const component = fs.readFileSync(resolve(here, "../components/AuthenticatedPreviewImage.js"), "utf8");
const page = fs.readFileSync(resolve(here, "../app/cameras/page.js"), "utf8");

assert.equal(component.includes('import { apiFetchBlob } from "../lib/api"'), true);
assert.equal(component.includes("apiFetchBlob(src)"), true);
assert.equal(component.includes("URL.createObjectURL(blob)"), true);
assert.equal(component.includes("URL.revokeObjectURL(createdUrl)"), true);
assert.equal(component.includes("}, [src]);"), true);
assert.equal(page.includes("<AuthenticatedPreviewImage"), true);
assert.equal(page.includes("<img src={camera.preview_url}"), false);
assert.equal(page.includes("<img src={`${testResult.preview_url}"), false);
