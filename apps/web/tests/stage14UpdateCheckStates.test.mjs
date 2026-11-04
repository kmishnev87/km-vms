import assert from 'node:assert/strict';
import {updateApplyOperatorModel} from '../lib/settingsUpdateApplyHelpers.js';

const t={updateApplyHeadlines:{not_checked:'Not checked',checking:'Checking',current:'Current',available:'Available',blocked:'Error',completed:'Completed'},
  updateApplySummaries:{not_checked:'Press check',checking:'Requesting',current:'Up to date',available:'Review',blocked:'Failed'},
  maintenanceStatuses:{idle:'Idle'},updateApplyResults:{}};
const initial={status:'known',projection_source:'installed_identity',check_state:'not_checked'};
const idle={status:'idle'};
let model=updateApplyOperatorModel(initial,idle,t);
assert.equal(model.headline,'Not checked');
assert.equal(model.summary,'Press check');
assert.equal(model.severity,'neutral');
assert.equal(updateApplyOperatorModel(initial,idle,t,'en',{checking:true}).headline,'Checking');
assert.equal(updateApplyOperatorModel({status:'current'},idle,t).headline,'Current');
assert.equal(updateApplyOperatorModel({status:'update_available',can_apply_from_ui:true},idle,t).headline,'Available');
assert.equal(updateApplyOperatorModel({...initial,last_check_status:'check_failed'},idle,t).headline,'Error');
const completed={status:'completed',expected_commit:'a'.repeat(40),installed_commit:'a'.repeat(40),commit_verified:true};
const nextRelease={status:'update_available',can_apply_from_ui:true,installed_release:{metadata_status:'complete'}};
assert.equal(updateApplyOperatorModel(nextRelease,completed,t).headline,'Available','an earlier successful apply cannot hide a newly available release');
