"""Read only the public TargetContent advertisement linked by HKUST's careers frame."""
from urllib.parse import urljoin, urlsplit, parse_qs

from .adapters import Batch, soup_of, body_text, apply_deadline
from .http import CrawlError
from .model import clean_text, posted_from, record
from .rules import employment_of

HOST = 'hrmsxprod.psft.ust.hk'


def parse_hkust_detail(job, html):
    soup = soup_of(html)
    title = soup.find(id='HRS_JO_WRK_POSTING_TITLE$0')
    reference = soup.find(id='HRS_JO_WRK_HRS_JOB_OPENING_ID$0')
    department = soup.find(id='Z_HRS_APPL_DW_DESCR100$0')
    content = soup.find(id='HRS_JO_PDSC_VW_DESCRLONG$0')
    if not title or not reference or clean_text(reference.get_text()) != job['reference'] or not content:
        raise CrawlError('HKUST 舊招聘平台未提供對應職位的公開內文。')
    text = body_text(content)
    if len(text) < 100:
        raise CrawlError('HKUST 公開職位詳情內文不足。')
    job.update(title=clean_text(title.get_text(' ', strip=True)), description=text, match_text=text, detail_complete=True)
    if department:
        job['department'] = clean_text(department.get_text(' ', strip=True))
    job['employment_type'] = employment_of(job['title'], body=text)
    job['posted_date'] = posted_from(text) or job['posted_date']
    if not job.get('deadline'):
        apply_deadline(job, text)
    return job


def fetch_hkust_detail(job, client):
    # PublicClient retains anonymous cookies through the ordinary same-host redirects.
    html = client.get(job['url'])
    frame = soup_of(html).select_one('frame[name="TargetContent"][src]')
    if frame:
        url = urljoin(job['url'], frame['src'])
        bits = urlsplit(url)
        query = parse_qs(bits.query)
        if (bits.scheme != 'https' or bits.hostname != HOST or bits.port != 8044
                or not bits.path.startswith('/psc/hrmsxprod/')
                or query.get('Page') != ['HRS_CE_JOB_DTL']
                or query.get('JobOpeningId') != [job['reference']]):
            raise CrawlError('HKUST 公開職位框架連結不符，已停止。', stop_source=True)
        html = client.get(url)
    return parse_hkust_detail(job, html)


def collect_known_hkust(source, client, cached_jobs, listing_error):
    """On a list connection failure, re-read known ads on the separate public host.

    Only actually fetched ads enter the batch. A cached list never proves that
    there are no new vacancies, and this batch can never mark other jobs missing.
    """
    result = Batch(complete=False, errors=[listing_error])
    failures = 0
    for previous in cached_jobs:
        if previous.get('source_id') != source['id'] or previous.get('status') == 'missing' or urlsplit(previous['url']).hostname != HOST:
            continue
        fields = {key: previous.get(key) for key in ('department', 'posted_date', 'deadline', 'deadline_type', 'deadline_raw')}
        job = record(source, previous['title'], previous['url'], previous['reference'], detail_complete=False, **fields)
        try:
            fetch_hkust_detail(job, client)
            result.jobs.append(job)
            failures = 0
        except (CrawlError, KeyError, ValueError) as error:
            result.errors.append(str(error) if isinstance(error, CrawlError) else 'HKUST 舊廣告格式有變。')
            failures += 1
            if getattr(error, 'stop_source', False) or failures >= 3:
                break
    result.errors.insert(0, f'清單未能更新；本輪只重新讀取 {len(result.jobs)} 個已知官方廣告，可能遺漏新增職位。')
    return result
