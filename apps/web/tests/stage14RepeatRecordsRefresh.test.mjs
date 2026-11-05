import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
const source=fs.readFileSync(new URL("../app/recordings/page.js",import.meta.url),"utf8");
const load=source.slice(source.indexOf("  async function loadRecordings("),source.indexOf("  async function loadRecorderStatus("));
const refresh=source.slice(source.indexOf("  async function refresh()"),source.indexOf("  function toggleSelected("));
const key=camera=>JSON.stringify([camera,"",1,20,"created_at","desc"]);
const requestIdRef={current:0},currentQueryKeyRef={current:key("A")};
const requests=[], errors=[];let resolveCameras;
const state={items:[],loadedQueryKey:"",loadState:"idle"};
function context(camera){
  const ctx={selectedCamera:camera,selectedDate:"",currentPage:1,pageSize:20,sortBy:"created_at",sortDir:"desc",currentQueryKey:key(camera),
    currentQueryKeyRef,requestIdRef,loadedQueryKey:"",URLSearchParams,
    apiFetch:path=>new Promise(resolve=>requests.push({path,resolve})),
    setRecordingsLoadState:value=>state.loadState=value,setSelectedPaths:()=>{},setItems:value=>state.items=value,
    setRecordingsSummary:()=>{},setRecordingsPagination:()=>{},setProductTimezone:()=>{},setError:()=>{},
    setLoadedQueryKey:value=>state.loadedQueryKey=value,normalizeRecordingError:x=>x,showRecordingActionError:error=>errors.push(error),
    productDateFilterParam:x=>x,loadCameras:()=>new Promise(resolve=>resolveCameras=resolve),loadRecorderStatus:async()=>{},
    resolveEffectiveRecordingCamera:(selected,options)=>options.includes(selected)?selected:"__all__",
    setSelectedCamera:()=>{},setCurrentPage:()=>{}};
  vm.runInNewContext(load+refresh+"\nthis.load=loadRecordings;this.refresh=refresh;",ctx);return ctx;
}
const a=context("A");const refreshing=a.refresh();
currentQueryKeyRef.current=key("B");
const b=context("B");const latest=b.load("B","",1);
assert.equal(requests.length,1);
requests[0].resolve({items:[{camera:"B"}],pagination:{total_count:1}});await latest;
resolveCameras(["A","B"]);await refreshing;
assert.equal(requests.length,1,"stale Refresh(A) must not issue a later A request after B");
assert.equal(state.items[0].camera,"B");
assert.equal(state.loadedQueryKey,currentQueryKeyRef.current);
assert.equal(state.loadState,"loaded");assert.deepEqual(errors,[]);
console.log("Real Records refresh/filter chain keeps newest rows, query identity and enabled actions PASS");
