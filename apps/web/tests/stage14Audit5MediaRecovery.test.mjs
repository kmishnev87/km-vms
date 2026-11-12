import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { isArchiveRecoveryHealthy } from '../lib/archivePlaybackRecovery.js';

const read = file => fs.readFileSync(new URL('../'+file, import.meta.url), 'utf8');
const records = read('app/recordings/page.js');
const code = records.slice(records.indexOf('function markViewerPlaybackHealthy('), records.indexOf('function closeDeleteDialog()'));
let attempted = true;
const video = { currentTime: 20, readyState: 4, error: null };
const context = { clearTimeout, isArchiveRecoveryHealthy, viewerRecoveryStableTimerRef:{current:null}, viewerVideoRef: {current: video}, viewerRecoveryProgressRef: {current: 20}, setViewerRefreshAttempted: value => { attempted = value; } };
vm.runInNewContext(code+';this.healthy=markViewerPlaybackHealthy;', context);
for (let pass=0; pass<3; pass++) {
  attempted = true; context.viewerRecoveryProgressRef.current = video.currentTime;
  context.healthy(); assert.equal(attempted, true, 'metadata alone cannot rearm an unplayable source');
  video.currentTime += 2; video.error = {}; context.healthy(); assert.equal(attempted, true);
  video.error = null; context.healthy(); assert.equal(attempted, false, 'a separate later failure gets a new bounded attempt');
}
const tile = read('components/ArchiveTilePlayer.js');
const tileCode = tile.slice(tile.indexOf('const handleHealthyProgress ='), tile.indexOf('    hardReset();\n    reportTileState("loading",', tile.indexOf('const handleHealthyProgress =')));
const tileContext = {clearTimeout, reportTileState(){}, stableReadyTimer:null, isArchiveRecoveryHealthy,cancelled:false,pendingLoad:null,refreshInFlight:false,recoveryProgressStart:10,refreshAttempts:1,video:{currentTime:12,readyState:4,error:null}};
vm.runInNewContext(tileCode+';this.healthy=handleHealthyProgress;',tileContext);
tileContext.pendingLoad = {}; tileContext.healthy(); assert.equal(tileContext.refreshAttempts,1);
tileContext.pendingLoad = null; tileContext.healthy(); assert.equal(tileContext.refreshAttempts,0);
assert.equal(isArchiveRecoveryHealthy({currentTime:10,readyState:4,error:null,paused:true},10,1999),false);
assert.equal(isArchiveRecoveryHealthy({currentTime:10,readyState:4,error:null,paused:true},10,2000),true);
assert.equal(isArchiveRecoveryHealthy({currentTime:10,readyState:4,error:{},paused:true},10,2000),false);
