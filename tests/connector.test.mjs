import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runBatch, unwrap, collect} from '../adapters/connector.mjs';

test('batch retains each outcome and bounds concurrency', async () => {
  let active=0, maximum=0;
  const saved=[];
  const jobs=Array.from({length:20},(_,i)=>({tool:'read',args:{i},key:String(i)}));
  await runBatch(jobs,async (_,args)=>{
    active++; maximum=Math.max(maximum,active);
    await new Promise(r=>setTimeout(r,1)); active--;
    if(args.i===7)throw new Error('synthetic denial');
    return {structuredContent:{data:{gid:String(args.i)}}};
  },r=>saved.push(r),3);
  assert.equal(saved.length,20);
  assert.equal(saved.filter(r=>r.failure).length,1);
  assert.ok(maximum<=3);
});

test('unwrap prefers structured result and parses JSON fallback',()=>{
  assert.equal(unwrap({structuredContent:{data:1}}).data,1);
  assert.equal(unwrap({content:[{type:'text',text:'{"data":2}'}]}).data,2);
  assert.throws(()=>unwrap({}));
});

test('collector follows pages, archived projects, recursive tasks and caps report',async()=>{
  const records=[];
  const call=async (name,args)=>{
    let data=[], next_page=null;
    if(name.endsWith('get_me'))data={gid:'1'};
    if(name.endsWith('get_projects')){
      if(!args.offset){data=[{gid:args.archived?'3':'2'}];next_page={offset:'next'};}
    }
    if(name.endsWith('get_project'))data={gid:args.project_id,sections:[]};
    if(name.endsWith('get_tasks'))data=[{gid:'4'}];
    if(name.endsWith('get_task'))data={gid:args.task_id,subtasks:args.task_id==='4'?[{gid:'5'}]:[],dependencies:args.task_id==='5'?[{gid:'4'}]:[]};
    return {structuredContent:{data,next_page}};
  };
  const report=await collect(call,r=>records.push(r));
  assert.equal(report.projects,2);
  assert.equal(report.tasks,2);
  assert.equal(report.status,'partial');
  assert.equal(records.filter(r=>r.tool.endsWith('get_task')).length,2);
  assert.ok(report.limitations.some(x=>x.includes('50')));
});
