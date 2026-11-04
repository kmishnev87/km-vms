import assert from 'node:assert/strict';
import {buildDashboardStatusSummary,systemHealthIndicatorModel} from '../lib/operatorWarnings.js';
const user={permissions:['run_diagnostics']};
for(const domains of [{},{storage:{severity:'ok'}},{recorder:{severity:'unknown'}}]){
  const summary=buildDashboardStatusSummary({domains});
  assert.equal(summary.severity,'unknown');
  assert.equal(systemHealthIndicatorModel({user,summary,runtimeStatusKnown:true}).state,'unknown');
}
const domains=Object.fromEntries(['cameras','storage','recorder','retention','reconciliation'].map(key=>[key,{severity:'ok'}]));
domains.live={severity:'unknown',items:[]};
assert.equal(buildDashboardStatusSummary({domains}).severity,'ok');
domains.recorder={severity:'error',safe_reason_codes:['recorder_heartbeat_stale']};
assert.equal(buildDashboardStatusSummary({domains}).severity,'error');
