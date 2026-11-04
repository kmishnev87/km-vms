import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function harness() {
  let token='session-a';
  const requests=[], events=new Map();
  const context={ getAuthToken:()=>token, clearAuthToken:()=>{token='';},
    apiFetch:()=>new Promise((resolve,reject)=>requests.push({resolve,reject})),
    window:{addEventListener:(name,fn)=>{if(!events.has(name))events.set(name,new Set());events.get(name).add(fn);},
      removeEventListener:(name,fn)=>events.get(name)?.delete(fn)},
  };
  const strip=source=>source.replace(/^import[\s\S]*?from\s+["'][^"']+["'];?\s*/gm,'').replaceAll('export const ','const ').replaceAll('export function ','function ');
  const core=strip(fs.readFileSync(new URL('../lib/currentUserCore.js',import.meta.url),'utf8'));
  const store=strip(fs.readFileSync(new URL('../lib/currentUser.js',import.meta.url),'utf8'));
  vm.runInNewContext(core+'\n'+store+'\nthis.store={loadCurrentUser,clearCurrentUserState,snapshot,subscribe};',context);
  return {store:context.store,requests,setToken:value=>{token=value;},getToken:()=>token,
    emit:(name,event={})=>events.get(name)?.forEach(fn=>fn(event)),events};
}

for (const oldFails of [false,true]) {
  const h=harness();
  const a=h.store.loadCurrentUser();
  h.setToken('session-b');
  const b=h.store.loadCurrentUser();
  if(oldFails) h.requests[0].reject(Object.assign(new Error('Forbidden'),{status:403}));
  else h.requests[0].resolve({id:1,username:'old-owner',permissions:['manage_users']});
  await a;
  const same=h.store.loadCurrentUser({force:true});
  assert.equal(h.requests.length,2,'late finally cannot clear the new in-flight owner');
  h.requests[1].resolve({id:2,username:'new-viewer',permissions:['view_live']});
  await Promise.all([b,same]);
  assert.equal(h.store.snapshot().currentUser.id,2);
  assert.equal(h.getToken(),'session-b');
}
{
  const h=harness();
  const unsubscribers=[h.store.subscribe(()=>{}),h.store.subscribe(()=>{}),h.store.subscribe(()=>{})];
  assert.equal(h.events.get('storage').size,1);
  for(let i=0;i<10;i++)h.emit('storage',{key:'vms_live_workspace_v1'});
  assert.equal(h.requests.length,0);
  h.emit('storage',{key:'token'});
  assert.equal(h.requests.length,1);
  h.requests[0].resolve({id:1,permissions:[]});
  await Promise.resolve(); await Promise.resolve();
  unsubscribers.forEach(fn=>fn());
  assert.equal(h.events.get('storage').size,0);
}
