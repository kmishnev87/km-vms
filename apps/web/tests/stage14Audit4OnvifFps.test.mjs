import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source=fs.readFileSync(new URL('../lib/cameraOnvifConfig.js',import.meta.url),'utf8');
const ctx={};
vm.runInNewContext(source.replaceAll('export ','')+'\nthis.invalid=invalidOnvifConfigField;this.change=changeOnvifConfigField;',ctx);

for(const matrix of [false,true]) {
  const fields={fps:{writable:true,readable:true,number_type:'integer',options:[],range:{min:1,max:30,step:1}}};
  const baseline={codec:'H264',resolution:'1920x1080',fps:25,supported:fields,
    options_by_codec:matrix?{H264:{fields,by_resolution:{'1920x1080':fields}}}:{}};
  const fraction=ctx.change(baseline,'fps','12.5');
  assert.equal(ctx.invalid(fraction,baseline),'fps','Media1 fractions must disable Apply before a camera write');
  assert.equal(ctx.invalid(ctx.change(baseline,'fps','12'),baseline),null);
  const floatFields={fps:{...fields.fps,number_type:'float',range:{min:1,max:30}}};
  const media2={...baseline,supported:floatFields,options_by_codec:matrix?{H264:{fields:floatFields}}:{}};
  assert.equal(ctx.invalid(ctx.change(media2,'fps','29.97'),media2),null,'Media2 fractions remain supported');
}
console.log('Media1 integer FPS validates in direct and codec/resolution metadata; Media2 stays fractional PASS');
