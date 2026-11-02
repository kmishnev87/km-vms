import assert from "node:assert/strict";
import fs from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const component = fs.readFileSync(resolve(here, "../components/AuthenticatedPreviewImage.js"), "utf8");
const page = fs.readFileSync(resolve(here, "../app/cameras/page.js"), "utf8");
const editor = fs.readFileSync(resolve(here, "../components/cameras/CameraEditor.js"), "utf8");

assert.equal(component.includes('import { apiFetchBlob } from "../lib/api"'), true);
assert.equal(component.includes("apiFetchBlob(src)"), true);
assert.equal(component.includes("URL.createObjectURL(blob)"), true);
assert.equal(component.includes("URL.revokeObjectURL(createdUrl)"), true);
assert.equal(component.includes("}, [src, cache]);"), true);
assert.equal(component.includes("cache.current.size > 8"), true);
assert.equal(editor.includes("cache={imageCache}"), true);
assert.equal(editor.includes("imageCache.current.clear()"), true);
assert.equal(page.includes("<AuthenticatedPreviewImage"), true);
assert.equal(editor.includes("<AuthenticatedPreviewImage"), true);
assert.equal(editor.includes("<img"), false);
assert.equal(page.includes("<img src={camera.preview_url}"), false);
assert.equal(page.includes("<img src={`${testResult.preview_url}"), false);

// A dialog-local cache may reuse an already authorized Blob, not bypass the first fetch.
function imageHarness(cache = null) {
  const states = [], effects = [], calls = [], revoked = [];
  let index = 0, sequence = 0;
  const scope = {
    useState(initial) { const at = index++; if (!(at in states)) states[at] = initial; return [states[at], (value) => { states[at] = value; }]; },
    useEffect(effect, dependencies) {
      const at = index++;
      if (!effects[at] || dependencies.some((value, i) => value !== effects[at].dependencies[i])) {
        effects[at]?.cleanup?.(); effects[at] = { dependencies, cleanup: effect() };
      }
    },
    apiFetchBlob: async (src) => { calls.push(src); return { blob: src }; },
    URL: { createObjectURL: () => `blob:synthetic-${++sequence}`, revokeObjectURL: (url) => revoked.push(url) },
  };
  const source = component.replace(/^import .*;$/gm, "").replace("export default function", "function")
    .replace('return <img src={objectUrl} alt={alt} className={className} />;', 'return { image: objectUrl };');
  vm.runInNewContext(`${source}\nthis.renderImage=AuthenticatedPreviewImage;`, scope);
  return { calls, revoked, render(src) { index=0; return scope.renderImage({ src, cache, fallback: "placeholder" }); }, unmount() { effects.forEach((effect) => effect?.cleanup?.()); } };
}
const flush = async () => { for(let i=0;i<8;i++) await Promise.resolve(); };
const memory = { current: new Map() }, cached = imageHarness(memory);
assert.equal(cached.render("/private/a.jpg"), "placeholder");
await flush();
const first = cached.render("/private/a.jpg").image;
cached.render("/private/b.jpg"); await flush();
assert.notEqual(cached.render("/private/b.jpg").image, first);
assert.equal(cached.render("/private/a.jpg").image, first, "cached lens image must render synchronously without a placeholder");
assert.deepEqual(cached.calls, ["/private/a.jpg", "/private/b.jpg"]);
for (let i=0;i<10;i++) { cached.render(`/private/extra-${i}.jpg`); await flush(); }
assert.equal(memory.current.size, 8);
assert(cached.revoked.length > 0, "evicted blobs must be revoked");
const legacy = imageHarness();
legacy.render("/private/uncached.jpg"); await flush();
legacy.unmount();
assert.equal(legacy.revoked.length, 1, "ordinary list images retain their existing cleanup contract");
console.log("Authenticated preview first-fetch, bounded memory cache and cleanup PASS");
