"""CPCE JAS public academic vacancy list (HKCC and SPEED shared employer)."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from .adapters import Batch, apply_deadline, body_text, soup_of
from .http import CrawlError
from .model import clean_text, parse_date, posted_from, record
from .rules import employment_of


def parse_cpce_list(source, html):
    soup = soup_of(html)
    table = soup.select_one('table.list-table')
    if not table or 'Initial screening date/Closing date' not in table.get_text(' ', strip=True):
        raise CrawlError('CPCE 招聘表格欄位未能確認。')
    jobs = []
    for row in table.select('tbody tr.list-row[data-id]'):
        cells = row.find_all('td', recursive=False)
        identifier = row.get('data-id', '')
        if len(cells) != 4 or not identifier.isdigit():
            raise CrawlError('CPCE 職位欄位或編號已改變。')
        department, title, review, reference = [clean_text(c.get_text(' ', strip=True)) for c in cells]
        if not title or not reference:
            raise CrawlError('CPCE 職位缺少標題或編號。')
        # The displayed date mixes initial screening/closing and is not a posted date.
        job = record(source, title, urljoin(source['url'], '/detail?id=' + identifier), reference,
                     department=department, detail_complete=False)
        job['employment_type'] = employment_of(title)
        jobs.append(job)
    if not jobs:
        # No current empty fixture: don't guess from absent rows.
        raise CrawlError('CPCE 沒有可確認的職位，需檢查是否改版。')
    if soup.select('.pagination a[href], a[rel="next"]'):
        raise CrawlError('CPCE 出現尚未支援的分頁；不能確認已取得完整名單。')
    return jobs


def parse_cpce_detail(job, html):
    soup = soup_of(html)
    blocks = soup.select('.main-content .jas_detail')
    if not blocks or not blocks[0].select_one('.jas_color_red h4'):
        raise CrawlError('CPCE 職位內文區塊未能確認。')
    for block in blocks:
        for node in block.select('a[href^="/apply"], form, button'):
            node.decompose()
    text = clean_text('\n'.join(body_text(block) for block in blocks))
    if len(text) < 100 or not re.search(r'\bDuties\b|\bQualifications\b', text, re.I):
        raise CrawlError('CPCE 職位詳情不足，保留列表及上次資料。')
    job.update(description=text, match_text=text, detail_complete=True)
    job['employment_type'] = employment_of(job['title'], body=text)
    job['posted_date'] = posted_from(text)
    apply_deadline(job, text)
    # CPCE wording gives a review date, not a hard closing date.
    if job['deadline_type'] == 'unknown':
        line = next((line for line in text.splitlines() if re.search(r'consideration of applications will commence', line, re.I)), '')
        if line and parse_date(line):
            job.update(deadline=parse_date(line), deadline_type='review', deadline_raw=line[:450])
    return job


def collect_cpce(source, client):
    result = Batch(jobs=parse_cpce_list(source, client.get(source['url'])), pages=1)
    for job in result.jobs:
        try:
            parse_cpce_detail(job, client.get(job['url']))
        except CrawlError as exc:
            result.complete = False
            result.errors.append(str(exc))
            if exc.stop_source:
                break
    return result
