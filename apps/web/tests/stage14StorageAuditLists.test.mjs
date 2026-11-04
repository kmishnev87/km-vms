import assert from 'node:assert/strict';
import {cameraStorageRows} from '../lib/storageOperations.js';
import {buildAuditEventsPath,sanitizeAuditFiltersFromEntries} from '../lib/auditEntryContract.js';
for(const count of [0,1,12,13,100]){
  const rows=Array.from({length:count},(_,id)=>({camera_id:id+1,size_bytes:count-id,problem_file_count:id===count-1?1:0}));
  const result=cameraStorageRows(rows);assert.equal(result.length,count);
  if(count)assert.equal(result.at(-1).problem_file_count,1);
}
assert.equal(sanitizeAuditFiltersFromEntries([]).filters.since_minutes,'1440');
for(const entries of [[['since_minutes','']],[['period','all']]]){
  const filters=sanitizeAuditFiltersFromEntries(entries).filters;
  assert.equal(filters.since_minutes,'');
  for(const offset of [0,50,100]){
    const path=buildAuditEventsPath(filters,offset);
    assert.equal(path.includes('since_minutes'),false);assert.match(path,new RegExp('offset='+offset));
  }
}
assert.match(buildAuditEventsPath({since_minutes:'360'}),/since_minutes=360/);
