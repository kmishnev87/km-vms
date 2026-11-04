import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../app/settings/page.js',import.meta.url),'utf8');
const load=source.slice(source.indexOf('  async function load()'),source.indexOf('  async function loadUsers()'));
const patch=source.slice(source.indexOf('  function patch(key,'),source.indexOf('  function reset',source.indexOf('  function patch(key,')));
for(const failedPath of ['/settings','/hardware/capabilities','/users/me']){
  const state={},pending={};const ctx={loadGenerationRef:{current:0},pendingLanguageRef:{current:'en'},
    apiFetch:path=>new Promise((resolve,reject)=>pending[path]={resolve,reject}),
    settingsDraftFromApi:value=>value,setSavedDraft:value=>state.saved=value,
    setDraft:value=>state.draft=typeof value==='function'?value(state.draft??null):value,
    setFormatStatus:value=>state.format=value,setHardware:value=>state.hardware=value,
    setCurrentUser:value=>state.user=value,showToast:value=>state.error=value,
    normalizedError:error=>error,lang:'ru',loadUsers:async()=>{state.users=true;}};
  vm.runInNewContext(load+';this.load=load;',ctx);
  const done=ctx.load();
  for(const path of Object.keys(pending)){
    if(path===failedPath)pending[path].reject(new Error('temporary failure'));
    else pending[path].resolve(path==='/settings'?{language:'ru',timezone:'UTC',recording_format_change:'idle'}:{permissions:['admin_access']});
  }
  await done;
  if(failedPath!=='/settings'){assert.equal(state.saved.timezone,'UTC');assert.equal(state.draft.timezone,'UTC');assert.equal(state.draft.language,'en');}
  if(failedPath!=='/users/me'){assert.ok(state.user);assert.equal(state.users,true);}
  if(failedPath!=='/hardware/capabilities')assert.ok(state.hardware);
}
// The real field updater must not construct a language-only settings draft.
const start=source.indexOf('  function patch(key,');
const fn=source.slice(start,source.indexOf('\n  function ',start+5));
let draft=null;
vm.runInNewContext(fn+';patch("language","en");',{setDraft:updater=>draft=updater(draft)});
assert.equal(draft,null);
