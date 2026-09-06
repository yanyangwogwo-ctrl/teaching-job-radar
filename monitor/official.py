"""Adapters for verified anonymous official HTML, PDF and JSON sources."""
from __future__ import annotations

import io
import json
import re
import ast
from datetime import datetime
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode, unquote
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from .adapters import Batch, soup_of, body_text, apply_deadline
from .http import CrawlError
from .model import clean_text, record, parse_date, posted_from
from .rules import employment_of


def local_date(value):
    if value and 'T' in str(value):
        try:
            stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if stamp.tzinfo:
                return stamp.astimezone(ZoneInfo('Asia/Hong_Kong')).date().isoformat()
        except ValueError:
            pass
    return parse_date(value)


def text_at(node, selector):
    found = node.select_one(selector)
    return clean_text(found.get_text(' ', strip=True)) if found else ''


def require_jobs(jobs, name):
    if not jobs:
        raise CrawlError(name + ' 找不到預期職位列，需確認是否改版。')
    return jobs


def parse_sfu(source, html):
    jobs = []
    for wrap in soup_of(html).select('.accordion-wrap'):
        title = text_at(wrap, '.accordion-btn')
        content = wrap.select_one('.accordion-content')
        reference = re.search(r'\(Ref\.?\s*:\s*([^)]*)\)', title, re.I)
        if not content or not reference:
            continue
        title = clean_text(title[:reference.start()])
        text = body_text(content)
        job = record(source, title, source['url'], reference.group(1), description=text, match_text=text)
        job['department'] = title.rsplit(', ', 1)[1] if ', ' in title else ''
        job['employment_type'] = employment_of(title, body=text)
        job['posted_date'] = posted_from(text)
        apply_deadline(job, text)
        jobs.append(job)
    return Batch(jobs=require_jobs(jobs, 'SFU'), pages=1)


def parse_hku_list(source, html):
    soup, jobs = soup_of(html), []
    for row in soup.select('#search-results-content tr'):
        anchor = row.select_one('a.job-link[href]')
        if not anchor:
            continue
        cells = row.find_all('td', recursive=False)
        if len(cells) != 4:
            raise CrawlError('HKU 職位表格欄位有變。')
        job = record(source, anchor.get_text(' ', strip=True), urljoin(source['url'], anchor['href']), text_at(row, '.job-externalJobNo'),
                     department=cells[2].get_text(' ', strip=True), detail_complete=False)
        stamp = cells[3].select_one('time[datetime]')
        if stamp:
            job.update(deadline=local_date(stamp['datetime']), deadline_type='closing', deadline_raw=cells[3].get_text(' ', strip=True))
        job['employment_type'] = employment_of(job['title'])
        jobs.append(job)
    more = soup.select_one('#search-results a.more-link[href]')
    return require_jobs(jobs, 'HKU'), urljoin(source['url'], more['href']) if more else None


def parse_hku_detail(job, html):
    soup = soup_of(html)
    content = soup.select_one('#job-details')
    if not content or len(content.get_text()) < 100:
        raise CrawlError('HKU 詳情頁的內文格式未能確認。')
    text = body_text(content)
    title = text_at(soup, '#job-content [itemprop=title]') or job['title']
    job.update(title=title, description=text, match_text=text, detail_complete=True)
    job['employment_type'] = employment_of(title, ' '.join(n.get_text(' ', strip=True) for n in soup.select('#job-content .work-type')), text)
    for item, field in [('datePosted', 'posted_date'), ('validThrough', 'deadline')]:
        node = soup.select_one(f'#job-content [itemprop={item}] time[datetime]')
        if node:
            job[field] = local_date(node['datetime'])
            if field == 'deadline':
                job.update(deadline_type='closing', deadline_raw=node.get_text(' ', strip=True))
    if not job.get('deadline'):
        apply_deadline(job, text)
    return job


def parse_polyu_list(source, html):
    soup, jobs = soup_of(html), []
    for row in soup.select('tr.ITS_clickableTableRow[data-href]'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 4:
            raise CrawlError('PolyU 表格欄位有變。')
        values = [clean_text(c.get_text(' ', strip=True)) for c in cells]
        job = record(source, values[1], urljoin(source['url'], row['data-href']), values[3], department=values[0],
                     deadline_raw=values[2], deadline_type='screening-or-closing', detail_complete=False)
        job['employment_type'] = employment_of(job['title'])
        jobs.append(job)
    return require_jobs(jobs, 'PolyU')


def parse_polyu_detail(job, html):
    soup = soup_of(html)
    content = soup.select_one('main .ITS_Content_RichTextEditor')
    title = text_at(soup, 'main h2')
    if not content or not title:
        raise CrawlError('PolyU 職位詳情格式未能確認。')
    text = body_text(content)
    job.update(title=title, department=text_at(soup, '.hro_topic') or job['department'], description=text, match_text=text,
               detail_complete=True, posted_date=posted_from(text))
    job['employment_type'] = employment_of(title, body=text)
    apply_deadline(job, text)
    return job


def parse_hksyu_list(source, html):
    soup, jobs = soup_of(html), []
    for card in soup.select('.accordion > .card'):
        if text_at(card, '.card-header') not in ('學術職位', 'Academic Posts'):
            continue
        for row in card.select('table.table-striped tbody tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) != 4:
                raise CrawlError('樹仁學術職位表格欄位有變。')
            link = cells[1].select_one('a[href]')
            if not link:
                raise CrawlError('樹仁職位缺少詳情連結。')
            work = cells[2].get_text(' ', strip=True)
            work = work.replace('FT', 'full-time').replace('PT', 'part-time')
            job = record(source, link.get_text(' ', strip=True), urljoin(source['url'], link['href']),
                         department=cells[0].get_text(' ', strip=True), detail_complete=False)
            job['employment_type'] = employment_of(job['title'], work)
            apply_deadline(job, 'Closing date: ' + cells[3].get_text(' ', strip=True))
            jobs.append(job)
    return require_jobs(jobs, '樹仁')


def parse_pdf_detail(job, data):
    if not data.startswith(b'%PDF-'):
        raise CrawlError('詳情未提供預期的 PDF。')
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted or len(reader.pages) > 30:
            raise ValueError('unsupported PDF')
        text = clean_text('\n'.join(page.extract_text() or '' for page in reader.pages))
        if len(text) < 100:
            raise ValueError('no readable text')
    except Exception:
        raise CrawlError('PDF 未能抽取足夠文字，需人手查看原文。') from None
    job.update(description=text, match_text=text, detail_complete=True, posted_date=posted_from(text))
    apply_deadline(job, text)
    return job


def parse_hkust_list(source, html):
    soup, jobs = soup_of(html), []
    for card in soup.select('.view-id-job_listing.view-display-id-block_2 .job-card'):
        link = card.select_one('h3 a[href]')
        reference = re.search(r'Job ID\s*:\s*(\d+)', card.get_text(' ', strip=True))
        if not link or not reference:
            raise CrawlError('HKUST 職位卡片缺少標題或編號。')
        job = record(source, link.get_text(' ', strip=True), urljoin(source['url'], link['href']), reference.group(1), detail_complete=False)
        for info in card.select('.info-row p'):
            icon = info.select_one('img[alt]')
            label = icon['alt'] if icon else ''
            stamp = info.select_one('time[datetime]')
            if label == 'Department':
                job['department'] = info.get_text(' ', strip=True)
            elif label == 'Open Date' and stamp:
                job['posted_date'] = local_date(stamp['datetime'])
            elif label == 'Closing Date' and stamp:
                job.update(deadline=local_date(stamp['datetime']), deadline_type='closing', deadline_raw=info.get_text(' ', strip=True))
        job['employment_type'] = employment_of(job['title'])
        jobs.append(job)
    return require_jobs(jobs, 'HKUST')


def parse_cityu_list(source, html):
    soup, jobs = soup_of(html), []
    for row in soup.select('#MainContent_gvJobAcad tr'):
        link = row.select_one('a[id*="_hlJob_"][href]')
        if not link:
            continue
        cells = row.find_all('td', recursive=False)
        url = urljoin(source['url'], link['href'])
        # Official older links use HTTP; present their same host/path over HTTPS.
        if url.startswith('http://www.cityu.edu.hk/'):
            url = 'https://' + url[len('http://'):]
        reference = parse_qs(urlsplit(url).query).get('ref', [''])[0]
        if not reference or len(cells) < 3:
            raise CrawlError('CityU 職位欄位或編號有變。')
        raw = text_at(row, 'span[id*="_lblDateClose_"]')
        job = record(source, link.get_text(' ', strip=True), url, reference, department=cells[1].get_text(' ', strip=True),
                     deadline_raw=raw, deadline_type='screening-or-closing', detail_complete=False)
        if re.search(r'until.{0,55}filled', raw, re.I):
            job['deadline_type'] = 'until-filled'
        job['employment_type'] = employment_of(job['title'])
        jobs.append(job)
    return require_jobs(jobs, 'CityU')


def parse_hkbu_page(source, payload):
    wrapper = payload['items'][0]
    jobs = []
    for row in wrapper['requisitionList']:
        key = str(row['Id'])
        url = source['url'].rsplit('/jobs', 1)[0] + '/job/' + key
        job = record(source, row['Title'], url, key, posted_date=local_date(row.get('PostedDate')),
                     department=row.get('Department') or '', detail_complete=False)
        job['employment_type'] = employment_of(job['title'], row.get('JobSchedule') or '')
        jobs.append(job)
    return jobs, int(wrapper['TotalJobsCount'])


def parse_hkbu_detail(job, row):
    if str(row.get('Id')) != job['reference']:
        raise CrawlError('HKBU 詳情與職位編號不一致。')
    text = body_text(soup_of('\n'.join(row.get(k) or '' for k in ('ExternalDescriptionStr', 'ExternalQualificationsStr', 'ExternalResponsibilitiesStr'))))
    if len(text) < 100:
        raise CrawlError('HKBU 詳情缺少足夠內文。')
    job.update(title=row.get('Title') or job['title'], description=text, match_text=text, detail_complete=True,
               department=row.get('Department') or job['department'])
    job['employment_type'] = employment_of(job['title'], row.get('JobSchedule') or '', text)
    job['posted_date'] = local_date(row.get('ExternalPostedStartDate')) or job['posted_date']
    closing = local_date(row.get('ExternalPostedEndDate'))
    if closing:
        job.update(deadline=closing, deadline_type='closing', deadline_raw=row['ExternalPostedEndDate'])
    else:
        apply_deadline(job, text)
    return job


def parse_cuhk_detail(job, html):
    # Read data literals, never execute the page's JavaScript.
    keys_match = re.search(r'descRequisition\s*:\s*\{.*?_hlid\s*:\s*(\[[^\r\n]*?\])', html, re.S)
    values_match = re.search(r"fillList\('requisitionDescriptionInterface',\s*'descRequisition',\s*(\[[^\r\n]*\])\);", html)
    if not keys_match or not values_match:
        raise CrawlError('CUHK 詳情頁的公開資料欄位未能確認。')
    try:
        keys, values = ast.literal_eval(keys_match.group(1)), ast.literal_eval(values_match.group(1))
        if len(keys) != len(values) or not all(isinstance(v, str) for v in keys + values):
            raise ValueError('shape')
        row = dict(zip(keys, (unquote(v[3:]) if v.startswith('!*!') else v for v in values)))
    except (ValueError, SyntaxError):
        raise CrawlError('CUHK 公開資料格式有變，未執行網頁程式。') from None
    if row.get('reqlistitem.contestnumber') != job['reference']:
        raise CrawlError('CUHK 詳情與職位編號不一致。')
    text = body_text(soup_of(row.get('reqlistitem.description', '')))
    if len(text) < 100:
        raise CrawlError('CUHK 詳情缺少足夠內文。')
    job.update(title=row.get('reqlistitem.title') or job['title'], department=row.get('reqlistitem.organization') or job['department'],
               description=text, match_text=text, detail_complete=True, posted_date=posted_from(text))
    job['employment_type'] = employment_of(job['title'], body=text)
    apply_deadline(job, text)
    return job


def collect_cuhk(source, client):
    result, indexed, page, expected = Batch(), {}, 1, None
    endpoint = urljoin(source['url'], '/careersection/rest/jobboard/searchjobs?lang=en&portal=10115020119')
    try:
        while True:
            if page > 30:
                raise CrawlError('CUHK 分頁超過安全上限。', stop_source=True)
            payload = {'multilineEnabled': True, 'sortingSelection': {'sortBySelectionParam': '3', 'ascendingSortingOrder': 'false'},
                       'fieldData': {'fields': {'KEYWORD': '', 'JOB_TITLE': ''}, 'valid': True},
                       'filterSelectionParam': {'searchFilterSelections': []}, 'pageNo': page}
            data = client.search_json(endpoint, payload, {'tz': 'GMT+08:00', 'tzname': 'Asia/Hong_Kong'})
            paging = data['pagingData']
            total, size = int(paging['totalCount']), int(paging['pageSize'])
            if size < 1 or int(paging['currentPageNo']) != page or (expected is not None and total != expected):
                raise CrawlError('CUHK 分頁資訊改變，暫不判定職位消失。')
            expected = total
            for row in data['requisitionList']:
                fields, reference = row['column'], row['contestNo']
                if len(fields) != 4 or fields[1] != reference or int(row['linkedColumn']) != 0:
                    raise CrawlError('CUHK 搜尋欄位次序有變。')
                url = urljoin(source['url'], 'jobdetail.ftl?' + urlencode({'job': reference, 'lang': 'en'}))
                job = record(source, fields[0], url, reference, department=fields[2], detail_complete=False,
                             unposting_raw=fields[3])
                job['employment_type'] = employment_of(job['title'])
                indexed[job['id']] = job
            result.pages += 1
            if page * size >= total:
                result.explicit_empty = total == 0
                break
            page += 1
        if len(indexed) != expected:
            result.complete = False
            result.errors.append(f'已讀完 {page} 頁，但找到 {len(indexed)} 個職位，系統顯示 {expected}；暫不判定舊職位消失。')
    except (CrawlError, KeyError, ValueError, IndexError) as error:
        result.complete = False
        result.errors.append(str(error) if isinstance(error, CrawlError) else 'CUHK 公開搜尋格式有變。')
        if getattr(error, 'stop_source', False):
            result.jobs = list(indexed.values())
            return result
    result.jobs = list(indexed.values())
    consecutive = 0
    for job in result.jobs:
        try:
            parse_cuhk_detail(job, client.get(job['url']))
            consecutive = 0
        except CrawlError as error:
            result.complete = False
            result.errors.append(str(error))
            consecutive += 1
            if error.stop_source or consecutive >= 3:
                break
    return result


def collect_official(source, client):
    adapter = source['adapter']
    if adapter == 'lingnan':
        from .cornerstone import collect_lingnan
        return collect_lingnan(source, client)
    if adapter == 'cuhk':
        return collect_cuhk(source, client)
    if adapter == 'sfu':
        return parse_sfu(source, client.get(source['url']))
    result = Batch()
    if adapter == 'hkbu_oracle':
        origin = 'https://' + urlsplit(source['url']).hostname
        api = origin + '/hcmRestApi/resources/latest/'
        offset, total, indexed = 0, None, {}
        try:
            while total is None or offset < total:
                if result.pages >= 20:
                    raise CrawlError('HKBU 分頁超過上限。', stop_source=True)
                query = urlencode({'finder': f'findReqs;siteNumber=CX_1,limit=100,offset={offset},sortBy=POSTING_DATES_DESC', 'expand': 'requisitionList', 'onlyData': 'true'})
                jobs, current_total = parse_hkbu_page(source, json.loads(client.get(api + 'recruitingCEJobRequisitions?' + query)))
                if total is not None and total != current_total:
                    raise CrawlError('HKBU 分頁期間職位總數改變，下一輪再核對。')
                total = current_total
                if not jobs and total:
                    raise CrawlError('HKBU 分頁提前結束。')
                for job in jobs:
                    indexed[job['id']] = job
                offset += len(jobs)
                result.pages += 1
                if not total:
                    result.explicit_empty = True
                    break
            if len(indexed) != total:
                raise CrawlError('HKBU 職位數目與系統總數不一致。')
        except (CrawlError, KeyError, ValueError, IndexError) as error:
            result.complete = False
            result.errors.append(str(error) if isinstance(error, CrawlError) else 'HKBU 公開資料格式有變。')
            if getattr(error, 'stop_source', False):
                result.jobs = list(indexed.values())
                return result
        result.jobs = list(indexed.values())
        detail = lambda job: parse_hkbu_detail(job, json.loads(client.get(api + 'recruitingCEJobRequisitionDetails/' + job['reference'] + '?expand=all&onlyData=true')))
    elif adapter == 'hku':
        url, visited, indexed = source['url'], set(), {}
        try:
            while url:
                if url in visited or len(visited) >= 20:
                    raise CrawlError('HKU 分頁循環或超過上限。', stop_source=True)
                visited.add(url)
                jobs, url = parse_hku_list(source, client.get(url))
                indexed.update({j['id']: j for j in jobs})
                result.pages += 1
        except CrawlError as error:
            result.complete = False
            result.errors.append(str(error))
            if error.stop_source:
                result.jobs = list(indexed.values())
                return result
        result.jobs = list(indexed.values())
        detail = lambda job: parse_hku_detail(job, client.get(job['url']))
    else:
        from .hsu import parse_hsu_list, parse_hsu_detail
        parsers = {'polyu': parse_polyu_list, 'hksyu': parse_hksyu_list, 'hkust': parse_hkust_list, 'cityu': parse_cityu_list, 'hsu': parse_hsu_list}
        result.jobs = parsers[adapter](source, client.get(source['url']))
        result.pages = 1
        if adapter == 'cityu':
            # Listing is useful, but challenged details are not a full-text feed.
            # No access retry against those detail hosts after the audit denial.
            result.complete = False
            result.errors = ['已讀取官方清單；詳情平台要求存取驗證，內文及相關度未能完整核對。']
            return result
        if adapter == 'hkust':
            from .peoplesoft import fetch_hkust_detail, HOST
            restricted = [job for job in result.jobs if urlsplit(job['url']).hostname != HOST]
            if restricted:
                result.complete = False
                result.errors.append(f'{len(restricted)} 則職位的詳情平台仍有存取限制；其餘舊招聘平台廣告另行讀取。')
            failures = 0
            for job in result.jobs:
                if urlsplit(job['url']).hostname != HOST:
                    continue
                try:
                    fetch_hkust_detail(job, client)
                    failures = 0
                except (CrawlError, KeyError, ValueError) as error:
                    result.complete = False
                    result.errors.append(str(error) if isinstance(error, CrawlError) else 'HKUST 舊招聘平台格式有變。')
                    failures += 1
                    if getattr(error, 'stop_source', False) or failures >= 3:
                        break
            return result
        if adapter == 'hksyu':
            detail = lambda job: parse_pdf_detail(job, client.get_bytes(job['url']))
        elif adapter == 'hsu':
            detail = lambda job: parse_hsu_detail(job, client.get(job['url']))
        else:
            detail = lambda job: parse_polyu_detail(job, client.get(job['url']))
    consecutive_errors = 0
    for job in result.jobs:
        try:
            detail(job)
            consecutive_errors = 0
        except (CrawlError, KeyError, ValueError) as error:
            result.complete = False
            result.errors.append(str(error) if isinstance(error, CrawlError) else '詳情資料格式有變，保留舊版本。')
            consecutive_errors += 1
            if getattr(error, 'stop_source', False) or consecutive_errors >= 3:
                break
    return result
