import assert from 'node:assert/strict';
import test from 'node:test';
import {createSingleFlight} from '../lib/singleFlight.js';

test('status readers share one request and can retry after failure', async () => {
  const reader=createSingleFlight();
  let finish, calls=0;
  const task=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
  const a=reader.run(task), b=reader.run(task);
  await Promise.resolve();
  assert.equal(a,b);
  assert.equal(calls,1);
  finish('status');
  assert.deepEqual(await Promise.all([a,b]),['status','status']);
  await assert.rejects(reader.run(async()=>{throw new Error('offline');}),/offline/);
  assert.equal(await reader.run(async()=> 'recovered'),'recovered');
});
