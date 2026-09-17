"""Draft helpers to integrate into monitor; validated against saved live HTML fixtures."""
import ast
import re
from urllib.parse import urljoin, parse_qs, urlsplit, unquote, urlencode
from monitor.adapters import Batch, soup_of, body_text, apply_deadline
from monitor.http import CrawlError
from monitor.model import record, clean_text, posted_from, parse_date
from monitor.rules import employment_of


def collect_statutory(source, client):
    if source['adapter'] == 'hkmu':
        return collect_hkmu(source, client)
    if source['adapter'] == 'eduhk':
        return collect_eduhk(source, client)
    if source['adapter'] == 'hkapa':
        return collect_hkapa(source, client)
    parsers = {'vtc': (parse_vtc_list, parse_vtc_detail), 'thei': (parse_thei_list, parse_thei_detail)}
    listing, detail = parsers[source['adapter']]
    result = listing(source, client.get(source['url']))
    from .catalog import read_details
    return read_details(source, client, result, lambda job: detail(job, client.get(job['url'])))


def collect_eduhk(source, client):
    result, queue, visited, indexed = Batch(), [source['url']], set(), {}
    try:
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            if len(visited) >= 30:
                raise CrawlError('EdUHK 分頁超過安全上限。')
            visited.add(url)
            batch, pages = parse_eduhk_list(source, client.get(url), url)
            indexed.update({job['id']: job for job in batch.jobs})
            result.pages += 1
            queue.extend(p for p in pages if p not in visited and p not in queue)
    except CrawlError as error:
        result.complete = False
        result.errors.append(str(error))
        if error.stop_source:
            result.jobs = list(indexed.values())
            return result
    result.jobs = list(indexed.values())
    from .catalog import read_details
    return read_details(source, client, result)


def parse_vtc_list(source, html):
    soup=soup_of(html); indexed={}
    for row in soup.select('tr[id^="job_opening_"]'):
        cells=row.find_all('td',recursive=False);link=row.select_one('a[href*="jobDetail.php?id="]')
        if len(cells)!=4 or not link: raise CrawlError('VTC 職位表格欄位有變。')
        parts=[clean_text(x) for x in cells[0].get_text('\n',strip=True).splitlines() if clean_text(x)]
        if len(parts)<2 or not (parts[-1].startswith('(') and parts[-1].endswith(')')):raise CrawlError('VTC 缺少可確認職位編號。')
        title=' '.join(parts[:-1]);reference=parts[-1][1:-1].strip();url=urljoin(source['url'],link['href']);job_id=parse_qs(urlsplit(url).query).get('id',[''])[0]
        if row['id'] != 'job_opening_'+job_id:raise CrawlError('VTC 職位連結與行編號不一致。')
        department=' / '.join(clean_text(x.get_text(' ',strip=True)) for x in cells[1:3] if clean_text(x.get_text(' ',strip=True)) not in ('--',''))
        job=record(source,title,url,reference,department=department,detail_complete=False)
        job['employment_type']=employment_of(title)
        apply_deadline(job,'Closing date: '+cells[3].get_text(' ',strip=True))
        indexed[job_id]=job
    if not indexed:raise CrawlError('VTC 未找到可確認的職位表格，未當作零職位。')
    return Batch(jobs=list(indexed.values()),pages=1)


def parse_vtc_detail(job,html):
    soup=soup_of(html);content=soup.select_one('.cropBoxb.box727 .ContentArf');title=soup.select_one('.cropBoxb.box727 .titlebx h5')
    if not content or not title:raise CrawlError('VTC 詳情缺少正文。')
    for x in content.select('a[href*="application"],a[href*="VTC1"]'):x.decompose()
    text=body_text(content)
    ref=re.search(r'Ref\.?\s*no\s*:\s*([^\n]+)',text,re.I)
    if not ref or re.sub(r'\s+','',job['reference']).lower() not in re.sub(r'\s+','',ref.group(1)).lower():raise CrawlError('VTC 詳情編號不一致。')
    if len(text)<150:raise CrawlError('VTC 詳情正文不足。')
    job.update(title=clean_text(title.get_text(' ',strip=True)),description=text,match_text=text,detail_complete=True,posted_date=posted_from(text))
    job['employment_type']=employment_of(job['title'],body=text);apply_deadline(job,text);return job


def parse_hkapa(source,html):
    soup=soup_of(html);jobs=[]
    for panel in soup.select('.job-detail__inner'):
        title=panel.select_one('.job-detail__title');link=panel.select_one('a.job-detail__btnlink[href]');ref=panel.select_one('.job-detail__ref strong');body=panel.select_one('.job-content');kind=panel.select_one('.job-detail__type')
        if not all((title,link,ref,body)):raise CrawlError('HKAPA 職位區塊欄位有變。')
        text=body_text(body);complete=len(text)>150 and not re.search(r'Please refer to Chinese version|請參閱英文版本',text,re.I)
        job=record(source,title.get_text(' ',strip=True),urljoin(source['url'],link['href']),ref.get_text(' ',strip=True),description=text if complete else '',match_text=text if complete else '',detail_complete=complete)
        job['employment_type']=employment_of(job['title'],kind.get_text(' ',strip=True) if kind else '',text)
        job['posted_date']=posted_from(text);deadline=panel.select_one('.job-detail__dl');apply_deadline(job,(deadline.get_text(' ',strip=True)+'\n' if deadline else '')+text);jobs.append(job)
    count=re.search(r'Showing\s+\d+\s*[-–]\s*\d+\s+of\s+(\d+)\s+job openings',soup.get_text(' ',strip=True),re.I)
    if not jobs:raise CrawlError('HKAPA 未找到可確認職位，未當作零職位。')
    # Chinese page labels differ; caller compares counterpart refs/count.
    if count and int(count.group(1))!=len(jobs):raise CrawlError('HKAPA 列表總數與詳情區塊數目不符。')
    return Batch(jobs=jobs,pages=1,complete=all(j['detail_complete'] for j in jobs))


def collect_hkapa(source,client):
    result=parse_hkapa(source,client.get(source['url']))
    if any(not j['detail_complete'] for j in result.jobs):
        try:
            other=parse_hkapa(source,client.get('https://www.hkapa.edu/tch/job-opportunity'));result.pages+=1;by_ref={j['reference']:j for j in other.jobs}
            for job in result.jobs:
                alternative=by_ref.get(job['reference'])
                if not job['detail_complete'] and alternative and alternative['detail_complete']:
                    for key in ('description','match_text','detail_complete','posted_date','deadline','deadline_type','deadline_raw'):job[key]=alternative[key]
            result.complete=all(j['detail_complete'] for j in result.jobs)
            if not result.complete:result.errors.append('HKAPA 部分職位未提供完整中英文正文。')
        except CrawlError as e:result.complete=False;result.errors.append(str(e))
    return result


def parse_thei_list(source,html):
    soup=soup_of(html);indexed={}
    for row in soup.select('.e-loop-item'):
        link=row.select_one('.elementor-widget-theme-post-title a[href]');dept=row.select_one('.elementor-widget-theme-post-excerpt');deadline=row.select_one('.elementor-widget-post-info')
        if not link:raise CrawlError('THEi 職位列表欄位有變。')
        url=urljoin(source['url'],link['href']);job=record(source,link.get_text(' ',strip=True),url,department=dept.get_text(' ',strip=True) if dept else '',detail_complete=False)
        job['employment_type']=employment_of(job['title']);apply_deadline(job,deadline.get_text(' ',strip=True) if deadline else '');indexed[url]=job
    if not indexed:raise CrawlError('THEi 未找到可確認職位，未當作零職位。')
    if soup.select('a.page-numbers,.elementor-pagination a[href]'):raise CrawlError('THEi 出現未處理分頁。')
    return Batch(jobs=list(indexed.values()),pages=1)


def parse_thei_detail(job,html):
    soup=soup_of(html);body=soup.select_one('.elementor-widget-theme-post-content');title=soup.select_one('.elementor-widget-theme-post-title')
    if not body or not title:raise CrawlError('THEi 詳情正文格式有變。')
    text=body_text(body);ref=re.search(r'Ref\.?\s*No\.?\s*:\s*([^\n]+)',text,re.I)
    if not ref or len(text)<200:raise CrawlError('THEi 詳情缺少編號或足夠正文。')
    # Keep canonical URL identity from listing: reference enrichment should not regenerate ID.
    job.update(reference=clean_text(ref.group(1)),title=title.get_text(' ',strip=True),description=text,match_text=text,detail_complete=True)
    published=soup.select_one('meta[property="article:published_time"]')
    job['posted_date']=parse_date(published.get('content','')) if published else posted_from(text)
    job['employment_type']=employment_of(job['title'],body=text);apply_deadline(job,text);return job


def parse_hkmu_detail(job,html):
    keys_match=re.search(r'descRequisition\s*:\s*\{.*?_hlid\s*:\s*(\[[^\r\n]*?\])',html,re.S)
    values_match=re.search(r"fillList\('requisitionDescriptionInterface',\s*'descRequisition',\s*(\[[^\r\n]*\])\);",html)
    if not keys_match or not values_match:raise CrawlError('HKMU 詳情的公開欄位格式有變。')
    try:
        keys,values=ast.literal_eval(keys_match.group(1)),ast.literal_eval(values_match.group(1))
        if len(keys)!=len(values) or not all(isinstance(v,str) for v in keys+values):raise ValueError('shape')
        row=dict(zip(keys,(unquote(v[3:] if v.startswith('!*!') else v) for v in values)))
    except (ValueError,SyntaxError):raise CrawlError('HKMU 公開資料格式有變，未執行網頁程式。') from None
    if row.get('reqlistitem.contestnumber')!=job['reference']:raise CrawlError('HKMU 詳情與職位編號不一致。')
    # HKMU description is university background; essential subject/duties are qualification field.
    text='\n'.join(body_text(soup_of(row.get(key,''))) for key in ('reqlistitem.description','reqlistitem.qualification'))
    meaningful=body_text(soup_of(row.get('reqlistitem.qualification','')))
    if len(meaningful)<100:raise CrawlError('HKMU 詳情缺少職位職責正文。')
    job.update(title=row.get('reqlistitem.title') or job['title'],department=row.get('reqlistitem.organization') or job['department'],description=text,match_text=meaningful,detail_complete=True,posted_date=posted_from(text))
    field=row.get('reqlistitem.jobfield','')
    # This portal category groups temporary and PT together; temporary alone is not proof of PT.
    metadata='' if re.search(r'temporary\s*/\s*part.?time',field,re.I) else field
    job['employment_type']=employment_of(job['title'],metadata,meaningful)
    unposting=row.get('reqlistitem.unpostingdate','');date_match=re.match(r'(\d{1,2})/([A-Za-z]{3})/(\d{4})',unposting)
    if date_match:apply_deadline(job,'Closing date: '+' '.join(date_match.groups()))
    else:apply_deadline(job,text)
    return job


def collect_hkmu(source,client):
    result=Batch();indexed={}
    for listing in source.get('list_urls',[source['url']]):
        try:
            landing=client.get(listing)
            portal=re.search(r"portalNo:\s*'([0-9]+)'",landing)
            if not portal or not re.search(r'userSignedIn:\s*false',landing):raise CrawlError('HKMU 公開訪客搜尋入口未能確認。')
            endpoint=urljoin(listing,'/careersection/rest/jobboard/searchjobs?lang=en&portal='+portal.group(1));page=1;expected=None;seen=set()
            while True:
                if page>30:raise CrawlError('HKMU 分頁超過安全上限。',stop_source=True)
                payload={'multilineEnabled':False,'sortingSelection':{'sortBySelectionParam':'3','ascendingSortingOrder':'false'},'fieldData':{'fields':{'KEYWORD':'','JOB_TITLE':''},'valid':True},'filterSelectionParam':{'searchFilterSelections':[]},'pageNo':page}
                data=client.search_json(endpoint,payload,{'tz':'GMT+08:00','tzname':'Asia/Hong_Kong'});paging=data['pagingData'];total=int(paging['totalCount']);size=int(paging['pageSize'])
                if data.get('careerSectionUnAvailable') or size<1 or int(paging['currentPageNo'])!=page or(expected is not None and total!=expected):raise CrawlError('HKMU 分頁資料或入口狀態有變。')
                expected=total
                for row in data['requisitionList']:
                    fields,ref=row['column'],row['contestNo']
                    if len(fields)!=3 or int(row['linkedColumn'])!=0 or not re.fullmatch(r'[A-Za-z0-9]+',ref):raise CrawlError('HKMU 搜尋欄位次序有變。')
                    url=urljoin(listing,'jobdetail.ftl?'+urlencode({'job':ref,'lang':'en'}));job=record(source,fields[0],url,ref,department=fields[1],detail_complete=False)
                    job['employment_type']=employment_of(job['title']);m=re.match(r'(\d{1,2})/([A-Za-z]{3})/(\d{4})',fields[2])
                    if m:apply_deadline(job,'Closing date: '+' '.join(m.groups()))
                    indexed[job['id']]=job;seen.add(ref)
                result.pages+=1
                if page*size>=total:break
                page+=1
            if len(seen)!=expected:raise CrawlError(f'HKMU 已讀完分頁但取得 {len(seen)} 個職位，入口顯示 {expected} 個；暫不判定職位消失。')
        except (CrawlError,KeyError,TypeError,ValueError) as e:
            result.complete=False;result.errors.append(str(e) if isinstance(e,CrawlError) else 'HKMU 公開搜尋資料格式有變。')
            if getattr(e,'stop_source',False):result.jobs=list(indexed.values());return result
    result.jobs=list(indexed.values());result.explicit_empty=result.complete and not result.jobs
    failures=0
    for job in result.jobs:
        try:parse_hkmu_detail(job,client.get(job['url']));failures=0
        except CrawlError as e:
            result.complete=False;result.errors.append(str(e));failures+=1
            if e.stop_source or failures>=3:break
    return result

def parse_eduhk_list(source,html,page_url=None):
    # Structure-independent but strict association: official careers PDF and a
    # smallest enclosing block containing its explicit ref/ad/closing labels.
    soup=soup_of(html);jobs=[];page_url=page_url or source['url']
    for link in soup.select('a[href]'):
        url=urljoin(page_url,link['href'])
        if not re.search(r'/cms/f/career/[^?#]+\.pdf(?:[?#]|$)',url,re.I):continue
        block=None
        for ancestor in list(link.parents)[:7]:
            txt=ancestor.get_text(' ',strip=True)
            if re.search(r'Ad\s+Date\s*:',txt,re.I) and re.search(r'Close\s+Date\s*:',txt,re.I):
                matching=[a for a in ancestor.select('a[href]') if re.search(r'/cms/f/career/[^?#]+\.pdf(?:[?#]|$)',urljoin(page_url,a['href']),re.I)]
                if len(matching)==1:block=ancestor
                break
        if block is None:raise CrawlError('EdUHK 職位 PDF 缺少可確認的日期及編號區塊。')
        txt=block.get_text('\n',strip=True);ref=re.search(r'\bRef\s*:\s*(\d+)\b',txt,re.I);posted=re.search(r'Ad\s+Date\s*:\s*([^\n]+)',txt,re.I);close=re.search(r'Close\s+Date\s*:\s*([^\n]+)',txt,re.I)
        if not ref or not posted or not close:raise CrawlError('EdUHK 職位欄位有變。')
        title=clean_text(link.get_text(' ',strip=True));before=txt[:ref.start()].strip();department=clean_text(before[len(title):]) if before.startswith(title) else ''
        job=record(source,title,url,ref.group(1),department=department,posted_date=parse_date(posted.group(1)),detail_complete=False)
        if not job['posted_date']:raise CrawlError('EdUHK 刊登日期格式未能確認。')
        job['employment_type']=employment_of(title);apply_deadline(job,'Closing date: '+close.group(1));jobs.append(job)
    if not jobs:raise CrawlError('EdUHK 未找到可確認職位，未當作零職位。')
    pages=sorted({urljoin(page_url,a['href']) for a in soup.select('a[href]') if re.search(r'/en/current-openings/page\d+(?:[?#]|$)',urljoin(page_url,a['href']))})
    return Batch(jobs=jobs,pages=1),pages
