import { test } from 'node:test';
import assert from 'node:assert/strict';
import { filterJobs, parseQuery, includesTerm, dayKey, csvCell, defaultCriteria, restoreCriteria, recentCutoff, keywordTerms } from '../dist/search.mjs';

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
test('unified preset keywords match across titles, body and subject metadata',()=>{
  assert.deepEqual(find({presetEnabled:true,presetKeywords:'AI literacy',partTimeOnly:true}),['a']);
  assert.deepEqual(find({presetEnabled:true,presetKeywords:'machine learning',partTimeOnly:false}),['b']);
  assert.deepEqual(find({presetEnabled:true,presetKeywords:'machine learning',partTimeOnly:true}),[]);
  const advert={...jobs[0],title:'Humanities teaching vacancy',description:'Support students as an instructor.',subjects:[{id:'ai',label:'AI literacy'}]};
  assert.equal(filterJobs([advert],{...defaults,presetEnabled:true,presetKeywords:'instructor, AI literacy',presetMode:'AND'},'2026-09-05').length,1);
  assert.deepEqual(find({presetEnabled:true,presetKeywords:'philosophy, tutor',presetMode:'OR'}),['b','a']);
  assert.deepEqual(find({presetEnabled:true,presetKeywords:'philosophy, tutor',presetMode:'AND'}),[]);
  assert.deepEqual(keywordTerms('AI literacy, 哲學\ncritical thinking'),['AI literacy','哲學','critical thinking']);
});
test('preset toggle keeps independent filters and edited terms intact',()=>{
  const preset={...defaults,presetEnabled:true,presetKeywords:'philosophy',partTimeOnly:true,query:'tutor',exclude:'',institutions:['b']};
  assert.equal(filterJobs(jobs,preset,'2026-09-05').length,0);
  assert.deepEqual(filterJobs(jobs,{...preset,presetEnabled:false},'2026-09-05').map(j=>j.id),['b']);
  assert.equal(filterJobs(jobs,{...preset,presetEnabled:false,exclude:'machine'},'2026-09-05').length,0);
  assert.equal(filterJobs(jobs,{...preset,presetEnabled:false,institutions:[]},'2026-09-05').length,0);
  assert.equal(filterJobs(jobs,{...preset,presetEnabled:false,to:'2026-09-04'},'2026-09-05').length,0);
  assert.equal(filterJobs([{...jobs[1],posted_date:'2026-07-04'}],{...preset,presetEnabled:false},'2026-09-05').length,0);
  assert.equal(filterJobs(jobs,preset,'2026-09-05').length,0);
  assert.equal(preset.presetKeywords,'philosophy');
});
test('CSV formulas are escaped',()=>{
  assert.equal(csvCell('=1+1'),'"\'=1+1"');
  assert.equal(csvCell('a,"b"'),'"a,""b"""');
});

test('default criteria expose terms and put newer adverts ahead of higher scores',()=>{
  const data={meta:{subjects:[{terms:['philosophy','machine learning']}]},sources:[{id:'a'},{id:'b'}]};
  const criteria=defaultCriteria(data);
  assert.equal(criteria.sort,'newest');
  assert.ok(criteria.presetKeywords.includes('philosophy, machine learning'));
  assert.ok(criteria.presetKeywords.includes('lecturer'));
  assert.equal(criteria.presetEnabled,true);
  assert.equal(criteria.presetMode,'OR');
  assert.equal(criteria.partTimeOnly,true);
  assert.equal(Object.hasOwn(criteria,'relevantOnly'),false);
  assert.deepEqual(filterJobs(jobs,{...criteria,partTimeOnly:false},'2026-09-05').map(j=>j.id),['b','a']);
  const restored=restoreCriteria({...defaults,relevantOnly:false},data);
  assert.equal(restored.presetEnabled,false);
});
test('saved searches migrate merged phrases and preserve modern empty or disabled presets',()=>{
  const data={meta:{subjects:[{terms:['philosophy']}]},sources:[{id:'a'},{id:'b'}]};
  const old={...defaults,subjectKeywords:'AI literacy, philosophy',roleKeywords:'instructor, tutor',partTimeOnly:false,query:'ethics',institutions:['a','gone']};
  const restored=restoreCriteria(old,data);
  assert.equal(restored.presetKeywords,'AI literacy, philosophy, instructor, tutor');
  assert.equal(restored.presetEnabled,true);
  assert.equal(restored.partTimeOnly,false);
  assert.equal(restored.query,'ethics');
  assert.deepEqual(restored.institutions,['a']);
  assert.equal(Object.hasOwn(restored,'subjectKeywords'),false);
  assert.equal(Object.hasOwn(restored,'roleKeywords'),false);
  const modern=restoreCriteria({...old,presetKeywords:'',presetEnabled:false,presetMode:'AND'},data);
  assert.equal(modern.presetKeywords,'');
  assert.equal(modern.presetEnabled,false);
  assert.equal(modern.presetMode,'AND');
});
test('two calendar months handles month ends and leap years',()=>{
  assert.equal(recentCutoff('2026-09-05'),'2026-07-05');
  assert.equal(recentCutoff('2026-04-30'),'2026-02-28');
  assert.equal(recentCutoff('2024-04-30'),'2024-02-29');
  assert.equal(recentCutoff('2026-01-31'),'2025-11-30');
});
test('old posted dates stay hidden despite recent first-seen or future deadlines',()=>{
  const adverts=[
    {...jobs[0],id:'boundary',posted_date:'2026-07-05'},
    {...jobs[0],id:'old',posted_date:'2026-07-04'},
    {...jobs[0],id:'future',posted_date:'2026-09-06'},
    {...jobs[1],id:'first-seen'},
    {...jobs[1],id:'old-first-seen',first_seen:'2026-07-04T15:59:59Z'},
    {...jobs[1],id:'hk-boundary',first_seen:'2026-07-04T16:00:00Z'}
  ];
  const result=filterJobs(adverts,{...defaults,dateBasis:'deadline',sort:'newest'},'2026-09-05');
  assert.deepEqual(result.map(j=>j.id).sort(),['boundary','first-seen','hk-boundary']);
});
test('explicit unified keywords search the complete available body',()=>{
  const advert={...jobs[0],title:'Part-time Lecturer in Accounting',department:'Business',description:'Applicants should have a Doctor of Philosophy degree.'};
  assert.equal(filterJobs([advert],{...defaults,presetEnabled:true,presetKeywords:'philosophy'},'2026-09-05').length,1);
});
