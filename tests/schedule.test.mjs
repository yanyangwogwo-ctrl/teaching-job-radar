import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {parseSchedule,replaceSchedule,loadSchedule} from '../dist/schedule.mjs';

const workflow="name: Daily monitor\non:\n  schedule:\n    - cron: '17 0 * * *' # 08:17 Hong Kong\n  workflow_dispatch:\npermissions:\n  contents: write\njobs:\n  monitor:\n    runs-on: ubuntu-latest\n";
test('repository workflow remains a supported daily schedule',()=>{
  const current=fs.readFileSync(new URL('../.github/workflows/daily.yml',import.meta.url),'utf8');
  assert.match(parseSchedule(current).time,/^\d{2}:\d{2}$/);
});
test('current UTC schedule is displayed in Hong Kong time',()=>{
  assert.equal(parseSchedule(workflow).time,'08:17');
});
test('time selection replaces only the cron line, including previous UTC day',()=>{
  for(const [time,cron] of [['00:05','5 16'],['08:17','17 0'],['23:59','59 15']]){
    const next=replaceSchedule(workflow,time);
    assert.equal(parseSchedule(next).time,time);
    assert.ok(next.includes(`cron: '${cron} * * *'`));
    assert.deepEqual(next.split('\n').filter(l=>!l.includes('- cron:')),workflow.split('\n').filter(l=>!l.includes('- cron:')));
  }
});
test('local timezone schedule is not shifted twice',()=>{
  const local=workflow.replace("'17 0 * * *'","'17 8 * * *'").replace('  workflow_dispatch:', '      timezone: "Asia/Hong_Kong" # local time\n  workflow_dispatch:');
  assert.equal(parseSchedule(local).time,'08:17');
  assert.match(replaceSchedule(local,'21:30'),/cron: '30 21 \* \* \*'/);
});
test('unsupported schedules and invalid input cannot generate changes',()=>{
  for(const value of ['', '24:00','9:00','00:60','08:00\npermissions: write-all'])assert.throws(()=>replaceSchedule(workflow,value));
  assert.throws(()=>replaceSchedule(workflow.replace('17 0 * * *','*/5 * * * *'),'09:00'));
  assert.throws(()=>parseSchedule(workflow.replace('  workflow_dispatch:', "    - cron: '0 12 * * *'\n  workflow_dispatch:")));
  assert.throws(()=>parseSchedule(workflow.replace('  workflow_dispatch:', '      timezone: "America/New_York"\n  workflow_dispatch:')));
});
test('blank lines and comments cannot hide a timezone or second schedule',()=>{
  const local=workflow.replace("'17 0 * * *'","'17 8 * * *'").replace('  workflow_dispatch:', '\n# timezone note\n      timezone: Asia/Hong_Kong\n  workflow_dispatch:');
  assert.equal(parseSchedule(local).time,'08:17');
  assert.equal(parseSchedule(replaceSchedule(local,'09:00')).time,'09:00');
  assert.match(replaceSchedule(local,'09:00'),/cron: '0 9 \* \* \*'/);
  const double=workflow.replace('  workflow_dispatch:', "\n# second entry\n    - cron: '0 12 * * *'\n  workflow_dispatch:");
  assert.throws(()=>replaceSchedule(double,'09:00'));
});
test('cloud confirmation comes from a fresh read, not the chosen time',async()=>{
  const fetcher=async(url,options)=>{assert.equal(options.credentials,'omit');return {ok:true,text:async()=>workflow};};
  const result=await loadSchedule(fetcher);
  assert.equal(result.time,'08:17');
  assert.equal(result.text,workflow);
  await assert.rejects(()=>loadSchedule(async()=>({ok:false,status:503})));
});
