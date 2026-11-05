import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { createSerializedLayoutWriter } from "../lib/serializedLayoutWriter.js";
const turn = () => new Promise(resolve => setImmediate(resolve));
const noop = () => {};

// A new page mount must use the same queue; stale pending B cannot overtake C.
{
  const writes = [], requests = [], acknowledgements = [];
  const write = payload => new Promise((resolve, reject) => { writes.push(payload); requests.push({resolve, reject}); });
  const options = {scope: "repeat-live", ownerSession: "dummy-session-A"};
  const old = createSerializedLayoutWriter(write, options);
  old.save("A", noop, noop); old.save("B", noop, noop); old.dispose();
  const current = createSerializedLayoutWriter(write, options);
  current.save("C", value => acknowledgements.push(value), noop);
  assert.deepEqual(writes, ["A"]);
  requests[0].resolve("A"); await turn();
  assert.deepEqual(writes, ["A", "C"]);
  requests[1].resolve("C"); await current.flush();
  assert.deepEqual(acknowledgements, ["C"]);
  current.dispose();
}

// Hydration waits for the previous page's last queued write instead of reading stale state.
{
  const writes = [], complete = [];
  const write = payload => new Promise(resolve => { writes.push(payload); complete.push(resolve); });
  const options = {scope: "repeat-chronology", ownerSession: "dummy-session-A"};
  const old = createSerializedLayoutWriter(write, options);
  old.save("A", noop, noop); old.save("B", noop, noop); old.dispose();
  const current = createSerializedLayoutWriter(write, options);
  let loaded = false;
  const hydration = current.flush().then(() => { loaded = true; });
  complete[0](); await turn();
  assert.deepEqual(writes, ["A", "B"]); assert.equal(loaded, false);
  complete[1](); await hydration; assert.equal(loaded, true);
  current.dispose();
}

// Failed requests release the shared queue and different accounts never share it.
{
  const requests = [], writes = [];
  let session = "A";
  const write = payload => new Promise((resolve, reject) => { writes.push(payload); requests.push({resolve, reject}); });
  const a = createSerializedLayoutWriter(write, {scope:"repeat-errors",ownerSession:"A"});
  a.save("A1", noop, noop, () => session === "A");
  a.save("A2", noop, noop, () => session === "A");
  session = "B";
  const b = createSerializedLayoutWriter(write, {scope:"repeat-errors",ownerSession:"B"});
  b.save("B1", noop, noop, () => session === "B");
  assert.deepEqual(writes, ["A1", "B1"]);
  requests[0].reject(new Error("dummy network failure")); requests[1].resolve();
  await Promise.all([a.flush(), b.flush()]);
  assert.equal(writes.includes("A2"), false); a.dispose(); b.dispose();
}

for (const name of ["live", "chronology"]) {
  const source = fs.readFileSync(new URL(`../app/${name}/page.js`, import.meta.url), "utf8");
  const guardStart = source.indexOf("  function isWorkspaceCurrent(");
  const guardEnd = source.indexOf("  async function loadCameras(", guardStart);
  const guard = source.slice(guardStart, guardEnd);
  const marker = source.indexOf("    if (!hydratedRef.current) return;");
  const effectStart = source.lastIndexOf("  useEffect(() => {", marker);
  const effectEnd = source.indexOf("\n  }, [tiles, sidebarCameraOrder", marker);
  const effect = source.slice(effectStart + "  useEffect(() => {".length, effectEnd);
  assert.ok(effectStart >= 0 && effectEnd > effectStart);

  for (const change of ["session", "generation", "none", "navigation", "navigation-after-signin"]) {
    let token = "A", callback;
    const writes = [];
    const context = {workspaceSession:"A",getAuthToken:()=>token,workspaceGenerationRef:{current:1},
      hydratedRef:{current:true},backendReadyRef:{current:true},cacheOwnerRef:{current:1},
      layoutWriterRef:{current:{invalidate:noop,save:(...args)=>writes.push(args)}},
      tiles:[{cameraId:"1"}],sidebarCameraOrder:[],lastBackendLayoutPayloadRef:{current:""},
      saveTiles:noop,saveTimerRef:{current:null},pendingLayoutSaveRef:{current:null},clearTimeout:noop,setTimeout:fn=>{callback=fn;return 1;},
      backendPayload:(tiles,sidebarCameraOrder)=>({tiles,sidebarCameraOrder}),sanitizeSidebarCameraOrder:value=>value,
      setLastPersistedSidebarCameraOrder:noop,setSidebarCameraOrder:noop,showWorkspaceError:noop,TEXT:{loadError:"dummy"}};
    vm.runInNewContext(guard + "\n(function () {" + effect + "\n})();", context);
    if (change === "session" || change === "navigation-after-signin") token = "B";
    if (change === "generation") context.workspaceGenerationRef.current += 1;
    if (change.startsWith("navigation")) {
      const initialization = source.indexOf("    const generation = ++workspaceGenerationRef.current;");
      const initializationEnd = source.indexOf("\n  }, [workspaceSession]);", initialization);
      const cleanup = source.slice(initialization, initializationEnd).match(/return \(\) => \{([\s\S]*?)\n    \};/)?.[1];
      assert.ok(cleanup);
      Object.assign(context, {writer:{dispose:noop},clearInterval:noop,timer:null,
        rangesInFlightRef:{current:null},rangesRequestIdRef:{current:0}});
      vm.runInNewContext("(function () {" + cleanup + "\n})();", context);
    } else callback();
    assert.equal(writes.length, ["none", "navigation"].includes(change) ? 1 : 0, `${name}: stale edit must not become another session's write; navigation keeps the last edit`);
    if (writes.length) {
      context.workspaceGenerationRef.current += 1;
      assert.equal(writes[0][3](), true, "already queued write survives navigation within the same session");
      token = "B"; assert.equal(writes[0][3](), false);
    }
  }

  // Late hydration of account A must not initialize account B or enable its writer.
  let token = "A";
  const pending = [], mutations = [];
  const loadStart = source.indexOf("  async function loadWorkspaceLayout(");
  const loadEnd = source.indexOf("\n  useEffect(", loadStart);
  const context = {WORKSPACE_KEY:name,getAuthToken:()=>token,workspaceGenerationRef:{current:1},
    layoutWriterRef:{current:{flush:async()=>{}}},apiFetch:()=>new Promise(resolve=>pending.push(resolve)),
    hydratedRef:{current:false},backendReadyRef:{current:false},cacheOwnerRef:{current:null},
    setTiles:()=>mutations.push("tiles"),setCurrentUser:()=>mutations.push("user"),showWorkspaceError:()=>mutations.push("error")};
  vm.runInNewContext(guard + source.slice(loadStart,loadEnd) + "\nthis.load = loadWorkspaceLayout;", context);
  const loading = context.load("A", 1); await turn();
  assert.equal(pending.length, 2);
  token = "B"; pending[0]({id:1}); pending[1]({tiles:[{cameraId:"1"}]});
  await loading;
  assert.deepEqual(mutations, []); assert.equal(context.hydratedRef.current, false);

  // A local backup is account-scoped, and unknown-owner legacy data is not auto-imported.
  const cacheSource = name === "live" ? source : fs.readFileSync(new URL("../lib/chronologyWorkspaceLayout.js", import.meta.url), "utf8");
  const cacheStart = cacheSource.indexOf("function readSavedTiles(");
  const cacheEnd = cacheSource.indexOf("function dedupeTiles(", cacheStart);
  const values = new Map([["test-cache", JSON.stringify([{cameraId:"legacy-other-user"}])]]);
  const cacheContext = {window:{},STORAGE_KEY:"test-cache",localStorage:{getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,value)},
    normalizeTile:value=>value,dedupeTiles:value=>value};
  vm.runInNewContext(cacheSource.slice(cacheStart,cacheEnd)+"\nthis.read=readSavedTiles;this.save=saveTiles;",cacheContext);
  cacheContext.save([{cameraId:"A"}],1);
  assert.equal(JSON.stringify(cacheContext.read(1)), '[{"cameraId":"A"}]');
  assert.equal(JSON.stringify(cacheContext.read(2)), "[]");
  assert.equal(JSON.stringify(cacheContext.read()), "[]");
}

const hookSource = fs.readFileSync(new URL("../lib/useAuthSession.js", import.meta.url), "utf8").replace(/^import[^\n]+\n/gm,"").replaceAll("export ","");
const handlers = new Map(), observed = []; let token = "A", cleanup;
const hookContext = {getAuthToken:()=>token,useState:initial=>[initial(),value=>observed.push(value)],useEffect:fn=>{cleanup=fn();},
  window:{addEventListener:(key,fn)=>handlers.set(key,fn),removeEventListener:key=>handlers.delete(key)}};
vm.runInNewContext(hookSource+"\nuseAuthSession();",hookContext);
token = "B"; handlers.get("storage")({key:"token"});
assert.equal(observed.at(-1),"B");
const count = observed.length; handlers.get("storage")({key:"test-cache_user_1"});
assert.equal(observed.length,count);
token = ""; handlers.get("km-vms-auth-changed")(); assert.equal(observed.at(-1),"");
cleanup(); assert.equal(handlers.size,0);
console.log("Workspace session/debounce/hydration/cache/cross-mount write ownership PASS");
