"""Deterministic matching: evidence, not a prediction of employability."""
from __future__ import annotations

import re
import unicodedata


def normalize(value: str) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).lower()
    value = re.sub(r'[‐‑‒–—−]', '-', value)
    value = re.sub(r'\bpart[\s-]*time\b', 'parttime', value)
    value = re.sub(r'\bfull[\s-]*time\b', 'fulltime', value)
    return re.sub(r'\s+', ' ', value).strip()


def contains(text: str, term: str) -> bool:
    haystack, needle = normalize(text), normalize(term)
    if not needle:
        return False
    # English tokens have boundaries; AI must not match "chair".
    pattern = re.escape(needle)
    if re.match(r'[a-z0-9]', needle):
        pattern = r'(?<![a-z0-9])' + pattern
    if re.search(r'[a-z0-9]$', needle):
        pattern += r'(?![a-z0-9])'
    return re.search(pattern, haystack) is not None


def role_of(title: str) -> str:
    value = normalize(title)
    for role, pattern in [('lecturer', r'\b(lecturers?|teachers?|teaching fellows?)\b|講師|教師'),
                          ('tutor', r'\btutors?\b|導師'),
                          ('instructor', r'\binstructors?\b|導師')]:
        if re.search(pattern, value):
            return role
    return 'other'


def employment_of(title: str, explicit: str = '', body: str = '') -> str:
    employment_title = re.split(r'\bfor\b|\bto teach\b', title, maxsplit=1, flags=re.I)[0]
    primary = normalize(employment_title + ' ' + explicit)
    primary = re.sub(r'fulltime (?:(?:day[- ]?)?(?:degree|subdegree|sub-degree|diploma) )?(?:programmes?|programs?|courses?|students?)', '', primary)
    part = 'parttime' in primary or '兼職' in primary or '兼任' in primary
    full = 'fulltime' in primary or '全職' in primary
    if part:
        return 'mixed' if full else 'part-time'
    if re.search(r'hourly[ -]?(paid|rate)|時薪', primary):
        return 'hourly'
    if full:
        return 'full-time'
    # Only direct employment statements, not a part-time course description.
    body = normalize(body)
    if re.search(r'(recruiting|appoint(?:ment|ed)?|employ(?:ed|ment)?).{0,60}(parttime (lecturer|instructor|tutor|teacher)|on a parttime basis)', body):
        return 'part-time'
    if re.search(r'(salary|remuneration).{0,60}hourly rate', body):
        return 'hourly'
    return 'unknown'


def subject_text(job: dict) -> str:
    # Adapters remove navigation and faculty boilerplate. Qualifications are
    # searchable but must not turn a generic PhD / thinking skill into a subject.
    text = job.get('match_text', '') or job.get('description', '')
    lines = []
    for line in text.splitlines():
        if re.search(r'equal opportunit|committed to equality|code of conduct|ethical conduct|professional integrity', line, re.I):
            continue
        if re.search(r'(?:doctor|master) of philosophy|teaching philosophy|personal philosophy', line, re.I):
            line = re.sub(r'(?:doctor|master) of philosophy|teaching philosophy|personal philosophy', '', line, flags=re.I)
        if re.search(r'(applicants?|candidates?).{0,90}(?:possess|have|demonstrate)|具備.{0,30}能力', line, re.I) and not re.search(r'teach|course|curriculum|授課|教授', line, re.I):
            line = re.sub(r'critical thinking|批判思考|批判性思維', '', line, flags=re.I)
        lines.append(line)
    return job['title'] + '\n' + job.get('department', '') + '\n' + '\n'.join(lines)


def evaluate(job: dict, prefs: dict) -> dict:
    searchable = subject_text(job)
    evidence, tags = [], []
    score = 0
    for subject in prefs['subjects']:
        hits = [term for term in subject['terms'] if contains(searchable, term)]
        if not hits:
            continue
        tags.append({'id': subject['id'], 'label': subject['label']})
        score = max(score, subject['weight'])
        lines = [line.strip() for line in searchable.splitlines() if any(contains(line, hit) for hit in hits)]
        evidence.append({'subject': subject['label'], 'terms': hits, 'snippet': (lines[0] if lines else hits[0])[:300]})
    rule = prefs['notifications']
    role = role_of(job['title'])
    employment = job.get('employment_type', 'unknown')
    is_pt = employment in ('part-time', 'hourly', 'mixed')
    if tags and is_pt and role in rule['roles']:
        score = min(100, score + 20)
    else:
        score = min(score, 50)
    searchable_all = ' '.join(str(job.get(k, '')) for k in ('title', 'department', 'description'))
    excluded = any(contains(searchable_all, term) for term in rule.get('exclude', []))
    institution_ok = not rule.get('institutions') or job['source_id'] in rule['institutions']
    matches = bool(tags and score >= rule['min_score'] and role in rule['roles'] and
                   (is_pt or not rule['require_part_time']) and not excluded and institution_ok)
    return {'role': role, 'subjects': tags, 'score': score, 'evidence': evidence, 'matches': matches}
