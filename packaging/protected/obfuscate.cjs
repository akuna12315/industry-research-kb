'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const work = path.join(root, 'work/protected');
const obfuscator = require(path.join(root, 'work/protected-tools/frontend/node_modules/javascript-obfuscator'));
const options = {
  compact:true, target:'browser-no-eval', seed:20261023,
  identifierNamesGenerator:'hexadecimal', renameGlobals:false, renameProperties:false,
  stringArray:true, stringArrayThreshold:0.85, stringArrayEncoding:['rc4'],
  stringArrayRotate:true, stringArrayShuffle:true, stringArrayIndexShift:true,
  controlFlowFlattening:true, controlFlowFlatteningThreshold:0.2,
  deadCodeInjection:false, debugProtection:false, selfDefending:false,
  sourceMap:false, disableConsoleOutput:false, showObfuscatorProMessage:false
};
const report = {tool:'javascript-obfuscator', version:require(path.join(root,'work/protected-tools/frontend/node_modules/javascript-obfuscator/package.json')).version, options, files:{}};
for(const name of ['app.js','research-charts.js']){
  const original = fs.readFileSync(path.join(root,'web/static',name),'utf8');
  const result = obfuscator.obfuscate(original,{...options,identifiersPrefix:name==='app.js'?'_yzA':'_yzC'}).getObfuscatedCode();
  fs.writeFileSync(path.join(work,'source/web/static',name),result);
  report.files[name] = {original_sha256:crypto.createHash('sha256').update(original).digest('hex'),protected_sha256:crypto.createHash('sha256').update(result).digest('hex'),original_bytes:Buffer.byteLength(original),protected_bytes:Buffer.byteLength(result)};
}
fs.writeFileSync(path.join(work,'frontend-protection.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify(report.files));
