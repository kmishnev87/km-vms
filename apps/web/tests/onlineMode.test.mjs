import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const load = async path => import(`data:text/javascript;base64,${Buffer.from(fs.readFileSync(new URL(path,import.meta.url),"utf8")).toString("base64")}`);
const {normalizeOnlineMode,wantsFastOnline,isMseCompatibilityFailure,onlineModeCopy}=await load("../lib/onlineMode.js");
test("stable remains the old default and its PTZ override is unchanged",()=>{
  assert.equal(normalizeOnlineMode(undefined),"stable"); assert.equal(normalizeOnlineMode("future"),"stable");
  assert.equal(wantsFastOnline("stable",false),false);assert.equal(wantsFastOnline("stable",true),true);
  for(const ptz of [true,false]) assert.equal(wantsFastOnline("fast",ptz),true);
});
test("only confirmed compatibility failures carry the HLS incompatibility explanation",()=>{
  for(const code of ["mse_browser_unsupported","mse_codec_unsupported","mse_decode_unsupported"]) assert(isMseCompatibilityFailure({code}));
  for(const code of ["mse_connection_closed","mse_stream_failed","mse_slow_viewer","mse_access_denied","mse_buffer_failed"]) assert(!isMseCompatibilityFailure({code}));
});
test("all three languages have mode, tooltip and fallback copy",()=>{
  for(const lang of ["ru","en","zh-CN"]) for(const value of Object.values(onlineModeCopy(lang))) assert(value.length>0);
});
test("settings use the existing row, live artwork and fast-first options after recording format",()=>{
  const page=fs.readFileSync(new URL("../app/settings/page.js",import.meta.url),"utf8");
  const row=page.slice(page.indexOf('data-settings-online-mode'),page.indexOf('settingsRow settingsRowHardware'));
  assert(row.includes('/assets/icons/ui/live.png'));
  assert(row.indexOf('value="fast"')<row.indexOf('value="stable"'));
  assert(page.indexOf('id="settings-recording"')<page.indexOf('id="settings-online-mode"'));
  assert(row.includes('InfoTip text={onlineCopy.help} paragraphs'));
  assert(row.includes('draft.online_mode === "fast" ? onlineCopy.fastSummary : onlineCopy.stableSummary'));
});
test("mode explanations remain two separate paragraphs in every language",()=>{
  for(const lang of ["ru","en","zh-CN"]) {
    const copy=onlineModeCopy(lang);
    const paragraphs=copy.help.split("\n\n");
    assert.equal(paragraphs.length,2);
    assert(paragraphs[0].startsWith(copy.fast));
    assert(paragraphs[1].startsWith(copy.stable));
    assert(copy.fastSummary.length>0 && copy.stableSummary.length>0);
    assert.notEqual(copy.fastSummary,copy.stableSummary);
  }
  const page=fs.readFileSync(new URL("../app/settings/page.js",import.meta.url),"utf8");
  const css=fs.readFileSync(new URL("../app/styles/20-05-settings-security-format.css",import.meta.url),"utf8");
  assert(page.includes('paragraphs ? " settingsInfoBubbleParagraphs" : ""'));
  assert.match(css,/\.settingsInfoBubble\.settingsInfoBubbleParagraphs\s*\{\s*white-space:\s*pre-line;/);
});
