export function normalize(value) {
  return String(value ?? '').normalize('NFKC').toLowerCase()
    .replace(/[‐‑‒–—−]/g, '-').replace(/\bpart[\s-]*time\b/g, 'parttime')
    .replace(/\bfull[\s-]*time\b/g, 'fulltime').replace(/\b(lecturer|tutor|instructor|teacher|fellow)s\b/g, '$1')
    .replace(/\s+/g, ' ').trim();
}

export function includesTerm(text, term) {
  const value = normalize(term);
  if (!value) return false;
  let pattern = value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  if (/^[a-z0-9]/.test(value)) pattern = '(?<![a-z0-9])' + pattern;
  if (/[a-z0-9]$/.test(value)) pattern += '(?![a-z0-9])';
  return new RegExp(pattern, 'u').test(normalize(text));
}

export function parseQuery(query) {
  const positive = [], negative = [];
  let buffer = '', quoted = false;
  function flush() {
    const term = buffer.trim();
    if (term.startsWith('-') && term.length > 1) negative.push(term.slice(1));
    else if (term && term !== '-') positive.push(term);
    buffer = '';
  }
  for (const character of String(query).slice(0, 500)) {
    if (character === '"') quoted = !quoted;
    else if (!quoted && /[\s,，]/.test(character)) flush();
    else buffer += character;
  }
  flush();
  return { positive, negative, unclosedQuote: quoted };
}

export function dayKey(value) {
  if (!value) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Hong_Kong', year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}

export function effectiveStatus(job, today = dayKey(new Date().toISOString())) {
  if (job.deadline_type === 'closing' && job.deadline && job.deadline < today) return 'closed';
  return job.status;
}

export function dateFor(job, basis) {
  if (basis === 'first_seen') return dayKey(job.first_seen);
  if (basis === 'deadline') return job.deadline_type === 'closing' ? job.deadline ?? '' : '';
  return job.posted_date || dayKey(job.first_seen);
}

export function recentCutoff(today = dayKey(new Date().toISOString())) {
  const [year, month, day] = today.split('-').map(Number);
  const target = new Date(Date.UTC(year, month - 3, 1));
  const lastDay = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
  target.setUTCDate(Math.min(day, lastDay));
  return target.toISOString().slice(0, 10);
}

export function isRecent(job, today = dayKey(new Date().toISOString())) {
  const date = dateFor(job, 'effective');
  return !!date && date >= recentCutoff(today) && date <= today;
}

export function keywordTerms(value) {
  return String(value ?? '').slice(0, 5000).split(/[,，\n]/u)
    .map(term => term.trim().replace(/^"(.*)"$/, '$1').trim()).filter(Boolean);
}

export function defaultCriteria(dataset) {
  return {
    query:'', exclude:'', mode:'AND',
    subjectKeywords:dataset.meta.subjects.flatMap(subject => subject.terms).join(', '),
    roleKeywords:'lecturer, tutor, instructor, teacher, teaching fellow, 講師, 導師, 教師',
    partTimeOnly:true, institutions:dataset.sources.map(source => source.id),
    status:'open', dateBasis:'effective', from:'', to:'', sort:'newest'
  };
}

export function restoreCriteria(value, dataset) {
  const defaults = defaultCriteria(dataset);
  const result = {...defaults, ...value};
  // Older saved searches used an invisible relevance switch. Convert it to
  // the same editable fields now shown in the form.
  if (!Object.hasOwn(value, 'subjectKeywords') && value.relevantOnly === false) {
    result.subjectKeywords = ''; result.roleKeywords = ''; result.partTimeOnly = false;
  }
  delete result.relevantOnly;
  result.institutions = result.institutions.filter(id => dataset.sources.some(source => source.id === id));
  return result;
}

function subjectText(job) {
  const lines = String(job.description ?? '').split('\n').flatMap(line => {
    if (/equal opportunit|committed to equality|code of conduct|ethical conduct|professional integrity/i.test(line)) return [];
    line = line.replace(/(?:doctor|master) of philosophy|teaching philosophy|personal philosophy/gi, '');
    if (/(applicants?|candidates?).{0,90}(?:possess|have|demonstrate)|具備.{0,30}能力/i.test(line) && !/teach|course|curriculum|授課|教授/i.test(line)) {
      line = line.replace(/critical thinking|批判思考|批判性思維/gi, '');
    }
    return [line];
  });
  return [job.title, job.department, ...lines].join('\n');
}

const employmentWords = { 'part-time': 'parttime 兼職', hourly: 'hourly 時薪 兼職 parttime', mixed: 'fulltime parttime 全職 兼職', 'full-time': 'fulltime 全職', unknown: '' };
export function searchText(job) {
  return [job.title, job.department, job.description, job.role, employmentWords[job.employment_type], ...(job.subjects ?? []).map(s => s.label + ' ' + s.id)].join('\n');
}

export function filterJobs(jobs, criteria, today = dayKey(new Date().toISOString())) {
  const query = parseQuery(criteria.query ?? '');
  const extra = parseQuery(criteria.exclude ?? '');
  const negative = [...query.negative, ...extra.positive, ...extra.negative];
  const selected = new Set(criteria.institutions ?? []);
  const subjects = keywordTerms(criteria.subjectKeywords);
  const roles = keywordTerms(criteria.roleKeywords);
  const result = jobs.filter(job => {
    if (!isRecent(job, today)) return false;
    if (!selected.has(job.source_id)) return false;
    if (criteria.partTimeOnly && !['part-time', 'hourly', 'mixed'].includes(job.employment_type)) return false;
    if (roles.length && !roles.some(term => includesTerm(job.title, term))) return false;
    if (subjects.length) {
      const text = subjectText(job);
      if (!subjects.some(term => includesTerm(text, term))) return false;
    }
    if (criteria.status !== 'all' && effectiveStatus(job, today) !== criteria.status) return false;
    const date = dateFor(job, criteria.dateBasis);
    if (criteria.from && (!date || date < criteria.from)) return false;
    if (criteria.to && (!date || date > criteria.to)) return false;
    const text = searchText(job);
    if (negative.some(term => includesTerm(text, term))) return false;
    return query.positive.length === 0 || (criteria.mode === 'OR'
      ? query.positive.some(term => includesTerm(text, term))
      : query.positive.every(term => includesTerm(text, term)));
  });
  return result.sort((a, b) => {
    if (criteria.sort === 'score' && a.score !== b.score) return b.score - a.score;
    if (criteria.sort === 'institution') {
      const name = a.institution.localeCompare(b.institution, 'zh-Hant');
      if (name) return name;
    }
    const basis = criteria.sort === 'deadline' ? 'deadline' : criteria.dateBasis;
    const av = dateFor(a, basis), bv = dateFor(b, basis);
    if (!av && bv) return 1;
    if (av && !bv) return -1;
    const ascending = criteria.sort === 'oldest' || criteria.sort === 'deadline';
    return (ascending ? av.localeCompare(bv) : bv.localeCompare(av)) || a.title.localeCompare(b.title);
  });
}

export function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\s]*[=+@-]/.test(text) || /^[\t\r]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g, '""') + '"';
}
