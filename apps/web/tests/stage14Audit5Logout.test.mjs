import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const store = () => { const values = new Map(); return {getItem:k=>values.get(k)||'', setItem:(k,v)=>values.set(k,v), removeItem:k=>values.delete(k)}; };
const tick = () => new Promise(resolve => setImmediate(resolve));
const mediaResponse = {status:200,ok:true,headers:{get:()=> 'application/json'},json:async()=>({media_token:'synthetic-media',expires_in:300})};
function harness() {
  const localStorage=store(), sessionStorage=store(), calls=[];
  const context={Date,URLSearchParams,AbortController,setTimeout,clearTimeout,process:{env:{}},localStorage,sessionStorage,
    CustomEvent:class { constructor(type, options) {this.type=type;this.detail=options?.detail;} },
    window:{dispatchEvent:()=>{}},fetch:(url,options)=>new Promise(resolve=>calls.push({url,options,resolve})),
  };
  const code=fs.readFileSync(new URL('../lib/api.js',import.meta.url),'utf8').replace(/^import[^\n]*\n/gm,'').replaceAll('export async function ','async function ').replaceAll('export function ','function ');
  vm.runInNewContext(code+';this.api={saveAuthToken,getAuthToken,logoutAuthSession,issueLiveMediaTokenInfo};',context);
  context.api.saveAuthToken('synthetic-session');
  return {context,calls,api:context.api};
}
{
  const h=harness();
  let fireDeadline;
  h.context.setTimeout = callback => { fireDeadline=callback; return 1; };
  h.context.clearTimeout = () => {};
  const pending=h.api.issueLiveMediaTokenInfo(1,'sub');
  const logout=h.api.logoutAuthSession();
  fireDeadline();await assert.rejects(logout);
  assert.equal(h.api.getAuthToken(),'synthetic-session','unconfirmed logout is not presented as completed');
  h.calls[0].resolve(mediaResponse);await pending;await tick();
  assert.equal(h.calls.length,1,'late cleanup cannot revive a timed-out logout request');
  assert.equal(h.api.getAuthToken(),'synthetic-session');
}
{
  const h=harness();
  const pending=h.api.issueLiveMediaTokenInfo(1,'sub');
  const logout=h.api.logoutAuthSession();
  await tick();assert.equal(h.calls.length,1,'logout must wait for earlier Set-Cookie');
  h.calls[0].resolve(mediaResponse);await pending;await tick();
  assert.equal(h.calls[1].url,'/api/auth/logout');
  assert.equal(h.api.getAuthToken(),'synthetic-session','viewer cleanup retains its own authorization');
  h.calls[1].resolve({status:204,ok:true});await logout;
  assert.equal(h.api.getAuthToken(),'');
}
{
  const h=harness(), pending=h.api.issueLiveMediaTokenInfo(1,'sub');
  const logout=h.api.logoutAuthSession();h.api.saveAuthToken('newer-session');
  h.calls[0].resolve(mediaResponse);await pending;await logout;
  assert.equal(h.calls.length,1,'stale logout cannot expire the newer login cookies');
  assert.equal(h.api.getAuthToken(),'newer-session');
}
