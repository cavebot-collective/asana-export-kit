const test = require('node:test');
const assert = require('node:assert/strict');
const {build, command, categories} = require('../asana_export_kit/web/builder.js');
function config() {return {output:'../archive',rpm:120,auth:'prompt',timeEntries:true,...Object.fromEntries(categories.map(k=>[k,true]))};}
test('capture checkboxes emit implemented switches',()=>{
  const args=build({...config(),stories:false,downloads:true,extended:true,timeEntries:false});
  assert.ok(args.includes('--prompt-token')); assert.ok(args.includes('--download-attachments'));
  assert.ok(args.includes('--extended')); assert.ok(args.includes('--skip-time-entries'));
  assert.equal(args[args.indexOf('--omit')+1],'stories');
});
test('watch owns output and prompts once, no OAuth scheduling',()=>{
  const c={...config(),schedule:true,interval:60,remote:'drive:archives'};
  const args=build(c); assert.equal(args[3],'watch'); assert.ok(args.includes('--root')); assert.ok(args.includes('--'));
  assert.ok(!args.includes('--out')); assert.throws(()=>build({...c,auth:'oauth'}));
});
test('quote metacharacters and reject line breaks',()=>{
  assert.equal(command(['echo',"x'; $(test)"],'powershell'),"& 'echo' 'x''; $(test)'");
  assert.equal(command(["a'b"],'posix'),"'a'\"'\"'b'");
  assert.throws(()=>command(['line\nbreak']));
  assert.throws(()=>build({...config(),workspaces:'123; rm'}));
});
