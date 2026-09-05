import {test} from 'node:test';
import assert from 'node:assert/strict';
import {loadDataset, lastCrawlAt} from '../dist/data-source.mjs';

const snapshot = {meta:{schema_version:1,generated_at:'2026-09-06T00:00:00Z',last_run:{finished_at:'2026-09-05T00:00:00Z'}},jobs:[],sources:[]};
const live = {...snapshot,meta:{...snapshot.meta,last_run:{finished_at:'2026-09-06T00:00:00Z'}}};
const feed = {enabled:true,url:'https://raw.githubusercontent.com/example/repo/main/dist/data/jobs.json'};
const response = data => ({ok:true,json:async()=>data});

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
