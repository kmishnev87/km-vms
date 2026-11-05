import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../app/recordings/page.js',import.meta.url),'utf8');
const watch=source.slice(source.indexOf('async function handleWatch('),source.indexOf('async function toggleViewerFrameFullscreen('));
const refresh=source.slice(source.indexOf('async function refreshViewerUrlAfterMediaError('),source.indexOf('function restoreViewerPlaybackState('));
const requests=[],state={};
const context={clearTimeout,viewerRecoveryStableTimerRef:{current:null},isRecordingAvailable:()=>true,viewerSelectionRef:{current:{revision:0,item:null}},viewerFrameRef:{current:null},
  viewerRestoreStateRef:{current:null},viewerRefreshInFlightRef:{current:false},viewerItem:null,viewerRefreshAttempted:false,
  viewerVideoRef:{current:{currentTime:8,playbackRate:2,paused:false,ended:false}},document:{fullscreenElement:null},
  buildRecordingStreamUrl:item=>new Promise((resolve,reject)=>requests.push({item,resolve,reject})),
  showRecordingActionError:error=>{state.error=error;},setError:()=>{},
};
for(const name of ['ViewerTitle','ViewerUrl','ViewerItem','ViewerPlaybackError','ViewerRefreshAttempted','ViewerResolution','ViewerRect','ViewerFullscreen','ViewerOpen']){
  context['set'+name]=value=>{state[name]=value;if(name==='ViewerItem')context.viewerItem=value;if(name==='ViewerRefreshAttempted')context.viewerRefreshAttempted=value;};
}
vm.runInNewContext(watch+refresh+';this.watch=handleWatch;this.close=closeViewer;this.refresh=refreshViewerUrlAfterMediaError;',context);
const a={id:1,filename:'A'},b={id:2,filename:'B'};
const first=context.watch(a),second=context.watch(b);
requests[1].resolve('url-b');await second;requests[0].resolve('url-a');await first;
assert.equal(state.ViewerItem,b);assert.equal(state.ViewerUrl,'url-b');assert.equal(state.ViewerTitle,'B');
const closed=context.watch(a);context.close();requests[2].resolve('late-a');await closed;
assert.equal(state.ViewerOpen,false);assert.equal(state.ViewerItem,null);
const reopened=context.watch(b);requests[3].resolve('url-b');await reopened;
const oldRefresh=context.refresh();
const newer=context.watch(a);requests[5].resolve('url-a');await newer;
const newRefresh=context.refresh();
requests[4].resolve('late-refresh-b');await oldRefresh;
assert.equal(context.viewerRefreshInFlightRef.current,true);
assert.equal(state.ViewerUrl,'url-a');
requests[6].resolve('fresh-a');await newRefresh;
assert.equal(state.ViewerUrl,'fresh-a');assert.equal(state.ViewerItem,a);
