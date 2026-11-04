import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function harness(){
  const storage=()=>{const data=new Map();return{getItem:key=>data.get(key)||null,setItem:(key,value)=>data.set(key,value),removeItem:key=>data.delete(key)};};
  const localStorage=storage(),sessionStorage=storage(),requests=[];
  const context={process:{env:{}},Date,URLSearchParams,localStorage,sessionStorage,
    CustomEvent:class{}, window:{localStorage,sessionStorage,dispatchEvent:()=>{}},
    fetch:()=>new Promise(resolve=>requests.push(resolve)),
  };
  const source=fs.readFileSync(new URL('../lib/api.js',import.meta.url),'utf8')
    .replace(/^import[^\n]*\n/gm,'').replaceAll('export async function ','async function ').replaceAll('export function ','function ').replaceAll('export const ','const ');
  vm.runInNewContext(source+';this.api={apiFetch,apiFetchBlob,getAuthToken,saveAuthToken};',context);
  return{api:context.api,requests};
}
const response=status=>({status,ok:false,headers:{get:()=> 'application/json'},json:async()=>({detail:'Localized failure'})});
for(const method of ['apiFetch','apiFetchBlob']){
  for (const path of ['/recordings/stream?media_token=expired-fixture', '/auth/login']) {
    const h=harness();h.api.saveAuthToken('session-a');
    const request=h.api[method](path);h.requests[0](response(401));await assert.rejects(request);
    assert.equal(h.api.getAuthToken(),'session-a','scoped media or camera/login credentials are not the application session');
  }
  for(const status of [401,403,503]){
    const h=harness(); h.api.saveAuthToken('session-a');
    const request=h.api[method]('/protected');h.requests[0](response(status));
    await assert.rejects(request);
    assert.equal(h.api.getAuthToken(),status===401?'':'session-a');
  }
  for(const next of ['session-b','session-a']){
    const h=harness();h.api.saveAuthToken('session-a');
    const request=h.api[method]('/protected');h.api.saveAuthToken(next);
    h.requests[0](response(401));await assert.rejects(request);
    assert.equal(h.api.getAuthToken(),next,'a prior session 401 cannot clear a new login, even if token bytes match');
  }
}
