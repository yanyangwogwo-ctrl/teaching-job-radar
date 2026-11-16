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

test('preset exclusions preserve phrases and cover title, department, subject and body',()=>{
  const preset={...defaults,presetEnabled:true,subjectKeywords:'',roleKeywords:'',partTimeOnly:false,presetExcludeKeywords:'machine learning，護理\nAI literacy'};
  assert.deepEqual(filterJobs(jobs,preset,'2026-09-05'),[]);
  for (const fields of [{title:'Lecturer in 護理'},{department:'護理學系'},{subjects:[{id:'nursing',label:'護理'}]},{description:'Teach 護理.'}]) {
    const advert={...jobs[0],description:'Teach ethics.',...fields};
    assert.equal(filterJobs([advert],preset,'2026-09-05').length,0);
  }
  const differentPhrase={...jobs[0],description:'Teach AI ethics and digital literacy.'};
  assert.equal(filterJobs([differentPhrase],preset,'2026-09-05').length,1);
  assert.equal(filterJobs([{...differentPhrase,title:'Chair in Philosophy'}],{...preset,presetExcludeKeywords:'AI',query:''},'2026-09-05').length,0);
  assert.equal(filterJobs([{...differentPhrase,title:'Chair in Philosophy',description:'Teach ethics.'}],{...preset,presetExcludeKeywords:'AI'},'2026-09-05').length,1);
});
test('preset exclusions toggle independently of manual exclusions and query negatives',()=>{
  const preset={presetEnabled:true,subjectKeywords:'',roleKeywords:'',partTimeOnly:false,presetExcludeKeywords:'AI literacy'};
  assert.deepEqual(find(preset),['b']);
  assert.deepEqual(find({...preset,presetEnabled:false}),['b','a']);
  assert.deepEqual(find({...preset,presetEnabled:false,exclude:'"machine learning"'}),['a']);
  assert.deepEqual(find({...preset,presetEnabled:false,query:'-"machine learning"'}),['a']);
  assert.deepEqual(find({...preset,exclude:'"machine learning"'}),[]);
  assert.deepEqual(find({...preset,presetExcludeKeywords:''}),['b','a']);
  assert.deepEqual(find(preset),['b']);
});

test('default criteria expose terms and put newer adverts ahead of higher scores',()=>{
  const data={meta:{subjects:[{terms:['philosophy','machine learning']}]},sources:[{id:'a'},{id:'b'}]};
  const criteria=defaultCriteria(data);
  assert.equal(criteria.sort,'newest');
  assert.equal(criteria.subjectKeywords,'philosophy, machine learning');
  assert.ok(criteria.roleKeywords.includes('lecturer'));
  assert.equal(criteria.presetEnabled,true);
  assert.equal(criteria.presetExcludeKeywords,'');
  assert.equal(Object.hasOwn(criteria,'presetMode'),false);
  assert.equal(criteria.partTimeOnly,false);
  assert.equal(Object.hasOwn(criteria,'relevantOnly'),false);
  assert.deepEqual(filterJobs(jobs,criteria,'2026-09-05').map(j=>j.id),['b','a']);
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
  assert.equal(restored.presetExcludeKeywords,'');
  assert.deepEqual(restored.institutions,['a']);
  assert.equal(restoreCriteria({...old,presetEnabled:false},data).presetEnabled,false);
  for (const value of ['', 'AI literacy，護理\naccounting']) {
    const saved=JSON.parse(JSON.stringify({...old,presetEnabled:false,presetExcludeKeywords:value,exclude:'nursing'}));
    const loaded=restoreCriteria(saved,data);
    assert.equal(loaded.presetExcludeKeywords,value);
    assert.equal(loaded.exclude,'nursing');
    assert.equal(loaded.presetEnabled,false);
  }
});
test('merged saved presets split into groups with original input retained for review',()=>{
  const data={meta:{subjects:[{terms:['philosophy']}]},sources:[{id:'a'},{id:'b'}]};
  const merged={...defaults,presetEnabled:false,presetKeywords:'AI literacy, instructor, custom topic',presetMode:'AND',partTimeOnly:false};
  const restored=restoreCriteria(merged,data);
  assert.equal(restored.subjectKeywords,'AI literacy, custom topic');
  assert.equal(restored.roleKeywords,'instructor');
  assert.equal(restored.presetEnabled,false);
  assert.equal(restored.partTimeOnly,false);
  assert.equal(restored.presetExcludeKeywords,'');
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
test('recent date ranges include today and cross month, leap year and year boundaries',()=>{
  assert.equal(recentCutoff('2026-09-05'),'2026-08-30');
  assert.equal(recentCutoff('2026-09-05','3'),'2026-09-03');
  assert.equal(recentCutoff('2024-03-02','3'),'2024-02-29');
  assert.equal(recentCutoff('2026-01-02','7'),'2025-12-27');
  assert.equal(recentCutoff('2026-09-05','14'),'2026-08-23');
  assert.equal(recentCutoff('2026-09-05','30'),'2026-08-07');
  assert.equal(recentCutoff('2026-09-05','all'),'');
});
test('old posted dates stay hidden within range, and all dates removes the old hard limit',()=>{
  const adverts=[
    {...jobs[0],id:'boundary',posted_date:'2026-08-30'},
    {...jobs[0],id:'old',posted_date:'2026-08-29'},
    {...jobs[0],id:'future',posted_date:'2026-09-06'},
    {...jobs[1],id:'first-seen'},
    {...jobs[1],id:'old-first-seen',first_seen:'2026-08-29T15:59:59Z'},
    {...jobs[1],id:'hk-boundary',first_seen:'2026-08-29T16:00:00Z'}
  ];
  const result=filterJobs(adverts,{...defaults,dateBasis:'deadline',sort:'newest'},'2026-09-05');
  assert.deepEqual(result.map(j=>j.id).sort(),['boundary','first-seen','hk-boundary']);
  assert.equal(filterJobs(adverts,{...defaults,recentDays:'all'},'2026-09-05').length,6);
  const oldOngoing={...jobs[0],posted_date:'2026-01-01',deadline_type:'until-filled',deadline:null};
  assert.equal(filterJobs([oldOngoing],{...defaults,recentDays:'all',status:'open'},'2026-09-05').length,1);
  assert.equal(filterJobs([oldOngoing],{...defaults,recentDays:'all',from:'2026-08-01'},'2026-09-05').length,0);
});
test('saved searches adopt seven days, preserve chosen ranges and retain explicit historical dates',()=>{
  const data={meta:{subjects:[]},sources:[{id:'a'},{id:'b'}]};
  assert.equal(defaultCriteria(data).recentDays,'7');
  assert.equal(restoreCriteria(defaults,data).recentDays,'7');
  assert.equal(restoreCriteria({...defaults,recentDays:'all'},data).recentDays,'all');
  assert.equal(restoreCriteria({...defaults,recentDays:'14'},data).recentDays,'14');
  assert.equal(restoreCriteria({...defaults,recentDays:'invalid'},data).recentDays,'7');
  assert.equal(restoreCriteria({...defaults,from:'2026-01-01'},data).recentDays,'all');
});
test('adjunct teaching titles enter defaults without replacing customised saved role words',()=>{
  const data={meta:{subjects:[{terms:['ethics']}]},sources:[{id:'a'}]};
  const criteria=defaultCriteria(data);
  const advert={...jobs[0],title:'Adjunct Faculty',description:'Teach ethics.',detail_complete:false};
  assert.equal(filterJobs([advert],criteria,'2026-09-05').length,1);
  assert.equal(filterJobs([{...advert,title:'Adjunct Teaching Faculty'}],criteria,'2026-09-05').length,1);
  assert.equal(filterJobs([{...advert,title:'Adjunct Research Associate'}],criteria,'2026-09-05').length,0);
  const oldRoles='lecturer, tutor, instructor, teacher, teaching fellow, 講師, 導師, 教師';
  assert.ok(restoreCriteria({...defaults,roleKeywords:oldRoles},data).roleKeywords.includes('adjunct faculty'));
  assert.equal(restoreCriteria({...defaults,roleKeywords:'tutor'},data).roleKeywords,'tutor');
  assert.equal(restoreCriteria({...defaults,roleKeywords:''},data).roleKeywords,'');
});
test('incomplete jobs can still match title and department without a notification match',()=>{
  const advert={...jobs[0],title:'Part-time Lecturer',department:'Philosophy',description:'',detail_complete:false,matches:false};
  const criteria={...defaults,presetEnabled:true,subjectKeywords:'philosophy',roleKeywords:'lecturer'};
  assert.equal(filterJobs([advert],criteria,'2026-09-05').length,1);
});
test('subject matching excludes irrelevant philosophy and generic skills boilerplate',()=>{
  const advert={...jobs[0],title:'Part-time Lecturer in Accounting',department:'Business',description:'Applicants should have a Doctor of Philosophy degree.'};
  const criteria={...defaults,presetEnabled:true,subjectKeywords:'philosophy, critical thinking',roleKeywords:'lecturer'};
  assert.equal(filterJobs([advert],criteria,'2026-09-05').length,0);
  assert.equal(filterJobs([{...advert,description:'Applicants should have critical thinking skills.'}],criteria,'2026-09-05').length,0);
  assert.equal(filterJobs([{...advert,description:'Teach critical thinking and philosophy of science.'}],criteria,'2026-09-05').length,1);
  assert.equal(filterJobs([advert],{...criteria,presetEnabled:false,query:'philosophy'},'2026-09-05').length,1);
});
