from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, parse_qs

from bs4 import BeautifulSoup

from .http import CrawlError
from .model import clean_text, record, parse_date, posted_from, deadline_from
from .rules import employment_of


@dataclass
class Batch:
    jobs: list[dict] = field(default_factory=list)
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    pages: int = 0
    explicit_empty: bool = False


def soup_of(html: str):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style, noscript'):
        node.decompose()
    return soup


def body_text(node) -> str:
    # Retain paragraph/row boundaries without splitting each inline span.
    copy = BeautifulSoup(str(node), 'html.parser')
    for element in copy.find_all(['br', 'p', 'li', 'tr', 'h1', 'h2', 'h3', 'h4']):
        element.insert_after('\n')
    return clean_text(copy.get_text(' ', strip=False))


def apply_deadline(job: dict, raw: str):
    date, kind, original = deadline_from(raw)
    if kind != 'unknown':
        job.update(deadline=date, deadline_type=kind, deadline_raw=original)


def parse_uow(source: dict, html: str) -> Batch:
    soup, result = soup_of(html), Batch(pages=1)
    for link in soup.select('.uw-tabs .tabs-title a[href^="#"]'):
        panel = soup.find(id=link['href'][1:])
        if not panel:
            result.complete = False
            result.errors.append('有職位連結但缺少對應內容區塊。')
            continue
        text = body_text(panel)
        ref = re.search(r'\[\s*Ref[.:\s]*(\d{4}/?(?:N?ACAD)\d+)\s*\]', text, re.I)
        if not ref:
            # Application forms are a separate accordion; not vacancies.
            if re.search(r'lecturer|tutor|instructor|officer|assistant|professor', link.get_text(), re.I):
                result.complete = False
                result.errors.append('職位區塊缺少可確認的編號。')
            continue
        title = clean_text(link.get_text(' ', strip=True))
        department_match = re.search(r'Faculty of [A-Za-z &]+', title)
        reference = re.sub(r'^(\d{4})(N?ACAD)', r'\1/\2', ref.group(1).upper())
        job = record(source, title, source['url'].rstrip('/') + '/' + link['href'], reference,
                     description=text, department=department_match.group(0).strip() if department_match else '')
        job['employment_type'] = employment_of(title, body=text)
        # Exclude the repeated faculty introduction, retaining the advertised
        # subject list, duties and qualifications for meaningful matching.
        start = re.search(r'We are currently recruiting|\bDuties\b', text, re.I)
        job['match_text'] = text[start.start():] if start else text
        job['posted_date'] = posted_from(text)
        apply_deadline(job, text)
        result.jobs.append(job)
    if not result.jobs:
        raise CrawlError('找不到可識別的職位區塊；未當作零職位處理。')
    return result


def parse_space_list(source: dict, html: str) -> list[dict]:
    soup, jobs = soup_of(html), []
    for table in soup.select('table[id^="list_"]'):
        college = table.find_previous('a', href=re.compile(r'/division/pt_list\.php'))
        department = clean_text(college.get_text(' ', strip=True)) if college else ''
        for row in table.select('tbody tr'):
            link = row.select_one('a[href*="job_dtls.php"]')
            if not link:
                continue
            cells = row.find_all('td', recursive=False)
            if len(cells) < 5:
                raise CrawlError('HKU SPACE 職位欄位已改變，請檢查讀取規則。')
            url = urljoin(source['url'], link['href'])
            reference = parse_qs(urlsplit(url).query).get('jcode', [''])[0]
            if not reference:
                raise CrawlError('職位缺少編號。')
            title = clean_text(link.get_text(' ', strip=True))
            # Listing gives a module, not the full job title. Always fetch detail.
            job = record(source, title, url, reference, department=department, posted_date=parse_date(cells[0].get_text(' ', strip=True)), employment_type='part-time', detail_complete=False)
            closing = clean_text(cells[3].get_text(' ', strip=True))
            apply_deadline(job, 'Closing date: ' + closing)
            jobs.append(job)
    if not jobs:
        raise CrawlError('HKU SPACE 未找到預期職位表格；未當作零職位處理。')
    return jobs


def parse_space_detail(job: dict, html: str) -> dict:
    soup = soup_of(html)
    content = soup.select_one('#cke_pastebin')
    title = content.select_one('h1') if content else None
    if not content or not title:
        raise CrawlError('HKU SPACE 職位詳情格式未能確認。')
    title_text = re.split(r'\[\s*RF\s*:', title.get_text(' ', strip=True), flags=re.I)[0].strip()
    # Strip apply/navigation elements, never store site-wide navigation.
    for node in content.select('center, table.lcontent'):
        node.decompose()
    text = body_text(content)
    if len(text) < 100:
        raise CrawlError('HKU SPACE 詳情內文過短，保留上次版本。')
    job.update(title=title_text, description=text, match_text=text, detail_complete=True)
    job['employment_type'] = employment_of(title_text, 'part-time', text)
    job['posted_date'] = posted_from(text) or job['posted_date']
    apply_deadline(job, text)
    return job


def parse_sce_list(source: dict, html: str) -> tuple[list[dict], list[str]]:
    soup, jobs = soup_of(html), []
    table = soup.select_one('table.table--job')
    if not table:
        raise CrawlError('HKBU-SCE 未找到預期招聘表格。')
    for row in table.select('tbody tr'):
        link, code = row.select_one('.job-title a[href]'), row.select_one('.job-code')
        if not link:
            continue
        cells = row.find_all('td', recursive=False)
        if len(cells) < 3:
            raise CrawlError('HKBU-SCE 招聘表格欄位已改變。')
        reference = re.sub(r'^職位編號\s*', '', code.get_text(' ', strip=True)) if code else ''
        job = record(source, link.get_text(' ', strip=True), urljoin(source['url'], link['href']), reference,
                     posted_date=parse_date(cells[1].get_text(' ', strip=True)), detail_complete=False)
        job['employment_type'] = employment_of(job['title'])
        apply_deadline(job, 'Closing date: ' + cells[2].get_text(' ', strip=True))
        jobs.append(job)
    if not jobs:
        raise CrawlError('HKBU-SCE 表格沒有可識別職位，需確認是否改版。')
    pages = sorted({urljoin(source['url'], a['href']) for a in soup.select('.pagination a[href]')
                    if 'paged=' in a['href'] and parse_qs(urlsplit(a['href']).query).get('paged') != ['1']})
    return jobs, pages


def parse_sce_detail(job: dict, html: str) -> dict:
    soup = soup_of(html)
    content = soup.select_one('.notes-box .mcec')
    if not content:
        raise CrawlError('HKBU-SCE 詳情頁缺少職位內容區塊。')
    for node in content.select('.btn-apply'):
        node.decompose()
    text = body_text(content)
    if len(text) < 100:
        raise CrawlError('HKBU-SCE 詳情內文過短，保留上次版本。')
    job.update(description=text, match_text=text, detail_complete=True)
    job['employment_type'] = employment_of(job['title'], body=text)
    apply_deadline(job, text)
    return job


def collect(source: dict, client) -> Batch:
    adapter = source['adapter']
    if adapter in ('sfu', 'hku', 'polyu', 'hkbu_oracle', 'hksyu', 'hkust', 'cityu', 'cuhk'):
        from .official import collect_official
        return collect_official(source, client)
    if adapter == 'uowchk':
        return parse_uow(source, client.get(source['url']))
    if adapter == 'hkuspace':
        result = Batch(jobs=parse_space_list(source, client.get(source['url'])), pages=1)
        parse_detail = parse_space_detail
    elif adapter == 'hkbu_sce':
        result, queue, visited, indexed = Batch(), [source['url']], set(), {}
        try:
            while queue:
                url = queue.pop(0)
                if url in visited:
                    continue
                if len(visited) >= 20:
                    raise CrawlError('分頁超過安全上限，尚未完成檢索。')
                visited.add(url)
                jobs, pages = parse_sce_list(source, client.get(url))
                result.pages += 1
                for job in jobs:
                    indexed[job['id']] = job
                queue.extend(p for p in pages if p not in visited and p not in queue)
        except CrawlError as error:
            result.complete = False
            result.errors.append(str(error))
            if error.stop_source:
                result.jobs = list(indexed.values())
                return result
        result.jobs = list(indexed.values())
        parse_detail = parse_sce_detail
    elif adapter == 'generic_html':
        return collect_generic(source, client)
    else:
        raise CrawlError('此來源的讀取模組尚未接通。')
    for job in result.jobs:
        try:
            parse_detail(job, client.get(job['url']))
        except CrawlError as error:
            result.complete = False
            result.errors.append(str(error))
            # Stop on explicit denials/challenges, not retry against the same site.
            if error.stop_source:
                break
    return result


def collect_generic(source: dict, client) -> Batch:
    """Config-only support for new, ordinary HTML lists with explicit selectors.

    Deliberately no arbitrary scripts, login, API guessing or browser automation.
    Pagination must use the declared next selector; cycles are failures.
    """
    selectors = source.get('selectors', {})
    if not all(selectors.get(k) for k in ('row', 'title', 'link', 'detail_body')):
        raise CrawlError('一般 HTML 來源必須設定 row、title、link、detail_body。')
    result, url, visited, ids = Batch(), source['url'], set(), set()
    while url:
        if url in visited or len(visited) >= 20:
            raise CrawlError('分頁循環或超過上限，停止該來源。')
        visited.add(url)
        soup = soup_of(client.get(url))
        rows = soup.select(selectors['row'])
        if not rows:
            if selectors.get('empty') and soup.select_one(selectors['empty']):
                result.explicit_empty = True
                break
            raise CrawlError('來源沒有預期的職位列或明確的零職位標示。')
        for row in rows:
            def field(key):
                node = row.select_one(selectors[key]) if selectors.get(key) else None
                return clean_text(node.get_text(' ', strip=True)) if node else ''
            anchor = row.select_one(selectors['link'])
            if not anchor or not anchor.get('href') or not field('title'):
                raise CrawlError('職位列缺少標題或連結。')
            job = record(source, field('title'), urljoin(url, anchor['href']), field('reference'),
                         department=field('department'), posted_date=parse_date(field('posted_date')))
            if job['id'] in ids:
                continue
            ids.add(job['id'])
            detail = soup_of(client.get(job['url'])).select_one(selectors['detail_body'])
            if not detail:
                raise CrawlError('詳情頁沒有指定內容區塊。')
            job['description'] = job['match_text'] = body_text(detail)
            job['employment_type'] = employment_of(job['title'], field('employment_type'), job['description'])
            apply_deadline(job, 'Closing date: ' + field('deadline') + '\n' + job['description'])
            result.jobs.append(job)
        result.pages += 1
        next_link = soup.select_one(selectors['next']) if selectors.get('next') else None
        url = urljoin(url, next_link['href']) if next_link and next_link.get('href') else None
    return result
