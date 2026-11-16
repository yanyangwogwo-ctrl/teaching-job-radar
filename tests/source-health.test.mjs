import {test} from 'node:test';
import assert from 'node:assert/strict';
import {sourceStatus, sourceRank, detailCoverageLabel} from '../dist/source-health.mjs';
const now=Date.parse('2026-11-17T00:00:00Z');
const source={enabled:true,status:'partial',capability:'list_only',severity:'warning',listing_complete:true,last_listing_success:'2026-11-16T12:00:00Z'};
test('source limitations stay visible while true faults sort first',()=>{
  assert.equal(sourceStatus(source,36,now),'limited');
  const failure={...source,severity:'error',listing_complete:false};
  assert.equal(sourceStatus(failure,36,now),'error');
  assert.ok(sourceRank(failure,36,now)<sourceRank(source,36,now));
  assert.equal(sourceStatus({...source,capability:'manual'},36,now),'manual');
  assert.equal(sourceStatus({...source,capability:'pending'},36,now),'pending');
});
test('listing freshness does not depend on ever having complete details',()=>{
  assert.equal(sourceStatus({...source,last_success:null},36,now),'limited');
  assert.equal(sourceStatus({...source,last_listing_success:'2026-11-14T00:00:00Z'},36,now),'stale');
  assert.equal(sourceStatus({enabled:true,status:'ok',last_success:'2026-11-14T00:00:00Z'},36,now),'stale');
  assert.equal(sourceStatus({enabled:true,status:'partial'},36,now),'partial');
});
test('coverage distinguishes no jobs from missing measurement',()=>{
  assert.equal(detailCoverageLabel({detail_complete_count:5,detail_total_count:29}),'5 / 29 則');
  assert.equal(detailCoverageLabel({detail_complete_count:0,detail_total_count:0,listing_complete:true}),'本輪清單沒有職位');
  assert.equal(detailCoverageLabel({detail_complete_count:0,detail_total_count:0,listing_complete:false}),'尚未取得可核實詳情');
  assert.equal(detailCoverageLabel({}),'此版本未提供');
});
