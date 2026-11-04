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
domains.reconciliation={severity:'unknown',status:'no_evidence',reason_codes:['no_evidence','reconciliation_unknown'],problem_file_count:0,cleanup_candidate_count:0};
let summary=buildDashboardStatusSummary({domains});
assert.equal(summary.severity,'ok','a full integrity scan is optional, not a prerequisite for healthy operational services');
assert.equal(summary.rows.find(row=>row.domain==='reconciliation').severity,'unknown','do not invent an archive integrity check');
assert.equal(systemHealthIndicatorModel({user,summary,runtimeStatusKnown:true}).state,'healthy');
for(const extra of [{severity:'warning',problem_file_count:1},{severity:'error',problem_file_count:1},{partial:true},{scan_limited:true},{reason_codes:['no_evidence','transport_failed']}]){
  const actual=buildDashboardStatusSummary({domains:{...domains,reconciliation:{...domains.reconciliation,...extra}}});
  assert.notEqual(actual.severity,'ok','real failures, incomplete checks and unknown errors remain visible');
}
for(const domain of ['recorder','storage','cameras','retention']){
  assert.equal(buildDashboardStatusSummary({domains:{...domains,[domain]:{severity:'unknown'}}}).severity,'unknown');
}
domains.recorder={severity:'error',safe_reason_codes:['recorder_heartbeat_stale']};
assert.equal(buildDashboardStatusSummary({domains}).severity,'error');
