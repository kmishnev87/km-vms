import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync(new URL('../components/ArchiveTilePlayer.js',import.meta.url),'utf8');
const code=source.slice(source.indexOf('  function safeTargetTimeForVideo'),source.indexOf('  useEffect(() => {',source.indexOf('  function safeTargetTimeForVideo')));
const listeners=new Map(),timers=new Map();let timerId=0;
const video={readyState:0,currentTime:0,duration:600,pause(){},play:async()=>{},
  addEventListener(name,fn){if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name).add(fn);},
  removeEventListener(name,fn){listeners.get(name)?.delete(fn);}};
const ctx={videoRef:{current:video},prepareSequenceRef:{current:0},prepareCancelRef:{current:null},
  coordinationRef:{current:{operationId:'A'}},playIntentRef:{current:false},playbackRateRef:{current:1},
  sampleVideoDimensions(){},setStatus(){},reportTileState(){},
  setTimeout(fn){timers.set(++timerId,fn);return timerId;},clearTimeout(id){timers.delete(id);}};
vm.runInNewContext(code+';this.prepare=prepareVideoAtTarget;',ctx);
for(let i=0;i<500;i++){
  ctx.prepare({targetTime:i});
  assert.equal([...listeners.values()].reduce((n,v)=>n+v.size,0),4);
  assert.equal(timers.size,1);
}
ctx.prepareCancelRef.current();assert.equal(timers.size,0);
assert.equal([...listeners.values()].reduce((n,v)=>n+v.size,0),0);
video.readyState=3;ctx.prepare({targetTime:video.currentTime});
assert.equal(timers.size,0,'synchronous ready does not leave a timer');
assert.equal([...listeners.values()].reduce((n,v)=>n+v.size,0),0);
video.readyState=0;ctx.prepare({targetTime:15});ctx.coordinationRef.current={operationId:'B'};
for(const fn of [...listeners.get('seeked')])fn();
assert.equal(timers.size,0);assert.equal([...listeners.values()].reduce((n,v)=>n+v.size,0),0);
