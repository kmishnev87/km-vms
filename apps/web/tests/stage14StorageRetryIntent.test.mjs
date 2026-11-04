import { readProductSourceFile } from "./helpers/readProductSources.mjs";
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../lib/useStorageMigrationController.js',import.meta.url),'utf8');
const code=['retryMigration','takeoverMigrationCleanup'].map(name=>{
  const start=source.indexOf('  async function '+name+'('),end=source.indexOf('\n  }\n',start);
  assert.ok(start>=0 && end>start);
  return source.slice(start,end+4);
}).join('\n');
let serial=0,lost=true;const requests=[];
const ctx={migrationOperation:{operation_id:'parent-A'},migrationPlan:{plan_id:'plan-A',canonical_hash:'hash-A'},
  migrationScenario:{canRetry:true,canCleanupTakeover:true},migrationRetryRequestKeyRef:{current:null},
  migrationTakeoverRequestKeyRef:{current:null},newStorageOperationId:prefix=>prefix+'-'+(++serial),
  setMigrationBusy(){},setMigrationMessage(){},setMigrationPlan(){},setMigrationOperation(){},
  errorDetailText:()=>'',copy:{},language:'ru',
  apiFetch:async(path,options)=>{requests.push({path,key:JSON.parse(options.body).idempotency_key});if(lost)throw new Error('lost reply');return{};}};
vm.runInNewContext(code+';this.retry=retryMigration;this.takeover=takeoverMigrationCleanup;',ctx);
await ctx.retry();await ctx.retry();assert.equal(requests[0].key,requests[1].key);
ctx.migrationOperation={operation_id:'parent-B'};await ctx.retry();assert.notEqual(requests[1].key,requests[2].key);
lost=false;await ctx.retry();assert.equal(requests[2].key,requests[3].key);
await ctx.retry();assert.notEqual(requests[3].key,requests[4].key);
lost=true;await ctx.takeover();await ctx.takeover();assert.equal(requests[5].key,requests[6].key);
ctx.migrationPlan={plan_id:'plan-C',canonical_hash:'hash-C'};await ctx.takeover();assert.notEqual(requests[6].key,requests[7].key);
