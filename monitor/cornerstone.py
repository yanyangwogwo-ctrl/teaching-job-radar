"""Lingnan's published anonymous careers session and public advertisements."""
from __future__ import annotations

import json
import re
from datetime import datetime

from .adapters import Batch, soup_of, body_text, apply_deadline
from .http import CrawlError
from .model import record, clean_text, parse_date
from .rules import employment_of

ORIGIN = 'https://lingnan.csod.com'
SEARCH = 'https://uk.api.csod.com/rec-job-search/external/jobs'


def anonymous_headers(html):
    marker = re.search(r'\bcsod\.context\s*=\s*(?=\{)', html)
    try:
        context = json.JSONDecoder().raw_decode(html[marker.end():])[0] if marker else {}
        if (context.get('user') != -103 or context.get('corp') != 'lingnan'
                or context.get('endpoints', {}).get('cloud') != 'https://uk.api.csod.com/'
                or not isinstance(context.get('token'), str) or not 1 <= len(context['token']) <= 20000):
            raise ValueError('unexpected session')
    except (ValueError, TypeError, AttributeError):
        raise CrawlError('嶺南公開匿名工作階段格式有變，已停止。', stop_source=True) from None
    # Per-request only: never persist, log, or attach this token to robots.txt.
    return {'Authorization': 'Bearer ' + context['token'], 'CSOD-Accept-Language': 'en-US'}


def listing_date(value):
    if not value or value == '-':
        return None
    try:
        # The published en-US listing has an explicit M/D/YYYY locale.
        return datetime.strptime(value, '%m/%d/%Y').date().isoformat()
    except (ValueError, TypeError):
        raise CrawlError('嶺南清單日期格式有變，未推測刊登日期。') from None


def parse_lingnan_page(source, payload):
    if payload.get('status') != 'Success':
        raise CrawlError('嶺南公開搜尋未回報成功。')
    data = payload['data']
    total, rows = data['totalCount'], data['requisitions']
    if type(total) is not int or total < 0 or not isinstance(rows, list):
        raise CrawlError('嶺南公開搜尋數量格式有變。')
    jobs = []
    for row in rows:
        key, title = str(row['requisitionId']), clean_text(row['displayJobTitle'])
        if not re.fullmatch(r'[1-9]\d*', key) or not title:
            raise CrawlError('嶺南清單缺少職位編號或名稱。')
        job = record(source, title, f'{ORIGIN}/ux/ats/careersite/4/home/requisition/{key}?c=lingnan&lang=en-US', key,
                     posted_date=listing_date(row.get('postingEffectiveDate')), detail_complete=False)
        job['employment_type'] = employment_of(title)
        closing = listing_date(row.get('postingExpirationDate'))
        if closing:
            job.update(deadline=closing, deadline_type='closing', deadline_raw=row['postingExpirationDate'])
        # externalDescription can be an internal-description placeholder; fetch the public ad.
        jobs.append(job)
    return jobs, total


def parse_lingnan_detail(job, payload):
    if payload.get('status') != 200:
        raise CrawlError('嶺南公開廣告未回報成功。')
    fields = [item['fields'] for group in payload['data'] for item in group['items']]
    if len(fields) != 1 or str(fields[0].get('id')) != job['reference']:
        raise CrawlError('嶺南詳情與職位編號不一致。')
    row = fields[0]
    text = body_text(soup_of(row.get('ad') or ''))
    start = re.search(r'Applications\s+are\s+(?:now\s+)?invited\s+for\s+the\s+following\s+posts?\s*:', text, re.I)
    if start:
        text = text[start.end():].strip()
    if len(text) < 100 or not row.get('title') or 'INTERNAL JOB DESCRIPTION' in text:
        raise CrawlError('嶺南公開廣告缺少足夠內文。')
    job.update(title=clean_text(row['title']), description=text, match_text=text, detail_complete=True)
    job['employment_type'] = employment_of(job['title'], body=text)
    # A fixed application-by date takes precedence over the standard until-filled footer.
    explicit = re.search(r'submit\s+(?:your|their|an?)\s+applications?\s+by\s+(\d{1,2}\s+[A-Za-z]+\s+20\d{2})', text, re.I)
    if explicit and parse_date(explicit.group(1)):
        apply_deadline(job, 'Application deadline: ' + explicit.group(1))
    elif not job.get('deadline'):
        apply_deadline(job, text)
    return job


def collect_lingnan(source, client):
    headers = anonymous_headers(client.get(source['url']))
    result, indexed, expected = Batch(), {}, None
    try:
        for page in range(1, 31):
            query = dict(careerSiteId=4, careerSitePageId=4, pageNumber=page, pageSize=25,
                         cultureId=1, cultureName='en-US', searchText='', states=[], countryCodes=[],
                         cities=[], placeID='', radius=None, postingsWithinDays=None,
                         customFieldCheckboxKeys=[], customFieldDropdowns=[], customFieldRadios=[])
            jobs, total = parse_lingnan_page(source, client.search_json(SEARCH, query, headers))
            if expected is not None and total != expected:
                raise CrawlError('嶺南分頁期間職位總數改變，下一輪再核對。')
            expected = total
            previous = len(indexed)
            indexed.update({job['id']: job for job in jobs})
            result.pages += 1
            if total == 0 and not jobs:
                result.explicit_empty = True
                break
            if not jobs or len(indexed) == previous:
                raise CrawlError('嶺南分頁提前結束或重複，未當作完整清單。')
            if page * 25 >= total:
                break
        if len(indexed) != expected:
            raise CrawlError('嶺南實際職位數與官方總數不一致。')
    except (CrawlError, KeyError, ValueError, TypeError) as error:
        result.complete = False
        result.errors.append(str(error) if isinstance(error, CrawlError) else '嶺南公開清單格式有變。')
        if getattr(error, 'stop_source', False):
            result.jobs = list(indexed.values())
            return result
    result.jobs = list(indexed.values())
    failures = 0
    for job in result.jobs:
        try:
            url = f"{ORIGIN}/Services/API/ATS/CareerSite/4/JobRequisitions/{job['reference']}?useMobileAd=false&cultureId=1"
            parse_lingnan_detail(job, client.response(url, headers=headers).json())
            failures = 0
        except (CrawlError, KeyError, ValueError, TypeError, IndexError) as error:
            result.complete = False
            result.errors.append(str(error) if isinstance(error, CrawlError) else '嶺南公開廣告格式有變。')
            failures += 1
            if getattr(error, 'stop_source', False) or failures >= 3:
                break
    return result
