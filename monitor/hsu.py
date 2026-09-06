"""HSU's official vacancy list and anonymously readable advertisements."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, parse_qs

from .adapters import soup_of, body_text, apply_deadline
from .http import CrawlError
from .model import record, clean_text, posted_from, parse_date
from .rules import employment_of


def parse_hsu_list(source, html):
    jobs = {}
    for link in soup_of(html).select('a[href]'):
        url = urljoin(source['url'], link['href'])
        parts = urlsplit(url)
        if parts.hostname != 'recruit.hsu.edu.hk' or parts.path != '/opening/content.php':
            continue
        key = parse_qs(parts.query).get('id', [''])[0]
        title = clean_text(link.get_text(' ', strip=True))
        if not re.fullmatch(r'[1-9]\d*', key) or not title:
            raise CrawlError('恒生大學職位連結缺少編號或名稱。')
        job = record(source, title, f'https://recruit.hsu.edu.hk/opening/content.php?id={key}', key, detail_complete=False)
        job['employment_type'] = employment_of(title)
        jobs[job['id']] = job
    if not jobs:
        raise CrawlError('恒生大學未提供預期職位連結；未當作零職位處理。')
    return list(jobs.values())


def parse_hsu_detail(job, html):
    text = body_text(soup_of(html))
    # Remove university-wide liberal-arts/critical-thinking copy before matching.
    marker = re.search(r'The\s+University\s+now\s+invites\s+applications?\b[^:]{0,1500}:', text, re.I)
    if not marker:
        # Some official ads omit the invitation sentence. Accept only the exact
        # listing title immediately before Ref, followed by a duties section.
        title_pattern = r'\s+'.join(re.escape(word) for word in job['title'].split())
        heading = re.search(title_pattern + r'\s*(?=\(Ref\s*:)', text, re.I)
        if not heading or not re.search(r'\bResponsibilities\b|\bDuties\b', text[heading.end():], re.I):
            raise CrawlError('恒生大學未提供預期公開職位內文。')
        text = text[heading.start():].strip()
    else:
        text = text[marker.end():].strip()
    reference = re.search(r'\(Ref\s*:\s*[^)]+\)', text, re.I)
    if not reference or reference.start() > 1200 or len(text) < 150:
        raise CrawlError('恒生大學廣告標題或職位編號格式有變。')
    title = clean_text(text[:reference.start()])
    job.update(title=title, description=text, match_text=text, detail_complete=True, posted_date=posted_from(text))
    job['employment_type'] = employment_of(title, body=text)
    department = re.split(r'\s+[–—-]\s+', title, maxsplit=1)
    if len(department) == 2:
        job['department'] = next((part for part in department if re.search(r'\bDepartment\b|\bSchool\b|\bOffice\b|\bRegistry\b', part, re.I)), '')
    explicit = re.search(r'apply\s+on\s+or\s+before\s+(\d{1,2}\s+[A-Za-z]+\s+20\d\s?\d)', text, re.I)
    if explicit:
        deadline = re.sub(r'\b(20\d)\s+(\d)\b', r'\1\2', explicit.group(1))
        if parse_date(deadline):
            apply_deadline(job, 'Application deadline: ' + deadline)
        else:
            raise CrawlError('恒生大學截止日期未能確認。')
    else:
        apply_deadline(job, text)
    return job
