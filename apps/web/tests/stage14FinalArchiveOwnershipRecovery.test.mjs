import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import test from 'node:test';
const read=p=>fs.readFileSync(new URL(`../${p}`, import.meta.url),'utf8');
const plain=s=>s.replace(/^import\b[\s\S]*?;\r?\n/gm,'').replaceAll('export default ','').replaceAll('export ','');
const turn=async()=>{for(let i=0;i<5;i++)await new Promise(r=>setImmediate(r));};
const noop=()=>{}, passed=[];
async function check(name,fn){await test(name,fn);passed.push(name);}

// Small dependency-aware hook runner: exact product functions, controlled dummy I/O.
function hooks(source,ctx,name,props,beforeCommit=noop){
 const slots=[],queue=new Set();let cursor=0,dirty=true,result,mounted=true;
 const equal=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 Object.assign(ctx,{
  useState(initial){const i=cursor++;if(!slots[i])slots[i]={kind:'state',value:typeof initial==='function'?initial():initial};return[slots[i].value,v=>{const n=typeof v==='function'?v(slots[i].value):v;if(!Object.is(n,slots[i].value)){slots[i].value=n;dirty=true;}}];},
  useRef(initial){const i=cursor++;if(!slots[i])slots[i]={kind:'ref',value:{current:initial}};return slots[i].value;},
  useCallback(fn,deps){const i=cursor++;if(!slots[i]||!equal(slots[i].deps,deps))slots[i]={kind:'callback',value:fn,deps};return slots[i].value;},
  useEffect(fn,deps){const i=cursor++;if(!slots[i])slots[i]={kind:'effect',deps:undefined,cleanup:null};if(!equal(slots[i].deps,deps)){slots[i].next=fn;slots[i].nextDeps=deps;queue.add(i);}},
 });
 vm.runInNewContext(source+`\nthis.component=${name};`,ctx);
 const h={
  flush(next){if(next!==undefined){props=next;dirty=true;}let loops=0;while(dirty&&mounted){assert.ok(++loops<40,'render/effect loop');dirty=false;cursor=0;result=ctx.component(props);beforeCommit(slots);const pending=[...queue];queue.clear();for(const i of pending){const s=slots[i];s.cleanup?.();s.fn=s.next;s.deps=s.nextDeps;s.cleanup=s.fn();}}return result;},
  unmount(){mounted=false;for(const s of slots)if(s.kind==='effect')s.cleanup?.();},
  strictCycle(){for(const s of slots)if(s.kind==='effect')s.cleanup?.();for(const s of slots)if(s.kind==='effect')s.cleanup=s.fn();return h.flush();},
  get result(){return result;},
 };h.flush();return h;
}
const exportSource=plain(read('lib/archiveExports.js'))+'\n'+plain(read('lib/useAuthSession.js'))+'\n'+plain(read('lib/useArchiveExport.js'));
function exportHarness(){
 let token='dummy-A',timer=0;const requests=[],anchors=[],objectUrls=[],events=new Map(),timers=new Map();
 const request=(path,options={},blob=false)=>new Promise((resolve,reject)=>requests.push({path,method:options.method||'GET',session:token,signal:options.signal,blob,resolve,reject}));
 const ctx={AbortController,DOMException,URL:{createObjectURL(b){objectUrls.push(b);return'blob:dummy';},revokeObjectURL:noop},
  setTimeout(fn){timers.set(++timer,fn);return timer;},clearTimeout:id=>timers.delete(id),getAuthToken:()=>token,apiFetch:request,apiFetchBlob:(p,o)=>request(p,o,true),
  window:{addEventListener(k,f){if(!events.has(k))events.set(k,new Set());events.get(k).add(f);},removeEventListener:(k,f)=>events.get(k)?.delete(f)},
  document:{createElement(){const a={clicked:false,click(){this.clicked=true;},remove:noop};anchors.push(a);return a;},body:{appendChild:noop}},
 };
 const h=hooks(exportSource,ctx,'useArchiveExport');
 return{h,context:ctx,requests,anchors,objectUrls,events,timers,
  token(v,notify=true){token=v;if(notify)for(const f of events.get('km-vms-auth-changed')||[])f();},
  async ready(id='dummy-job'){const p=h.result.loadRecent();requests.at(-1).resolve({items:[{id,status:'done',download_ready:true,filename:'dummy.mkv'}]});await p;h.flush();h.result.selectReady(id);h.flush();},
 };
}
await check('F07 switch before effect blocks next workflow request',async()=>{
 const{h,token,requests}=exportHarness();const p=h.result.prepare({camera_id:1});token('dummy-B',false);requests[0].resolve({id:'A-job',status:'queued'});assert.equal(await p,null);assert.equal(requests.length,1);token('dummy-B');h.flush();assert.equal(h.result.job,null);assert.equal(h.result.busy,false);assert.equal(h.result.phase,'idle');
});
await check('F07 stale A finally cannot overwrite active B preparation',async()=>{
 const{h,token,requests}=exportHarness();const a=h.result.prepare({camera_id:1});token('dummy-B');h.flush();assert.equal(requests[0].signal.aborted,true);const b=h.result.prepare({camera_id:2});h.flush();requests[0].resolve({id:'A-job',status:'queued'});await a;h.flush();assert.equal(h.result.busy,true);assert.equal(h.result.job,null);requests[1].resolve({id:'B-job',status:'done'});await turn();assert.equal(requests.at(-1).path,'/archive/exports/B-job/manifest');requests.at(-1).resolve({manifest:'dummy'});await b;h.flush();assert.equal(h.result.job.id,'B-job');assert.equal(h.result.phase,'ready');assert.equal(h.result.busy,false);assert.ok(requests.slice(1).every(r=>r.session==='dummy-B'));
});
await check('F07 stale recent ignored and unmount aborts pending operations',async()=>{
 const{h,token,requests,events}=exportHarness();const old=h.result.loadRecent();token('dummy-B');h.flush();requests[0].resolve({items:[{id:'old-A',status:'done',download_ready:true}]});await old;h.flush();assert.equal(h.result.recent.length,0);const list=h.result.loadRecent(),prep=h.result.prepare({camera_id:2});h.unmount();assert.ok(requests.every(r=>r.signal.aborted));assert.ok([...events.values()].every(v=>v.size===0));requests[1].resolve({items:[]});requests[2].resolve({id:'late',status:'queued'});await Promise.all([list,prep]);assert.equal(requests.length,3);
});
for(const manifest of[false,true])await check(`F07 stale ${manifest?'manifest':'native'} download has no browser side effect`,async()=>{
 const{h,ready,requests,token,anchors,objectUrls}=exportHarness();await ready();const p=h.result.download(manifest);token('dummy-B',false);requests.at(-1).resolve(manifest?{blob:{dummy:true},filename:'dummy.json'}:{media_token:'TEST_ONLY',filename:'dummy.mkv'});await p;assert.equal(anchors.length,0);assert.equal(objectUrls.length,0);token('dummy-B');h.flush();assert.equal(h.result.transferBusy,false);assert.equal(h.result.job,null);
});
await check('F07 same-session native and manifest download paths preserved',async()=>{
 const{h,ready,requests,anchors,objectUrls}=exportHarness();await ready('dummy-safe');let p=h.result.download();assert.equal(requests.at(-1).blob,false);requests.at(-1).resolve({media_token:'TEST_ONLY',filename:'dummy.mkv'});await p;h.flush();assert.equal(anchors.at(-1).clicked,true);assert.ok(anchors.at(-1).href.includes('/dummy-safe/file?'));p=h.result.download(true);assert.equal(requests.at(-1).blob,true);requests.at(-1).resolve({blob:{dummy:true},filename:'dummy.json'});await p;h.flush();assert.equal(objectUrls.length,1);assert.equal(anchors.at(-1).download,'dummy.json');
});
await check('F07 stop waiting preserves durable job and resume never recreates it',async()=>{
 const{h,requests}=exportHarness();const p=h.result.prepare({camera_id:1});requests[0].resolve({id:'durable-dummy',status:'queued'});await turn();h.flush();assert.equal(requests[1].path,'/archive/exports/durable-dummy/generate');h.result.stopWaiting();h.flush();assert.equal(h.result.job.id,'durable-dummy');assert.equal(h.result.phase,'waiting_stopped');requests[1].resolve({id:'durable-dummy',status:'done'});await p;const resume=h.result.resume();assert.equal(requests.at(-1).path,'/archive/exports/durable-dummy');requests.at(-1).resolve({id:'durable-dummy',status:'done',manifest_ready:true});await turn();requests.at(-1).resolve({manifest:'dummy'});await resume;h.flush();assert.equal(requests.filter(r=>r.path==='/archive/exports').length,1);assert.equal(h.result.phase,'ready');
});
await check('F07 strict cleanup/setup leaves active hook and one auth listener',async()=>{
 const{h,events,requests}=exportHarness();h.strictCycle();assert.equal(events.get('km-vms-auth-changed').size,1);const p=h.result.loadRecent();requests.at(-1).resolve({items:[]});await p;h.flush();assert.equal(h.result.recentState,'ready');
});
for(const stage of['generate','generate-fallback','resume','resume-fallback','poll-wait','poll-get','manifest'])await check(`F07 auth change at ${stage} await blocks all later stages`,async()=>{
 const{h,requests,token,timers}=exportHarness();
 const resumed=stage.startsWith('resume');
 const p=h.result.prepare({camera_id:1},resumed?{resumeId:'dummy-resume'}:{});
 requests[0].resolve({id:resumed?'dummy-resume':'dummy-job',status:stage==='manifest'?'done':stage.startsWith('poll')||resumed?'running':'queued',can_resume:resumed});
 await turn();
 if(stage.endsWith('fallback')){requests.at(-1).reject(new Error('dummy response lost'));await turn();}
 if(stage==='poll-get'){const[id,fn]=timers.entries().next().value;timers.delete(id);fn();await turn();}
 const count=requests.length;
 token('dummy-B',false);
 if(stage==='poll-wait'){const[id,fn]=timers.entries().next().value;timers.delete(id);fn();}
 else requests.at(-1).resolve(stage==='manifest'?{manifest:'dummy'}:{id:resumed?'dummy-resume':'dummy-job',status:'done'});
 assert.equal(await p,null);assert.equal(requests.length,count);assert.ok(requests.every(r=>r.session==='dummy-A'));
 token('dummy-B');h.flush();assert.equal(h.result.phase,'idle');assert.equal(h.result.job,null);
});
const chronologySource=read('app/chronology/page.js');
const quickStart=chronologySource.indexOf('  async function startQuickDownloadForCamera(');
const quickEnd=chronologySource.indexOf('\n  async function startQuickDownloadForAllCameras',quickStart);
assert.ok(quickStart>=0&&quickEnd>quickStart);
const quickFunction=chronologySource.slice(quickStart,quickEnd);
const ownerGuard=chronologySource.match(/  function isWorkspaceCurrent\([\s\S]*?\n  \}/)?.[0];assert.ok(ownerGuard);
for(const change of['auth','generation','failure-after-auth','none'])await check(`F07 quickdownload ${change} owns token-to-anchor and parent callbacks`,async()=>{
 const e=exportHarness(),mutations=[];
 const ctx={workspaceSession:'dummy-A',workspaceGenerationRef:{current:1},getAuthToken:e.context.getAuthToken,
  visibleTiles:[{id:'tile-A'}],playbackMapRef:{current:{'tile-A':{hasVideo:true,cameraId:'1',segmentId:44,archiveRootId:'root-A'}}},
  quickDownloadTimestamp:()=> '2026-11-05T10:20:00',startChronologyCurrentRecordingDownload:e.context.startChronologyCurrentRecordingDownload,
  quickDownloadCameraNames:{'1':'Dummy camera'},selectedCameraNames:{},TEXT:{camera:'Camera',quickDownloadReady:'started'},
  setDownloadChooserOpen:noop,setQuickDownloadBusy:v=>mutations.push(['busy',v]),setDownloadResults:()=>mutations.push(['results']),setOperationToast:()=>mutations.push(['toast']),
  normalizeChronologyDownloadError:v=>v,showWorkspaceError:()=>mutations.push(['error'])};
 vm.runInNewContext(ownerGuard+'\n'+quickFunction+'\nthis.quick=startQuickDownloadForCamera;',ctx);
 const pending=ctx.quick('1');assert.equal(e.requests.length,1);assert.ok(e.requests[0].path.includes('segment_id=44&archive_root_id=root-A'));
 if(change.includes('auth'))e.token('dummy-B',false);
 if(change==='generation')ctx.workspaceGenerationRef.current++;
 if(change==='failure-after-auth')e.requests[0].reject(new Error('dummy response lost'));
 else e.requests[0].resolve({media_token:'TEST_ONLY',segment_id:44,archive_root_id:'root-A',filename:'dummy.mkv'});
 await pending;
 if(change==='none'){assert.equal(e.anchors.at(-1).clicked,true);assert.ok(e.anchors.at(-1).href.includes('segment_id=44&archive_root_id=root-A'));assert.deepEqual(mutations,[['busy',true],['results'],['toast'],['busy',false]]);}
 else{assert.equal(e.anchors.length,0);assert.deepEqual(mutations,[['busy',true]]);}
});

const archive=read('components/ArchiveTilePlayer.js'),start=archive.indexOf('export default function ArchiveTilePlayer('),end=archive.lastIndexOf('\n  return (');
assert.ok(start>=0&&end>start);
const archiveComponent=archive.slice(start,end).replace('export default ','')+'\nreturn {status};\n}';
function archiveHarness(){
 const requests=[],states=[],timers=new Map(),listeners=new Map();let timer=0;
 const video={src:'',currentSrc:'',currentTime:0,readyState:0,duration:600,paused:true,playCount:0,videoWidth:640,videoHeight:360,
  pause(){this.paused=true;},play(){this.paused=false;this.playCount++;return Promise.resolve();},load(){this.currentSrc=this.src;},removeAttribute(k){if(k==='src'){this.src='';this.currentSrc='';this.readyState=0;}},
  addEventListener(k,f){if(!listeners.has(k))listeners.set(k,new Set());listeners.get(k).add(f);},removeEventListener:(k,f)=>listeners.get(k)?.delete(f),emit(k,ready=null){if(ready!==null)this.readyState=ready;for(const f of[...listeners.get(k)||[]])f();},
 };
 const ctx={URLSearchParams,normalizeVideoDimensions:(width,height)=>({width,height}),selectCompactVideoRenderMode:()=>({renderer:'video'}),issueChronologyMediaToken:(cameraId,relPath,playback)=>new Promise((resolve,reject)=>requests.push({cameraId,relPath,playback,resolve,reject})),
  setTimeout(fn,delay){timers.set(++timer,{fn,delay});return timer;},clearTimeout:id=>timers.delete(id),document:{fullscreenElement:null,addEventListener:noop,removeEventListener:noop},MEDIA_REFRESH_RETRY_LIMIT:1};
 let props={playback:{hasVideo:true,cameraId:'1',relPath:'dummy-A.mkv',playbackKey:'dummy-key-A',offsetSec:10},coordination:null,speed:1,isPlaying:false,tileId:'tile-A',onTilePlaybackState:(tile,operation,state,facts)=>states.push({tile,operation,state,facts})};
 vm.runInNewContext(plain(read('lib/archivePlaybackRecovery.js'))+';this.isArchiveRecoveryHealthy=isArchiveRecoveryHealthy;',ctx);
 const h=hooks(archiveComponent,ctx,'ArchiveTilePlayer',props,slots=>{const refs=slots.filter(s=>s.kind==='ref');refs[0].value.current={contains:()=>false};refs[1].value.current=video;});
 return{h,video,requests,states,timers,listeners,
  update(patch){props={...props,...patch};return h.flush(props);},operation(id,offset=props.playback.offsetSec){props={...props,playback:{...props.playback,offsetSec:offset},coordination:{operationId:id,releaseState:'holding'}};return h.flush(props);}};
}
await check('F08 initial token error recovers SAME key and pauses until barrier release',async()=>{
 const{h,video,requests,operation,update,states}=archiveHarness();requests[0].reject(new Error('dummy network unavailable'));await turn();h.flush();assert.equal(h.result.status,'error');operation('play-recovered');assert.equal(requests.length,2);requests[1].resolve('TEST_ONLY');await turn();h.flush();video.emit('loadedmetadata',3);h.flush();assert.equal(video.currentTime,10);assert.equal(video.playCount,0);assert.equal(h.result.status,'ready');assert.ok(states.some(s=>s.operation==='play-recovered'&&s.state==='ready'));update({coordination:{operationId:'play-recovered',releaseState:'released'},isPlaying:true});assert.equal(video.paused,false);assert.ok(video.playCount>0);
});
await check('F08 pending initial token uses latest same-key offset without parallel requests',async()=>{
 const{h,video,requests,operation,states}=archiveHarness();operation('seek42',42);operation('seek55',55);assert.equal(requests.length,1);requests[0].resolve('TEST_ONLY');await turn();h.flush();video.emit('loadedmetadata',3);h.flush();assert.equal(video.currentTime,55);assert.equal(video.playCount,0);assert.ok(states.some(s=>s.operation==='seek55'&&s.state==='ready'&&s.facts.targetTime===55));operation('healthy70',70);assert.equal(requests.length,1);assert.equal(video.currentTime,70);
});
await check('F08 explicit new operation retries while stable operation never loops',async()=>{
 const{h,requests,operation}=archiveHarness();requests[0].reject(new Error('dummy outage'));await turn();h.flush();operation('retry1',27);requests[1].reject(new Error('still unavailable'));await turn();h.flush();for(let i=0;i<5;i++)h.flush();assert.equal(requests.length,2);operation('retry2',30);assert.equal(requests.length,3);requests[2].resolve('TEST_ONLY');await turn();h.flush();
});
await check('F08 old token after source change or unmount cannot assign src or readiness',async()=>{
 const a=archiveHarness();a.update({playback:{hasVideo:true,cameraId:'2',relPath:'dummy-B.mkv',playbackKey:'dummy-key-B',offsetSec:99},coordination:{operationId:'B',releaseState:'holding'}});assert.equal(a.requests.length,2);a.requests[0].resolve('STALE_TEST_ONLY');await turn();a.h.flush();assert.equal(a.video.src,'');assert.equal(a.states.some(s=>s.state==='ready'),false);a.requests[1].resolve('CURRENT_TEST_ONLY');await turn();a.h.flush();a.video.emit('loadedmetadata',3);a.h.flush();assert.equal(a.video.currentTime,99);assert.ok(a.video.src.includes('CURRENT_TEST_ONLY'));assert.ok(a.states.some(s=>s.operation==='B'&&s.state==='ready'));const b=archiveHarness(),count=b.states.length;b.h.unmount();assert.ok([...b.listeners.values()].every(s=>s.size===0));assert.equal(b.timers.size,0);b.requests[0].resolve('UNMOUNTED_TEST_ONLY');await turn();assert.equal(b.video.src,'');assert.equal(b.states.length,count);
});
await check('F08 automatic media refresh bounded but explicit Play permits new recovery',async()=>{
 const{h,video,requests,operation}=archiveHarness();requests[0].resolve('TEST_ONLY');await turn();h.flush();video.emit('loadedmetadata',3);h.flush();video.emit('stalled');video.emit('stalled');assert.equal(requests.length,2);requests[1].reject(new Error('dummy refresh failure'));await turn();h.flush();video.emit('error');h.flush();assert.equal(requests.length,2);assert.equal(h.result.status,'unsupported');operation('manual-after-exhausted',35);assert.equal(requests.length,3);requests[2].resolve('TEST_ONLY');await turn();h.flush();video.emit('loadedmetadata',3);h.flush();assert.equal(video.currentTime,35);
});
await check('F08 pause during pending token never resumes playback on late metadata',async()=>{
 const{h,video,requests,operation,update,timers}=archiveHarness();operation('pending-play',48);update({coordination:{operationId:'pending-play',releaseState:'cancelled'},isPlaying:false});requests[0].resolve('TEST_ONLY');await turn();h.flush();video.emit('loadedmetadata',3);h.flush();assert.equal(video.currentTime,48);assert.equal(video.playCount,0);assert.equal(video.paused,true);assert.equal(timers.size,0);
});

await check('F09 JSX structure places dialog within shellRef fullscreen root',()=>{
 const source=read('app/chronology/page.js'),jsx=source.slice(source.indexOf('\n  return (\n    <Layout'));assert.ok(jsx.length<source.length);const stack=[],nodes=[],pattern=/<\/?([A-Za-z][\w.]*)\b/g,voids=new Set(['img','input','br','hr','meta','link','source','track']);let match;
 while((match=pattern.exec(jsx))){let e=pattern.lastIndex,braces=0,quote='';for(;e<jsx.length;e++){const c=jsx[e];if(quote){if(c==='\\')e++;else if(c===quote)quote='';continue;}if(c==='"'||c==="'"||c==='`'){quote=c;continue;}if(c==='{')braces++;else if(c==='}')braces--;else if(c==='>'&&braces===0)break;}assert.ok(e<jsx.length);const raw=jsx.slice(match.index,e+1),tag=match[1];pattern.lastIndex=e+1;if(raw.startsWith('</')){assert.equal(stack.pop()?.tag,tag,`unbalanced ${raw}`);continue;}const n={tag,raw,ancestors:[...stack]};nodes.push(n);if(!raw.endsWith('/>')&&!voids.has(tag))stack.push(n);}
 assert.equal(stack.length,0);const shell=nodes.find(n=>/ref=\{shellRef\}/.test(n.raw)),modal=nodes.find(n=>/className="modal archiveExportModal"/.test(n.raw));assert.ok(shell&&modal);assert.ok(modal.ancestors.includes(shell));
});
console.log(JSON.stringify({passed:passed.length,checks:passed,note:'Exact source VM hooks/effects with dummy API/media; no browser or product mutation.'},null,2));
