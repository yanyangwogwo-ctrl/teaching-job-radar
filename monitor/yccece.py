"""Draft for root to integrate; inspected public fixtures on 2026-09-17."""
import json
import re
from urllib.parse import urljoin, parse_qs
from bs4 import BeautifulSoup
from monitor.adapters import Batch, soup_of, body_text, apply_deadline
from monitor.http import CrawlError
from monitor.model import record, clean_text, posted_from
from monitor.rules import employment_of


_JSON_STRING = r'"(?:[^"\\]|\\.)*"'

def parse_yccece_list(source, html):
    """Read string values from the public Nuxt state; never evaluate scripts."""
    soup = BeautifulSoup(html, 'html.parser')
    scripts = [n.get_text() for n in soup.select('script:not([src])')
               if n.get_text().startswith('window.__NUXT__=')]
    if len(scripts) != 1:
        raise CrawlError('耀中公開職位資料格式有變。')
    text = scripts[0]
    pattern = r'\{type:[^,{}]+,key:"DetailLink-[^"]+",content:\{([^{}]*)\}\}'
    jobs = {}
    for match in re.finditer(pattern, text):
        props = {m[1]: json.loads(m[2]) for m in
                 re.finditer(r'(title|link):(' + _JSON_STRING + ')', match[1])}
        link, title = props.get('link', ''), props.get('title', '')
        if not re.fullmatch(r'\?job=[a-z0-9-]+', link):
            continue
        if not title:
            raise CrawlError('耀中職位缺少標題。')
        key = parse_qs(link[1:])['job'][0]
        jobs[key] = record(source, title, urljoin(source['url'], link), key,
                           detail_complete=False)
    # A changed string alias/layout must become a warning, never silently omit jobs.
    all_links = {json.loads(m.group()) for m in re.finditer(_JSON_STRING, text)
                 if '?job=' in m.group()}
    if not jobs or all_links != {'?job=' + key for key in jobs}:
        raise CrawlError('耀中職位清單數量未能核對。')
    return list(jobs.values())


def parse_yccece_detail(job, payload):
    data = payload.get('data', {})
    if data.get('category') != 'job' or data.get('key') != job['reference']:
        raise CrawlError('耀中詳情與職位編號不符。')
    components = json.loads(data.get('components', '[]'))
    pieces = []
    for component in components:
        kind = component.get('type')
        if kind == 'CustomSpace':
            continue
        if kind != 'CustomText':
            raise CrawlError('耀中廣告含尚未識別的內容區塊。')
        content = component.get('content', {})
        for part in ('title', 'text'):
            html = content.get(part, {}).get('content', '')
            if html:
                pieces.append(body_text(soup_of(html)))
    body = '\n'.join(pieces)
    if len(body) < 100:
        raise CrawlError('耀中詳情內文過短。')
    job.update(title=clean_text(data['title']), description=body,
               match_text=body, detail_complete=True)
    job['employment_type'] = employment_of(job['title'], body=body)
    # API `time` is not labelled publication time; do not misrepresent it.
    job['posted_date'] = posted_from(body)
    apply_deadline(job, body)
    return job


def collect_yccece(source, client):
    result = Batch(jobs=parse_yccece_list(source, client.get(source['url'])), pages=1)
    for job in result.jobs:
        try:
            api = ('https://db.ycyw-edu.com/api/component/production/'
                   'static-detail-category/job/static-details/' + job['reference'])
            # These are literal public website-routing headers from the official JS.
            data = client.response(api, headers={'website': 'yccece-en',
                                                'api-key': 'api-key'}).json()
            parse_yccece_detail(job, data)
        except (CrawlError, ValueError, KeyError, TypeError) as error:
            result.complete = False
            result.errors.append(str(error) if isinstance(error, CrawlError)
                                 else '耀中詳情資料格式未能確認。')
            if isinstance(error, CrawlError) and error.stop_source:
                break
    return result
