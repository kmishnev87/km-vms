import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const source=fs.readFileSync(new URL("../lib/liveMsePlayer.js",import.meta.url),"utf8");
const {createLiveMsePlayer}=await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const settle=async()=>{for(let n=0;n<8;n++)await Promise.resolve();};

function harness({supported=true,https=false}={}) {
  const sockets=[],medias=[],revoked=[],failures=[],playing=[],audio=[],timers=new Map();
  let timerId=0,tokenCalls=0;
  class Video extends EventTarget {
    constructor(){super();this.attribute=null;this.currentTime=0;this.playbackRate=1;this.paused=true;this.range=null;}
    set src(v){this.attribute=v;} get src(){return this.attribute;}
    getAttribute(){return this.attribute;} removeAttribute(){this.attribute=null;}
    load(){} pause(){this.paused=true;}
    play(){if(this.paused){this.paused=false;queueMicrotask(()=>this.dispatchEvent(new Event("playing")));}return Promise.resolve();}
    get buffered(){const range=this.range;return {length:range?1:0,start:()=>range.start,end:()=>range.end};}
  }
  const video=new Video();
  class Buffer extends EventTarget {
    updating=false;chunks=[];
    appendBuffer(chunk){this.chunks.push(chunk);this.updating=true;queueMicrotask(()=>{this.updating=false;this.dispatchEvent(new Event("updateend"));});}
    remove(_,end){if(video.range)video.range.start=end;this.updating=true;queueMicrotask(()=>{this.updating=false;this.dispatchEvent(new Event("updateend"));});}
  }
  class Media extends EventTarget {
    static isTypeSupported(){return supported;}
    constructor(){super();this.readyState="closed";medias.push(this);queueMicrotask(()=>{this.readyState="open";this.dispatchEvent(new Event("sourceopen"));});}
    addSourceBuffer(mime){this.mime=mime;this.buffer=new Buffer();return this.buffer;}
  }
  class Socket {
    constructor(url){this.url=url;this.readyState=0;this.sent=[];sockets.push(this);}
    open(){this.readyState=1;this.onopen?.();}
    send(v){this.sent.push(v);}
    close(){this.readyState=3;this.onclose?.();}
    message(v){this.onmessage?.({data:v});}
  }
  const player=createLiveMsePlayer({video,cameraId:7,stream:"sub",MediaSourceClass:Media,WebSocketClass:Socket,
    location:{href:`${https?"https":"http"}://vms.example.test:8088/live`,protocol:https?"https:":"http:"},
    urls:{createObjectURL:()=>`blob:unit-${medias.length}`,revokeObjectURL:u=>revoked.push(u)},
    requestToken:async()=>{tokenCalls++;return {mediaToken:"synthetic-private-media-token",expiresIn:300};},
    onFailure:e=>failures.push(e),onPlaying:()=>playing.push(true),onAudio:i=>audio.push(i),
    schedule:(run,delay)=>{const id=++timerId;timers.set(id,{run,delay});return id;},unschedule:id=>timers.delete(id),
  });
  return {player,video,sockets,medias,revoked,failures,playing,audio,timers,tokens:()=>tokenCalls,
    runTimer:async delay=>{const [id,timer]=[...timers].find(([,t])=>t.delay===delay);timers.delete(id);await timer.run();await settle();}};
}

test("WebSocket is same-origin and has no credential query, including HTTPS",async()=>{
  for(const https of [false,true]){
    const h=harness({https});await h.player.start();await settle();
    assert.equal(h.sockets[0].url,`${https?"wss":"ws"}://vms.example.test:8088/api/live/7/sub/mse`);
    assert(!h.sockets[0].url.includes("token"));
    h.player.destroy();assert.equal(h.sockets[0].readyState,3);assert.equal(h.timers.size,0);
  }
});
test("initialization and small fragments are appended in order and played",async()=>{
  const h=harness();await h.player.start();await settle();const socket=h.sockets[0];socket.open();
  socket.message(JSON.stringify({type:"init",mime:'video/mp4; codecs="avc1.64001F"',audio_available:true}));
  const init=new Uint8Array([1,2]).buffer,fragment=new Uint8Array([3,4]).buffer;
  socket.message(init);socket.message(fragment);await settle();
  assert.deepEqual(h.medias[0].buffer.chunks,[init,fragment]);assert(h.playing.length);assert.equal(h.audio[0].audio_available,true);
  assert.equal(h.failures.length,0);h.player.destroy();assert.equal(h.timers.size,0);
});
test("growing playback delay catches up instead of accumulating seconds",async()=>{
  const h=harness();await h.player.start();await settle();const socket=h.sockets[0];socket.open();
  socket.message(JSON.stringify({type:"init",mime:'video/mp4; codecs="avc1.64001F"'}));
  h.video.range={start:0,end:10};h.video.currentTime=0;
  socket.message(new ArrayBuffer(8));await settle();
  assert.equal(h.video.currentTime,9.8);h.player.destroy();assert.equal(h.video.playbackRate,1);
});
test("media authority renews on the existing socket without changing video source",async()=>{
  const h=harness();await h.player.start();await settle();const socket=h.sockets[0];socket.open();const src=h.video.src;
  await h.runTimer(210000);
  assert.equal(h.sockets.length,1);assert.equal(h.video.src,src);assert.equal(h.tokens(),2);
  assert.deepEqual(JSON.parse(socket.sent[0]),{type:"refresh",media_token:"synthetic-private-media-token"});
  h.player.destroy();assert.equal(h.timers.size,0);
});
test("unsupported codec fails once and releases socket and object URL",async()=>{
  const h=harness({supported:false});await h.player.start();await settle();h.sockets[0].open();
  h.sockets[0].message(JSON.stringify({type:"init",mime:'video/mp4; codecs="unsupported"'}));
  assert.equal(h.failures.length,1);assert.equal(h.failures[0].code,"mse_codec_unsupported");
  assert.equal(h.sockets[0].readyState,3);assert.equal(h.revoked.length,1);assert.equal(h.timers.size,0);
});
test("destroy cancels reconnection and late work cannot reopen a source",async()=>{
  const h=harness();await h.player.start();await settle();h.sockets[0].close();
  assert([...h.timers.values()].some(t=>t.delay===1000));
  const old=[...h.timers.values()].find(t=>t.delay===1000).run;
  h.player.destroy();await old();await settle();
  assert.equal(h.sockets.length,1);assert.equal(h.timers.size,0);assert.equal(h.failures.length,0);
});
