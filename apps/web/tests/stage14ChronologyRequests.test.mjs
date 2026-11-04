import assert from 'node:assert/strict';
import {createCoalescedPlaybackResolver} from '../lib/coalescedPlaybackResolver.js';
const turn=()=>new Promise(resolve=>setImmediate(resolve));
for (const delay of [0,500,1500,5000]) {
  const queue=createCoalescedPlaybackResolver(), pending=[],applied=[];
  let active=0,peak=0;
  const request=(value,automatic=false)=>queue.resolve({automatic,
    run:()=>{active++;peak=Math.max(peak,active);return new Promise(resolve=>pending.push(()=>{active--;resolve(value);}));},
    apply:value=>applied.push(value)});
  const first=request('segment-A');
  for(let elapsed=1000;elapsed<=delay;elapsed+=1000) void request('clock-'+elapsed,true);
  pending.shift()();await first;await turn();
  assert.equal(applied[0],'segment-A',`response survives ${delay}ms clock advances`);
  while(pending.length){pending.shift()();await turn();}
  assert.equal(peak,1);
  const old=request('old'),newer=request('seek-B');
  pending.shift()();await old;await turn();
  pending.shift()();assert.deepEqual(await newer,{applied:true});
  assert.equal(applied.at(-1),'seek-B');assert.equal(applied.includes('old'),false);
  const closing=request('after-close');queue.invalidate();pending.shift()();
  assert.deepEqual(await closing,{applied:false});
}
