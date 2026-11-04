import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../app/setup/page.js', import.meta.url), 'utf8');
const draftRead = source.indexOf('const draft = safeReadDraft();');
const effectStart = source.lastIndexOf('useEffect(() => {', draftRead);
const effect = source.slice(source.indexOf('{', effectStart) + 1, source.indexOf('}, [setLocale]);', draftRead));
// Execute the real hydration callback, including a repeat with a changed
// provider callback. A language render must never restore the form again.
let form = { username: 'initial', password: '', password_confirm: '' };
let language = 'ru';
let restorations = 0;
const context = {
  draftRestored: { current: false },
  safeReadDraft: () => { restorations += 1; return { language: 'ru', step: 2, form: { username: 'owner' } }; },
  setForm: fn => { form = fn(form); }, setLanguage: value => { language = value; },
  setLocale: () => {}, setDraftLoaded: () => {}, setStep: () => {},
  setupTimezoneValue: value => value, normalizeLocale: value => value, restoreDraftStep: value => value,
};
vm.runInNewContext(`function hydrate() {${effect}}; this.hydrate = hydrate;`, context);
context.hydrate();
form.password = 'memory-only-test-value';
language = 'en';
context.setLocale = () => {};
context.hydrate();
assert.equal(restorations, 1);
assert.equal(form.password, 'memory-only-test-value');
assert.equal(language, 'en');
