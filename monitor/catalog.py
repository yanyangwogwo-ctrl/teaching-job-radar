"""Verified configurable HTML/PDF feeds for additional tertiary employers.

Selectors describe an inspected official page, never a heuristic web search.
Read the complete list before details so a failed attachment preserves titles.
"""
from __future__ import annotations

import re
import json
import unicodedata
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlsplit, parse_qs

from .adapters import Batch, soup_of, body_text, apply_deadline
from .http import CrawlError
from .model import clean_text, record, parse_date, posted_from
from .rules import employment_of


def node_at(node, selector):
    return node if selector == ':self' else node.select_one(selector) if selector else None


def field_at(node, selector):
    found = node_at(node, selector)
    return clean_text(found.get_text(' ', strip=True)) if found else ''


def source_date(value, source):
    # Numeric D/M/Y is enabled only for a verified source locale.
    value = ''.join(c for c in str(value or '') if unicodedata.category(c) != 'Cf')
    parsed = parse_date(value)
    if parsed or source.get('date_order') != 'DMY':
        return parsed
    match = re.fullmatch(r'\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](20\d{2})\s*', value or '')
    if match:
        day, month, year = map(int, match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            pass
    return None


def parse_catalog_page(source, html, url=None):
    url = url or source['url']
    selectors = source['selectors']
    soup, jobs = soup_of(html), []
    rows = soup.select(selectors['row'])
    for row in rows:
        anchor = node_at(row, selectors['link'])
        if not anchor or not anchor.get('href'):
            raise CrawlError('職位列缺少官方詳情連結。')
        title = field_at(row, selectors['title'])
        if not title:
            raise CrawlError('職位列缺少標題。')
        target = urljoin(url, anchor['href'])
        if source['id'] == 'hkct' and urlsplit(target).path.startswith('/abouthkct/'):
            # Published unlocalized links redirect to an HTTP URL. Use the
            # verified HTTPS Chinese route, whose advertisements retain English.
            target = target.replace('/abouthkct/', '/tc/abouthkct/', 1)
        reference = field_at(row, selectors.get('reference'))
        if source.get('reference_query'):
            reference = parse_qs(urlsplit(target).query).get(source['reference_query'], [''])[0]
            if not reference:
                raise CrawlError('職位連結缺少預期編號。')
        stamp = node_at(row, selectors.get('posted_date'))
        posted = (stamp.get('datetime') or stamp.get_text(' ', strip=True)) if stamp else ''
        job = record(source, title, target, reference,
                     department=field_at(row, selectors.get('department')),
                     posted_date=source_date(posted, source), detail_complete=False)
        explicit = field_at(row, selectors.get('employment_type')) or source.get('employment_type', '')
        job['employment_type'] = employment_of(title, explicit)
        closing = field_at(row, selectors.get('deadline'))
        parsed = source_date(closing, source)
        if parsed:
            job.update(deadline=parsed, deadline_type='closing', deadline_raw=closing)
        else:
            apply_deadline(job, 'Closing date: ' + closing)
        jobs.append(job)
    empty = bool(not rows and selectors.get('empty') and soup.select_one(selectors['empty']))
    if not jobs and not empty:
        raise CrawlError('未找到預期職位列或明確的零職位標示；可能需要調整讀取規則。')
    pages = [urljoin(url, a['href']) for a in soup.select(selectors['next']) if a.get('href')] if selectors.get('next') else []
    return jobs, list(dict.fromkeys(pages)), empty


def parse_catalog_detail(source, job, html):
    selectors = source['selectors']
    soup = soup_of(html)
    content = soup.select_one(selectors['detail_body'])
    if not content:
        raise CrawlError('詳情頁缺少已確認的職位內容區塊。')
    for selector in source.get('detail_remove', []):
        for element in content.select(selector):
            element.decompose()
    text = body_text(content)
    if len(text) < 100:
        raise CrawlError('職位詳情內文過短，需核對原文。')
    title = field_at(soup, selectors.get('detail_title')) or job['title']
    job.update(title=title, description=text, match_text=text, detail_complete=True)
    department = field_at(soup, selectors.get('detail_department'))
    if department:
        job['department'] = department
    job['employment_type'] = employment_of(title, job['employment_type'], text)
    if source['id'] == 'hpshcc' and re.search(r'considered for the post of[^.\n]{0,100}part[- ]time Lecturer', text, re.I):
        # The advertised post explicitly accepts a PT appointment alternative.
        job['employment_type'] = 'mixed' if job['employment_type'] == 'full-time' else 'part-time'
    job['posted_date'] = posted_from(text) or job['posted_date']
    posted_node = node_at(soup, selectors.get('detail_posted'))
    if posted_node:
        raw = posted_node.get('content') or posted_node.get('datetime') or posted_node.get_text(' ', strip=True)
        if source.get('detail_posted_format') == 'successfactors_utc':
            try:
                stamp = datetime.strptime(raw, '%a %b %d %H:%M:%S UTC %Y').replace(tzinfo=timezone.utc)
                job['posted_date'] = stamp.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
            except ValueError:
                raise CrawlError('招聘平台的刊登日期格式改變，需核對。') from None
        else:
            job['posted_date'] = source_date(raw, source) or job['posted_date']
    apply_deadline(job, text)
    return job


def read_details(source, client, result, detail=None):
    failures = 0
    for job in result.jobs:
        if job['detail_complete']:
            continue
        try:
            if detail:
                detail(job)
            elif source.get('detail_mode') == 'gcc_pdf':
                from .official import parse_pdf_detail
                html = client.get(job['url'])
                # DearFlip embeds a JSON data literal; never execute the script.
                matches = re.finditer(r'window\.df_option_\d+\s*=\s*', html)
                urls = []
                for match in matches:
                    data, _ = json.JSONDecoder().raw_decode(html[match.end():])
                    if isinstance(data, dict) and isinstance(data.get('source'), str):
                        urls.append(urljoin(job['url'], data['source']))
                urls = list(dict.fromkeys(urls))
                if len(urls) != 1:
                    raise CrawlError('招聘頁未提供唯一可確認的 PDF 廣告。')
                posted = job['posted_date']
                parse_pdf_detail(job, client.get_bytes(urls[0]))
                job['posted_date'] = job['posted_date'] or posted
                job['employment_type'] = employment_of(job['title'], job['employment_type'], job['description'])
            elif source.get('detail_mode') == 'pdf' or urlsplit(job['url']).path.lower().endswith('.pdf'):
                from .official import parse_pdf_detail
                posted = job['posted_date']
                parse_pdf_detail(job, client.get_bytes(job['url']))
                job['posted_date'] = job['posted_date'] or posted
                job['employment_type'] = employment_of(job['title'], job['employment_type'], job['description'])
                if source['id'] == 'hkac':
                    # The college introduction mentions its educational philosophy
                    # in every advertisement; it is not a philosophy teaching role.
                    text = job['description']
                    start = re.search(r'Requirements\s*:|Job\s+Description\s*:|Qualifications\s*:', text, re.I)
                    if start:
                        job['match_text'] = text[start.start():]
                        job['description'] = job['title'] + '\n' + text[start.start():]
            else:
                parse_catalog_detail(source, job, client.get(job['url']))
            failures = 0
        except (CrawlError, ValueError, KeyError, IndexError) as error:
            result.complete = False
            result.errors.append(str(error) if isinstance(error, CrawlError) else '職位詳情格式改變，保留清單及舊記錄。')
            failures += 1
            if getattr(error, 'stop_source', False) or failures >= 3:
                break
    return result


def collect_catalog(source, client):
    result, queue, visited, indexed = Batch(), list(source.get('list_urls') or [source['url']]), set(), {}
    try:
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            if len(visited) >= source.get('max_pages', 30):
                raise CrawlError('職位清單分頁超過上限，尚未完成。')
            visited.add(url)
            jobs, pages, empty = parse_catalog_page(source, client.get(url), url)
            indexed.update({job['id']: job for job in jobs})
            result.pages += 1
            result.explicit_empty |= empty
            queue.extend(p for p in pages if p not in queue and p not in visited)
    except CrawlError as error:
        result.complete = False
        result.errors.append(str(error))
        if error.stop_source:
            result.jobs = list(indexed.values())
            return result
    result.jobs = list(indexed.values())
    if result.jobs:
        result.explicit_empty = False
    return read_details(source, client, result)


def collect_inline(source, client):
    selectors = source['selectors']
    soup = soup_of(client.get(source['url']))
    result = Batch(pages=1)
    for panel in soup.select(selectors['row']):
        title = field_at(panel, selectors['title'])
        body = node_at(panel, selectors['detail_body'])
        if not title or not body or len(body.get_text()) < 100:
            raise CrawlError('同頁招聘廣告缺少標題或完整內容。')
        text = body_text(body)
        # Several ads share one URL and have no published reference. The title
        # is the stable internal key, but is not displayed as an invented ref.
        job = record(source, title, source['url'], title, description=text, match_text=text)
        job['reference'] = ''
        job['employment_type'] = employment_of(title, body=text)
        job['posted_date'] = posted_from(text)
        apply_deadline(job, text)
        if job['id'] in {j['id'] for j in result.jobs}:
            raise CrawlError('同頁廣告出現重複標題，需要核對職位識別方式。')
        result.jobs.append(job)
    if not result.jobs:
        raise CrawlError('官方頁面缺少預期同頁招聘廣告，未當作零職位。')
    return result
