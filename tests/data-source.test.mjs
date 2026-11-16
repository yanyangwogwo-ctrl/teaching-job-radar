import {test} from 'node:test';
import assert from 'node:assert/strict';
import {loadDataset, loadArchive, needsArchive, lastCrawlAt} from '../dist/data-source.mjs';

const snapshot = {meta:{schema_version:1,generated_at:'2026-09-06T00:00:00Z',last_run:{finished_at:'2026-09-05T00:00:00Z'}},jobs:[],sources:[]};
const live = {...snapshot,meta:{...snapshot.meta,last_run:{finished_at:'2026-09-06T00:00:00Z'}}};
const feed = {enabled:true,url:'https://raw.githubusercontent.com/example/repo/main/dist/data/jobs.json'};
const response = data => ({ok:true,json:async()=>data});

// A private host serves local JSON only to its signed-in viewer. The public
// GitHub feed remains cross-origin and must not receive credentials.
function privateHostFetcher({cloudOffline=false}={}) {
  return async (url, options) => {
    if (url === './feed-config.json' || url === './data/jobs.json') {
      if (options.credentials !== 'same-origin') return {ok:false,status:401};
      return response(url === './feed-config.json' ? feed : snapshot);
    }
    assert.equal(url, feed.url);
    assert.equal(options.credentials, 'omit');
    if (cloudOffline) throw new Error('offline');
    return response(live);
  };
}

test('signed-in viewer can load protected configuration and public cloud data',async()=>{
  const result=await loadDataset(privateHostFetcher());
  assert.equal(result.mode,'cloud');
  assert.equal(result.dataset,live);
});
test('signed-in viewer can use protected snapshot when cloud is unavailable',async()=>{
  const result=await loadDataset(privateHostFetcher({cloudOffline:true}));
  assert.equal(result.mode,'snapshot');
  assert.equal(result.dataset,snapshot);
  assert.match(result.warning,/舊版本/);
});

test('prefer public cloud data',async()=>{
  const calls=[];
  const fetcher=async url=>{calls.push(url);return response(url==='./feed-config.json'?feed:live);};
  const result=await loadDataset(fetcher);
  assert.equal(result.mode,'cloud');
  assert.equal(result.dataset,live);
  assert.equal(calls.includes('./data/jobs.json'),false);
});
test('network failure visibly falls back without changing snapshot time',async()=>{
  const fetcher=async url=>{if(url===feed.url)throw new Error('network');return response(url==='./feed-config.json'?feed:snapshot);};
  const result=await loadDataset(fetcher);
  assert.equal(result.mode,'snapshot');
  assert.match(result.warning,/舊版本/);
  assert.equal(lastCrawlAt(result.dataset),'2026-09-05T00:00:00Z');
});
test('malformed cloud data uses labelled fallback',async()=>{
  const fetcher=async url=>response(url==='./feed-config.json'?feed:url===feed.url?{jobs:[]}:snapshot);
  const result=await loadDataset(fetcher);
  assert.equal(result.mode,'snapshot');
  assert.ok(result.warning);
});
test('both cloud and snapshot failure remains a loading failure',async()=>{
  const fetcher=async url=>{if(url==='./feed-config.json')return response(feed);throw new Error('network');};
  await assert.rejects(()=>loadDataset(fetcher));
});
test('unapproved data host is not fetched',async()=>{
  const calls=[];
  const fetcher=async url=>{calls.push(url);return response(url==='./feed-config.json'?{...feed,url:'https://example.test/data'}:snapshot);};
  const result=await loadDataset(fetcher);
  assert.equal(result.mode,'snapshot');
  assert.equal(calls.some(url=>url.startsWith('https://example.test')),false);
});

const recent = {...snapshot,meta:{...snapshot.meta,feed_scope:'recent',feed_days:30,total_jobs:2,archive:{url:'jobs-archive.json',count:2,generation_id:snapshot.meta.generated_at}},jobs:[{id:'new'}]};
const archive = {...snapshot,meta:{...snapshot.meta,feed_scope:'full'},jobs:[{id:'new'},{id:'old'}]};
test('all dates loads complete history from the same cloud repository without credentials',async()=>{
  assert.equal(needsArchive(recent,{recentDays:'7'}),false);
  assert.equal(needsArchive(recent,{recentDays:'all'}),true);
  const result=await loadArchive(recent,feed.url,async (url,options)=>{
    assert.equal(url,feed.url.replace('jobs.json','jobs-archive.json'));
    assert.equal(options.credentials,'omit');
    return response(archive);
  });
  assert.deepEqual(result.jobs.map(job=>job.id),['new','old']);
  assert.equal(needsArchive(result,{recentDays:'all'}),false);
});
test('snapshot history remains on the same Site session and legacy complete feeds need no archive',async()=>{
  await loadArchive(recent,'./data/jobs.json',async(url,options)=>{
    assert.equal(url,'./data/jobs-archive.json');
    assert.equal(options.credentials,'same-origin');
    return response(archive);
  });
  assert.equal(await loadArchive(snapshot,'./data/jobs.json',()=>assert.fail('unneeded fetch')),snapshot);
});
test('archive failures and inconsistent generations stay visible instead of replacing current jobs',async()=>{
  await assert.rejects(()=>loadArchive(recent,feed.url,async()=>{throw new Error('offline');}));
  for (const invalid of [
    {...archive,meta:{...archive.meta,generated_at:'different'}},
    {...archive,jobs:[{id:'old'}]},
    {...archive,jobs:[{id:'old'},{id:'old'}]},
    {...archive,jobs:[{id:'unrelated'},{id:'old'}]}
  ]) await assert.rejects(()=>loadArchive(recent,feed.url,async()=>response(invalid)));
  assert.deepEqual(recent.jobs,[{id:'new'}]);
});
test('archive location cannot redirect outside the approved feed directory',async()=>{
  for (const url of ['https://example.com/jobs-archive.json','../jobs-archive.json','//raw.githubusercontent.com/other/repo/main/jobs-archive.json']) {
    await assert.rejects(()=>loadArchive({...recent,meta:{...recent.meta,archive:{...recent.meta.archive,url}}},feed.url,()=>assert.fail('unsafe fetch')));
  }
});
