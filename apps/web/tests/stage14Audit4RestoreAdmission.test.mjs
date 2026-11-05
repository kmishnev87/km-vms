import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../lib/settingsMaintenanceController.js',import.meta.url),'utf8');
function exact(name){const m=new RegExp('^  (?:async )?function '+name+'\\(','m').exec(source);assert.ok(m,name);const e=source.indexOf('\n  }',m.index);return source.slice(m.index,e+4);}
const turn=async()=>{for(let i=0;i<8;i++)await new Promise(r=>setImmediate(r));};
function harness({status={status:'idle'},admission='not_admitted',inFlight=false}={}){
 const calls=[],toasts=[],ctx={
  ownsMaintenanceSession:()=>true,captureAuthSession:()=>()=>true,
  currentRestorePendingRef:{current:{submissionId:'own-submission',artifactId:'own-backup'}},
  currentRestorePollInFlightRef:{current:false},currentRestoreApplyInFlightRef:{current:inFlight?'own-submission':null},
  currentRestoreDialog:{accepted:true},currentRestoreStatus:null,
  setCurrentRestoreDialog:value=>{ctx.currentRestoreDialog=typeof value==='function'?value(ctx.currentRestoreDialog):value;},
  setCurrentRestoreStatus:value=>{ctx.currentRestoreStatus=value;},
  commitCurrentRestorePending:value=>{ctx.currentRestorePendingRef.current=value;},
  showToast:value=>toasts.push(value),t:{maintenanceCurrentRestoreNotAdmitted:'Not admitted'},
  currentRestoreTerminal:s=>Boolean(s?.terminal_result),refreshMaintenanceBackupProjections:async()=>{},
  formatAuditTimestamp:()=>'',lang:'en',CURRENT_RESTORE_CONFIRMATION_PHRASE:'RESTORE KM VMS',
  updateApplyRequestIsAmbiguous:e=>!e.status||e.status>=500,currentRestoreReasonCode:()=>'',currentRestoreReasonText:()=>'',
  apiFetch:async(path,options)=>{calls.push({path,options});if(path.endsWith('/status'))return status;
   return {submission_id:'own-submission',admission,restore_status:{submission_id:'own-submission',status:'queued'}};},
 };
 vm.runInNewContext(exact('pollCurrentRestoreStatus')+'\nthis.poll=pollCurrentRestoreStatus;',ctx);
 return {ctx,calls,toasts};
}
{
 const h=harness();await h.ctx.poll();
 assert.equal(h.ctx.currentRestorePendingRef.current,null);
 assert.equal(h.ctx.currentRestoreDialog,null);
 assert.equal(h.toasts.length,1);
 assert.equal(h.calls.length,2);
 assert.equal(h.calls[1].path,'/system/restore/current/reconcile');
 assert.equal(h.calls.filter(c=>c.path.endsWith('/apply')).length,0,'reconciliation never auto-retries restore');
}
{
 const h=harness({admission:'admitted'});await h.ctx.poll();
 assert.ok(h.ctx.currentRestorePendingRef.current);
 assert.equal(h.ctx.currentRestoreStatus.status,'queued');
 assert.equal(h.ctx.currentRestoreDialog.reconnecting,false);
 assert.equal(h.toasts.length,0,'missing projection must not discard durable admission');
}
{
 const h=harness({inFlight:true});await h.ctx.poll();
 assert.equal(h.calls.length,1,'do not retire a POST still awaited by this page');
 assert.ok(h.ctx.currentRestorePendingRef.current);
}
{
 const h=harness({status:{status:'completed',submission_id:'own-submission',terminal_result:'completed'}});await h.ctx.poll();
 assert.equal(h.calls.length,1);assert.equal(h.ctx.currentRestorePendingRef.current,null);
}
{
 const h=harness();h.ctx.apiFetch=async()=>{throw Object.assign(new Error('temporary'),{status:503});};await h.ctx.poll();
 assert.ok(h.ctx.currentRestorePendingRef.current);assert.equal(h.toasts.length,0);
 assert.equal(h.ctx.currentRestoreDialog.reconnecting,true);
}
{
 const h=harness();h.ctx.captureAuthSession=()=>()=>false;await h.ctx.poll();
 assert.ok(h.ctx.currentRestorePendingRef.current);assert.equal(h.calls.length,1);assert.equal(h.toasts.length,0);
}
{
 const plain=fs.readFileSync(new URL('../lib/actorPendingStorage.js',import.meta.url),'utf8').replaceAll('export ','');
 const ctx={};vm.runInNewContext(plain+'\nthis.scope=actorPendingStorage;',ctx);
 const values=new Map(),storage={getItem:k=>values.get(k)??null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
 const a=ctx.scope(storage,1),b=ctx.scope(storage,2);
 a.setItem('pending','already accepted operation A');
 assert.equal(b.getItem('pending'),null);b.removeItem('pending');
 assert.equal(a.getItem('pending'),'already accepted operation A','session change preserves own durable-job recovery');
}
console.log('Audit4 restore admission: unadmitted unlock, admitted preservation, in-flight guard, no auto retry, account-scoped recovery PASS');
