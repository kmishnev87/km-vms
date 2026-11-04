import assert from 'node:assert/strict';
import test from 'node:test';
import { createPlaybackCoordinator } from '../lib/chronologyPlaybackCoordinator.js';
import { layoutTiles, normalizeTile, backendPayload } from '../lib/chronologyWorkspaceLayout.js';

function harness() {
  const pending = new Map(), all = [], resumes = [];
  let clock = 1000, serial = 0;
  const store = createPlaybackCoordinator({ now: () => clock++, onResume: () => resumes.push('resume'),
    schedule: (run, delay) => { const id = ++serial; pending.set(id, {run, delay}); all.push(run); return id; },
    cancelTimer: id => pending.delete(id) });
  return { store, pending, all, resumes };
}

test('only the current seek can release its barrier or timers', () => {
  const h = harness();
  const old = h.store.start({targetTs:'old', shouldResume:true, expectedTileIds:['a']});
  const current = h.store.start({targetTs:'new', shouldResume:true, expectedTileIds:['b','c']});
  assert.equal(h.pending.size, 2);
  assert.equal(h.store.release(old.id), false);
  assert.equal(h.pending.size, 2);
  h.all[0](); h.all[1]();
  assert.equal(h.store.getSnapshot().id, current.id);
  assert.equal(h.store.getSnapshot().releaseState, 'holding');
  assert.equal(h.store.updateTile('a', old.id, 'ready'), false);
  h.store.updateTile('b', current.id, 'ready');
  assert.equal(h.store.getSnapshot().releaseState, 'holding');
  h.store.updateTile('c', current.id, 'empty');
  assert.equal(h.store.getSnapshot().releaseState, 'released');
  assert.deepEqual(h.resumes, ['resume']);
  assert.equal(h.pending.size, 0);
  h.store.release(current.id);
  assert.equal(h.resumes.length, 1);
});

test('timeouts are bounded and preserve ready tiles', () => {
  const h = harness();
  const operation = h.store.start({targetTs:'now', shouldResume:false, expectedTileIds:['a','b']});
  h.store.updateTile('a', operation.id, 'ready', {position:42});
  h.all[0]();
  assert.equal(h.store.getSnapshot().state, 'partial');
  assert.equal(h.store.getSnapshot().releaseState, 'holding');
  h.all[1]();
  assert.equal(h.store.getSnapshot().tileStates.a.state, 'ready');
  assert.equal(h.store.getSnapshot().tileStates.b.state, 'timeout');
  assert.equal(h.store.getSnapshot().releaseState, 'partial');
  assert.deepEqual(h.resumes, []);
});

test('pause and unmount reject late callbacks; repeated seeks keep two timers at most', () => {
  const h = harness();
  let operation;
  for (let i=0; i<1000; i++) {
    operation=h.store.start({targetTs:String(i), shouldResume:true, expectedTileIds:['a']});
    assert.equal(h.pending.size, 2);
  }
  h.store.cancel('paused');
  h.store.updateTile('a', operation.id, 'ready');
  h.all.at(-1)();
  assert.equal(h.store.getSnapshot().releaseState, 'cancelled');
  assert.equal(h.pending.size, 0);
  assert.deepEqual(h.resumes, []);
  h.store.dispose();
  assert.equal(h.store.start({targetTs:'late',expectedTileIds:[]}), null);
  h.store.activate();
  assert.equal(h.store.start({targetTs:'remount',expectedTileIds:[]}).releaseState, 'released');
});

test('extracted layout preserves normalized bounds, deduplication and backend shape', () => {
  const old = normalizeTile({id:'a', cameraId:'1',x:10,y:20,w:520,h:300});
  const tiles=layoutTiles([old, {...old}, {id:'b',cameraId:'2',wPct:.3,hPct:.4,xPct:.4,yPct:.2}], {width:1600,height:900});
  assert.equal(tiles.length, 2);
  for (const tile of tiles) {
    assert.ok(tile.xPct >= 0 && tile.yPct >= 0);
    assert.ok(tile.xPct+tile.wPct <= 1 && tile.yPct+tile.hPct <= 1);
  }
  const payload=backendPayload(tiles,['2','1','2']);
  assert.equal(payload.tiles.length,2);
  assert.deepEqual(payload.sidebarCameraOrder,['2','1']);
});
