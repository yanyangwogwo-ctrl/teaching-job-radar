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
test('preset requires any subject AND any title role, plus employment type',()=>{
  const preset={presetEnabled:true,subjectKeywords:'philosophy, AI literacy',roleKeywords:'lecturer, instructor',partTimeOnly:true};
  assert.deepEqual(find(preset),['a']);
  const accounting={...jobs[0],title:'Part-time Lecturer in Accounting',department:'Business',description:'Teach accounting.'};
  const research={...jobs[0],title:'Research Assistant in Philosophy',description:'Assist the lecturer in teaching philosophy.'};
  const filters={...defaults,...preset};
  assert.equal(filterJobs([accounting,research],filters,'2026-09-05').length,0);
  const science={...jobs[0],title:'Part-time Instructor',description:'Teach AI literacy.'};
  assert.equal(filterJobs([science],filters,'2026-09-05').length,1);
  assert.deepEqual(find({...preset,subjectKeywords:'machine learning',roleKeywords:'tutor'}),[]);
  assert.deepEqual(find({...preset,subjectKeywords:'machine learning',roleKeywords:'tutor',partTimeOnly:false}),['b']);
  assert.deepEqual(find({...preset,subjectKeywords:'',roleKeywords:'',partTimeOnly:false}),['b','a']);
  assert.deepEqual(keywordTerms('AI literacy, 哲學\ncritical thinking'),['AI literacy','哲學','critical thinking']);
});
test('preset toggle keeps independent filters and edited terms intact',()=>{
  const preset={...defaults,presetEnabled:true,subjectKeywords:'philosophy',roleKeywords:'lecturer',partTimeOnly:true,query:'tutor',exclude:'',institutions:['b']};
  assert.equal(filterJobs(jobs,preset,'2026-09-05').length,0);
  assert.deepEqual(filterJobs(jobs,{...preset,presetEnabled:false},'2026-09-05').map(j=>j.id),['b']);
  assert.equal(filterJobs(jobs,{...preset,presetEnabled:false,exclude:'machine'},'2026-09-05').length,0);
  assert.equal(filterJobs(jobs,{...preset,presetEnabled:false,institutions:[]},'2026-09-05').length,0);
  assert.equal(filterJobs(jobs,{...preset,presetEnabled:false,to:'2026-09-04'},'2026-09-05').length,0);
  assert.equal(filterJobs([{...jobs[1],posted_date:'2026-07-04'}],{...preset,presetEnabled:false},'2026-09-05').length,0);
  assert.equal(filterJobs(jobs,preset,'2026-09-05').length,0);
  assert.equal(preset.subjectKeywords,'philosophy');
  assert.equal(preset.roleKeywords,'lecturer');
});
test('CSV formulas are escaped',()=>{
  assert.equal(csvCell('=1+1'),'"\'=1+1"');
  assert.equal(csvCell('a,"b"'),'"a,""b"""');
});

test('default criteria expose terms and put newer adverts ahead of higher scores',()=>{
  const data={meta:{subjects:[{terms:['philosophy','machine learning']}]},sources:[{id:'a'},{id:'b'}]};
  const criteria=defaultCriteria(data);
  assert.equal(criteria.sort,'newest');
  assert.equal(criteria.subjectKeywords,'philosophy, machine learning');
  assert.ok(criteria.roleKeywords.includes('lecturer'));
  assert.equal(criteria.presetEnabled,true);
  assert.equal(Object.hasOwn(criteria,'presetMode'),false);
  assert.equal(criteria.partTimeOnly,true);
  assert.equal(Object.hasOwn(criteria,'relevantOnly'),false);
  assert.deepEqual(filterJobs(jobs,{...criteria,partTimeOnly:false},'2026-09-05').map(j=>j.id),['b','a']);
  const restored=restoreCriteria({...defaults,relevantOnly:false},data);
  assert.equal(restored.presetEnabled,false);
});
test('saved separate groups preserve empty values and disabled state',()=>{
  const data={meta:{subjects:[{terms:['philosophy']}]},sources:[{id:'a'},{id:'b'}]};
  const old={...defaults,subjectKeywords:'AI literacy',roleKeywords:'',partTimeOnly:false,query:'ethics',institutions:['a','gone']};
  const restored=restoreCriteria(old,data);
  assert.equal(restored.subjectKeywords,'AI literacy');
  assert.equal(restored.roleKeywords,'');
  assert.equal(restored.presetEnabled,true);
  assert.equal(restored.partTimeOnly,false);
  assert.equal(restored.query,'ethics');
  assert.deepEqual(restored.institutions,['a']);
  assert.equal(restoreCriteria({...old,presetEnabled:false},data).presetEnabled,false);
});
test('merged saved presets split into groups with original input retained for review',()=>{
  const data={meta:{subjects:[{terms:['philosophy']}]},sources:[{id:'a'},{id:'b'}]};
  const merged={...defaults,presetEnabled:false,presetKeywords:'AI literacy, instructor, custom topic',presetMode:'AND',partTimeOnly:false};
  const restored=restoreCriteria(merged,data);
  assert.equal(restored.subjectKeywords,'AI literacy, custom topic');
  assert.equal(restored.roleKeywords,'instructor');
  assert.equal(restored.presetEnabled,false);
  assert.equal(restored.partTimeOnly,false);
  assert.deepEqual(restored.legacyMergedPreset,{keywords:merged.presetKeywords,mode:'AND'});
  assert.equal(Object.hasOwn(restored,'presetKeywords'),false);
  assert.equal(Object.hasOwn(restored,'presetMode'),false);
  assert.equal(merged.presetKeywords,'AI literacy, instructor, custom topic');
  const roleOnly=restoreCriteria({...merged,presetKeywords:'lecturer'},data);
  assert.equal(roleOnly.subjectKeywords,'philosophy');
  const blank=restoreCriteria({...merged,presetKeywords:''},data);
  assert.equal(blank.subjectKeywords,'');
  assert.equal(blank.roleKeywords,'');
  const splitWins=restoreCriteria({...merged,subjectKeywords:'ethics',roleKeywords:''},data);
  assert.equal(splitWins.subjectKeywords,'ethics');
  assert.equal(splitWins.roleKeywords,'');
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
test('subject matching excludes irrelevant philosophy and generic skills boilerplate',()=>{
  const advert={...jobs[0],title:'Part-time Lecturer in Accounting',department:'Business',description:'Applicants should have a Doctor of Philosophy degree.'};
  const criteria={...defaults,presetEnabled:true,subjectKeywords:'philosophy, critical thinking',roleKeywords:'lecturer'};
  assert.equal(filterJobs([advert],criteria,'2026-09-05').length,0);
  assert.equal(filterJobs([{...advert,description:'Applicants should have critical thinking skills.'}],criteria,'2026-09-05').length,0);
  assert.equal(filterJobs([{...advert,description:'Teach critical thinking and philosophy of science.'}],criteria,'2026-09-05').length,1);
  assert.equal(filterJobs([advert],{...criteria,presetEnabled:false,query:'philosophy'},'2026-09-05').length,1);
});
