import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('../lib/settingsMaintenanceController.js', import.meta.url), 'utf8');
const extract = (name) => {
  const body = source.match(new RegExp(`^  (?:async )?function ${name}\\([^]*?^  }`, 'm'))?.[0];
  assert(body, `missing actual controller function: ${name}`);
  return body;
};
function harness({ reject = false, busy = '', restore = null } = {}) {
  const state = { dialogs: [], requests: [], toasts: [], busy, result: null };
  const pendingRef = { current: null };
  const scope = {
    maintenanceBusy:busy, currentRestorePending:restore, maintenanceBackupPendingRef:pendingRef,
    maintenanceBackupRecoveryRef:{current:false}, maintenanceBackupAdmissionRef:{current:null},
    createUpdateApplySubmissionId:()=>'synthetic-submission',
    createBackupOperationPending:(kind,artifactId,submissionId)=>({kind,artifactId,submissionId}),
    commitBackupOperationPending:(value)=>{pendingRef.current=value;return true;},
    setMaintenanceBusy:(value)=>{state.busy=typeof value==='function'?value(state.busy):value;},
    setMaintenanceConfirm:(value)=>{if(value)state.dialogs.push('confirmation');},
    setMaintenanceDetail:(value)=>{if(value)state.dialogs.push(value);},
    setMaintenanceActionResult:()=>{}, setMaintenanceBackupResult:(value)=>{state.result=value;},
    showToast:(value)=>state.toasts.push(value),
    backupOperationFallback:()=> 'check failed', updateApplyRequestIsAmbiguous:()=>false,
    t:{maintenanceBackupOperationLabels:{check:'Check'},maintenanceBackupsTitle:'Backups'},
    apiFetch:async(path,options)=>{state.requests.push({path,body:JSON.parse(options.body)});if(reject)throw {status:400};return {state:'completed'};},
    acceptBackupOperationReceipt:async()=>{pendingRef.current=null;state.result={status:'validated'};},
  };
  const functions = new Function(...Object.keys(scope), `${extract('performMaintenanceBackupOperation')}\n${extract('requestCheckMaintenanceBackup')}\nreturn {requestCheckMaintenanceBackup,performMaintenanceBackupOperation};`)(...Object.values(scope));
  return {state,...functions};
}
for (const reject of [false,true]) {
  const test=harness({reject});
  await test.requestCheckMaintenanceBackup({id:'synthetic-artifact'});
  assert.deepEqual(test.state.dialogs,[], 'check may open neither a confirmation nor a result dialog');
  assert.equal(test.state.requests.length,1);
  assert.equal(test.state.requests[0].path,'/system/restore/apply', 'still checks restoration in a temporary database');
  assert.equal(test.state.requests[0].body.artifact_id,'synthetic-artifact');
  assert.equal(test.state.requests[0].body.confirm,true, 'backend contract remains explicit');
  assert.equal(test.state.busy,'');
  assert.equal(test.state.result.status,reject?'failed':'validated');
}
for (const options of [{busy:'backup-create'},{restore:{status:'running'}}]) {
  const test=harness(options);await test.requestCheckMaintenanceBackup({id:'synthetic-artifact'});
  assert.equal(test.state.requests.length,0,'conflicting operations remain blocked');
}
const missing=harness();await missing.requestCheckMaintenanceBackup(null);assert.equal(missing.state.requests.length,0);
const failed=harness({reject:true});await failed.requestCheckMaintenanceBackup({id:'synthetic-artifact'});
assert.equal(failed.state.toasts[0].variant,'warning','a failure remains visible without a modal');
console.log('Backup checks run in the existing list without confirmation/result dialogs; backend contract and conflict guards preserved');
