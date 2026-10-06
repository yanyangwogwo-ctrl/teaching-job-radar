"""Bounded read-only follow-up for observed source failures."""
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin
from monitor.run import read_config
from monitor.http import PublicClient
from monitor.adapters import soup_of
def out(kind, **fields):
    print(json.dumps(dict(kind=kind, **fields),ensure_ascii=False),flush=True)
def main():
    sources={s["id"]:s for s in read_config(Path("."))[0]}
    source=sources["twc"]
    html=PublicClient(source).get("https://careers.twc.edu.hk/job/Executive-Officer-1307/1367155566/")
    node=soup_of(html).select_one(".jobDisplay")
    out("twc_closed_dom",html=str(node)[:12000])
    source=dict(sources["hkas"],url="https://hkac.org.hk/en/about_join/",allowed_hosts=["hkac.org.hk"])
    soup=soup_of(PublicClient(source).get(source["url"]))
    import re
    stamps=soup.find_all(string=re.compile("Post Date:"))
    out("arts_structure",examples=[str(n.parent.parent)[:19000] for n in stamps[:2]],count=len(stamps))
    source=sources["cuhk"]; client=PublicClient(source); refs=[]; expected=None
    for page in range(1,11):
        payload={"multilineEnabled":True,"sortingSelection":{"sortBySelectionParam":"3","ascendingSortingOrder":"true"},"fieldData":{"fields":{"KEYWORD":"","JOB_TITLE":""},"valid":True},"filterSelectionParam":{"searchFilterSelections":[]},"pageNo":page}
        data=client.search_json(urljoin(source["url"],"/careersection/rest/jobboard/searchjobs?lang=en&portal=10115020119"),payload,{"tz":"GMT+08:00","tzname":"Asia/Hong_Kong"})
        paging=data["pagingData"]; rows=data["requisitionList"]; expected=int(paging["totalCount"])
        refs.extend(str(r["contestNo"]) for r in rows)
        out("cuhk_ascending_page",page=page,paging=paging,refs=[r["contestNo"] for r in rows])
        if page*int(paging["pageSize"])>=expected: break
    out("cuhk_ascending_counts",expected=expected,raw=len(refs),unique=len(set(refs)),repeated={k:v for k,v in Counter(refs).items() if v>1})
if __name__=="__main__": main()
