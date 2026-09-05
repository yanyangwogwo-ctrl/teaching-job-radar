from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, date
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

MONTHS = {m.lower(): i for i, m in enumerate(['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'], 1)}


def clean_text(value: str) -> str:
    return '\n'.join(re.sub(r'[\t \u00a0]+', ' ', line).strip() for line in str(value or '').splitlines() if line.strip())


def parse_date(value: str) -> str | None:
    """No inference from job references, filenames, relative ages or locale-ambiguous dates."""
    value = str(value or '').strip()
    value = re.sub(r'(\d{1,2})-([A-Za-z]{3,9})-(20\d{2})', r'\1 \2 \3', value)
    patterns = [r'(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?(?!\d)',
                r'\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(20\d{2})\b',
                r'\b([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(20\d{2})\b']
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, value)
        if not match:
            continue
        parts = match.groups()
        try:
            if index == 0:
                year, month, day = map(int, parts)
            else:
                label = parts[1] if index == 1 else parts[0]
                month = next(n for name, n in MONTHS.items() if name.startswith(label.lower()))
                day, year = int(parts[0] if index == 1 else parts[1]), int(parts[2])
            return date(year, month, day).isoformat()
        except (ValueError, StopIteration):
            continue
    return None


def deadline_from(text: str) -> tuple[str | None, str, str]:
    lines = clean_text(text).splitlines()
    for i, line in enumerate(lines):
        if re.search(r'closing date|application deadline|截止日期|截止申請|截止報名', line, re.I):
            fragment = ' '.join(lines[i:i+3])[:450]
            if re.search(r'whichever is earlier', fragment, re.I) and parse_date(fragment):
                return parse_date(fragment), 'closing', fragment
            if re.search(r'until.{0,35}filled|on[ -]?going|長期|額滿即止', fragment, re.I):
                return None, 'until-filled', fragment
            parsed = parse_date(fragment)
            if parsed:
                return parsed, 'closing', fragment
    for line in lines:
        if re.search(r'initial screening|review of applications|首輪|開始審閱', line, re.I):
            return parse_date(line), 'review', line[:450]
    for line in lines:
        if re.search(r'until.{0,35}filled|on[ -]?going|額滿即止|長期招聘', line, re.I):
            return None, 'until-filled', line[:450]
    return None, 'unknown', ''


def posted_from(text: str) -> str | None:
    lines = clean_text(text).splitlines()
    for i, line in enumerate(lines):
        if re.search(r'posted (on|date)|posting date|advertised|刊登日期|發布日期|發佈日期', line, re.I):
            result = parse_date(' '.join(lines[i:i+2]))
            if result:
                return result
    return None


def canonical_url(url: str) -> str:
    bits = urlsplit(url)
    params = sorted((k, v) for k, v in parse_qsl(bits.query, keep_blank_values=True)
                    if not k.lower().startswith('utm_') and k not in ('lang', 'job-mail-subscribe-privacy'))
    return urlunsplit((bits.scheme.lower(), bits.netloc.lower(), bits.path, urlencode(params), bits.fragment))


def record(source: dict, title: str, url: str, reference: str = '', **values) -> dict:
    title = clean_text(title)
    reference = clean_text(reference)
    identity = reference.upper().replace(' ', '') if reference else canonical_url(url)
    key = hashlib.sha256((source['group'] + '\0' + identity).encode()).hexdigest()[:24]
    result = {'id': key, 'source_id': source['id'], 'institution': source['institution'], 'title': title,
              'reference': reference, 'url': url, 'source_url': source['url'], 'department': '',
              'employment_type': 'unknown', 'posted_date': None, 'deadline': None,
              'deadline_type': 'unknown', 'deadline_raw': '', 'description': '', 'match_text': '',
              'detail_complete': True, **values}
    result['description'] = clean_text(result['description'])
    return result


def content_hash(job: dict) -> str:
    fields = ['title', 'department', 'employment_type', 'posted_date', 'deadline', 'deadline_type', 'deadline_raw', 'description', 'match_text']
    payload = {key: job.get(key) for key in fields}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
