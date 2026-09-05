import { test } from 'node:test';
import assert from 'node:assert/strict';
import { filterJobs, parseQuery, includesTerm, dayKey, csvCell } from '../dist/search.mjs';

const jobs = [
  {id:'a',source_id:'a',institution:'A',title:'Part-time Lecturers in Philosophy',department:'Humanities',description:'Teach ethics and AI literacy.',role:'lecturer',employment_type:'part-time',subjects:[],score:85,matches:true,status:'open',posted_date:'2026-09-04',first_seen:'2026-09-05T00:17:00Z',deadline_type:'closing',deadline:'2026-09-10'},
  {id:'b',source_id:'b',institution:'B',title:'Tutor',department:'Computing',description:'Teach machine learning.',role:'tutor',employment_type:'full-time',subjects:[],score:0,matches:false,status:'open',posted_date:null,first_seen:'2026-09-04T18:00:00Z',deadline_type:'review',deadline:'2026-08-30'},
];
const defaults = {institutions:['a','b'],status:'all',relevantOnly:false,dateBasis:'effective',sort:'newest',mode:'AND',query:'',exclude:''};
const find = values => filterJobs(jobs,{...defaults,...values},'2026-09-05').map(j=>j.id);

test('quoted phrases, negative phrases and Chinese separators',()=>{
  assert.deepEqual(parseQuery('philosophy，"AI literacy" -"machine learning"'),{positive:['philosophy','AI literacy'],negative:['machine learning'],unclosedQuote:false});
  assert.equal(parseQuery('"unfinished').unclosedQuote,true);
});
test('parttime aliases and plurals match while AI is bounded',()=>{
  assert.equal(includesTerm(jobs[0].title,'parttime'),true);
  assert.equal(includesTerm(jobs[0].title,'lecturer'),true);
  assert.equal(includesTerm('Chair in Nursing','AI'),false);
});
test('AND combines subjects and role; OR broadens; exclusions remain mandatory',()=>{
  assert.deepEqual(find({query:'philosophy, parttime, lecturer'}),['a']);
  assert.deepEqual(find({query:'philosophy tutor',mode:'OR'}),['b','a']);
  assert.deepEqual(find({query:'philosophy tutor -fulltime',mode:'OR'}),['a']);
  assert.deepEqual(find({query:'philosophy',exclude:'"AI literacy"'}),[]);
});
test('institution selection is independent including select none',()=>{
  assert.deepEqual(find({institutions:[]}),[]);
  assert.deepEqual(find({institutions:['b'],query:'philosophy'}),[]);
  assert.deepEqual(find({institutions:['a']}),['a']);
});
test('inclusive dates and HK first-seen date',()=>{
  assert.equal(dayKey(jobs[1].first_seen),'2026-09-05');
  assert.deepEqual(find({from:'2026-09-05',to:'2026-09-05'}),['b']);
  assert.deepEqual(find({dateBasis:'first_seen',from:'2026-09-05',to:'2026-09-05'}),['a','b']);
});
test('review dates are not closing dates',()=>{
  assert.deepEqual(find({dateBasis:'deadline',from:'2026-01-01'}),['a']);
  assert.deepEqual(find({status:'open',institutions:['b']}),['b']);
});
test('relevance is optional and CSV formulas are escaped',()=>{
  assert.deepEqual(find({relevantOnly:true}),['a']);
  assert.equal(csvCell('=1+1'),'"\'=1+1"');
  assert.equal(csvCell('a,"b"'),'"a,""b"""');
});
