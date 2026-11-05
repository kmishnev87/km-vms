import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const read = p => fs.readFileSync(new URL('../' + p, import.meta.url), 'utf8');
const plain = s => s.replace(/^import\b[\s\S]*?;\r?\n/gm, '').replaceAll('export default ', '').replaceAll('export ', '');
function exact(source, name) {
  const start = source.search(new RegExp('^  (?:async )?function ' + name + '\\(', 'm'));
  const end = source.indexOf('\n  }', start);
  assert.ok(start >= 0 && end > start, name);
  return source.slice(start, end + 4);
}
const storage = () => { const map = new Map(); return {getItem:k=>map.get(k)??null,setItem:(k,v)=>map.set(k,String(v)),removeItem:k=>map.delete(k)}; };

{
  const localStorage = storage(), firstSession = storage(), otherSession = storage();
  const events = [], observations = [];
  const context = sessionStorage => ({Date,URLSearchParams,process:{env:{}},localStorage,sessionStorage,
    CustomEvent:class{},window:{dispatchEvent:()=>events.push('auth')},fetch:()=>{throw Error('no network');}});
  const first=context(firstSession), other=context(otherSession);
  const source=plain(read('lib/api.js'))+'\nthis.save=saveAuthToken;this.read=getAuthToken;';
  vm.runInNewContext(source,first);vm.runInNewContext(source,other);
  const expiresAt='2099-01-01T00:00:00Z';
  first.save('dummy-A',{persistent:true,expiresAt});
  assert.equal(other.read(),'dummy-A');
  events.length=0;
  for(const method of ['setItem','removeItem']) {
    const original=localStorage[method];
    localStorage[method]=(...args)=>{original(...args);observations.push(other.read());};
  }
  first.save('dummy-B',{persistent:true,expiresAt});
  assert.equal(first.read(),'dummy-B');assert.equal(other.read(),'dummy-B');
  assert.ok(observations.every(value=>['dummy-A','dummy-B'].includes(value)),
    'a replacing persistent login must not publish a transient no-token state');
  assert.deepEqual(events,['auth'],'one completed session change, no intermediate logout');
}

function deletionHarness() {
  const calls = [], applied = [];
  const localStorage = storage(), sessionStorage = storage();
  const ctx = {Date, URLSearchParams, process:{env:{}}, localStorage, sessionStorage,
    CustomEvent:class{}, window:{localStorage,sessionStorage,dispatchEvent:()=>{}},
    fetch:async(url,options)=>{calls.push({url,options});return {ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({ok:true,status:'completed'})};},
    setBusy:()=>{},setError:()=>{},setDeleteDialog:value=>{ctx.dialog=value;},
    recordingsActionReady:true,canDelete:true,newClientOperationId:()=> 'dummy-owned-operation',
    recordingIdentityQuery:()=> 'segment_id=7&archive_root_id=owned',formatSizeBytes:()=> '1 KB',
    t:{deleteOneTitle:'Delete?',deleteOneMessage:'{name}',size:'Size',remove:'Delete',cancel:'Cancel'},
    refresh:async()=>applied.push('refresh'),showDeletionResult:()=>applied.push('result'),showDeletionError:()=>applied.push('error'),
  };
  const page = read('app/recordings/page.js');
  vm.runInNewContext(plain(read('lib/api.js')) + '\n' + ['executeExactDelete','handleDeleteOne'].map(n=>exact(page,n)).join('\n') + '\nthis.begin=handleDeleteOne;this.setSession=saveAuthToken;', ctx);
  ctx.setSession('dummy-A');
  return {ctx,calls,applied};
}

{
  const h = deletionHarness();
  h.ctx.begin({filename:'owned-A.mkv'});
  const confirmation = h.ctx.dialog;
  h.ctx.setSession('dummy-B');
  await confirmation.onConfirm();
  assert.equal(h.calls.length,0,'old intent must not issue DELETE under B');
  assert.deepEqual(h.applied,[]);
}
{
  const h = deletionHarness();
  h.ctx.begin({filename:'owned-A.mkv'});
  await h.ctx.dialog.onConfirm();
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].options.headers.Authorization,'Bearer dummy-A');
  assert.deepEqual(h.applied,['refresh','result']);
}
{
  const h = deletionHarness();
  let resolve;
  h.ctx.fetch = (url,options)=>{h.calls.push({url,options});return new Promise(r=>{resolve=r;});};
  h.ctx.begin({filename:'owned-A.mkv'});
  const pending = h.ctx.dialog.onConfirm();
  h.ctx.setSession('dummy-B');
  resolve({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>({ok:true,status:'completed'})});
  await pending;
  assert.equal(h.calls.length,1,'already accepted work is not submitted again');
  assert.deepEqual(h.applied,[],'late A result must not refresh/mutate B page');
}

{
  let actor = {id:1,username:'A'};
  const ctx = {useState:v=>[v,()=>{}],useRef:v=>({current:v}),useEffect:()=>{},
    useCurrentUser:()=>({currentUser:actor}),languageOf:()=> 'ru'};
  vm.runInNewContext(plain(read('lib/useSettingsFormLoader.js'))+'\nthis.loadForm=useSettingsFormLoader;',ctx);
  assert.equal(ctx.loadForm({}).currentUser.id,1);
  actor={id:2,username:'B'};
  assert.equal(ctx.loadForm({}).currentUser.id,2,'settings uses shared current-user state, not mount-only actor');
}

{
  const source = read('lib/useSettingsUsersController.js');
  let actor = 'A', resolve;
  const effects = [];
  const ctx={captureAuthSession:()=>{const owner=actor;return()=>owner===actor;},
    userModal:{mode:'edit',id:1,username:'A',display_name:'A',role:'owner',is_active:true,password:'dummy-new',password_confirm:'dummy-new',current_password:'dummy-current'},
    currentUser:{id:1,username:'A',role:'owner'},userBusyRef:{current:false},
    setUserBusy:()=>{},setUserModal:()=>effects.push('modal'),t:{toasts:{}},
    apiFetch:()=>new Promise(r=>{resolve=r;}),showToast:()=>effects.push('toast'),
    clearAuthToken:()=>effects.push('logout'),router:{replace:()=>effects.push('route')},
    window:{sessionStorage:{setItem:()=>effects.push('notice')}},
  };
  vm.runInNewContext(exact(source,'submitUserModal')+'\nthis.submit=submitUserModal;',ctx);
  const pending=ctx.submit({preventDefault:()=>{}});
  actor='B';resolve({});await pending;
  assert.deepEqual(effects,[],'late self-credential response must not log out a different new session');
}

const root = read('app/layout.js'), boundary=read('components/AuthSessionBoundary.js');
assert.match(root,/<AuthSessionBoundary>\{children\}<\/AuthSessionBoundary>/);
assert.match(boundary,/key=\{owner\.current\.generation\}/);
assert.doesNotMatch(boundary,/key=\{session\}/,'tokens must not be used as rendered keys');
console.log('Audit4 session ownership: stale confirm, late result, current user and credential/logout guards PASS');
