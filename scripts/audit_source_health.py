"""Bounded read-only audit: no store, notifications or status changes."""
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin
import requests
from monitor.run import read_config
from monitor.http import PublicClient, CrawlError
from monitor.adapters import soup_of

def out(kind, **fields):
    print(json.dumps(dict(kind=kind, **fields), ensure_ascii=False), flush=True)

def inspect(source, url, label):
    client = PublicClient(source)
    try:
        html = client.get(url)
        soup = soup_of(html)
        out("page", source=source["id"], label=label, url=url,
            title=soup.title.get_text(" ", strip=True) if soup.title else "",
            headings=[n.get_text(" ", strip=True) for n in soup.select("h1,h2,h3")][:20],
            counts={s:len(soup.select(s)) for s in ["tr[close-date]", "a[href*=about_us]", '[itemprop="description"]', ".jobdescription", ".jobdescriptioncontainer", ".jobDisplay"]},
            text=soup.get_text(" ", strip=True)[-7000:])
        if source["id"] == "cice":
            tables=[t for t in soup.select("table") if t.select('a[href*="doc/job/"]')]
            out("cice_tables", label=label, tables=[str(t)[:20000] for t in tables[:2]],
                links=[{"text":a.get_text(" ",strip=True),"href":a.get("href")} for a in soup.select("a[href]") if "about_us" in a["href"] or "doc/job/" in a["href"]][:30])
    except Exception as error:
        out("failure", source=source["id"], label=label, reason=str(error)[:400])

def main():
    sources={s["id"]:s for s in read_config(Path("."))[0]}
    for url in [sources["cice"]["url"], "https://www.cice.edu.hk/en/about_us.aspx"]:
        inspect(sources["cice"],url,"recruitment")
    data=json.loads(Path("dist/data/jobs.json").read_text())
    twc=[j for j in data["jobs"] if j["source_id"]=="twc" and (not j.get("detail_complete") or j["reference"]=="1315")]
    for job in twc[:4]:
        inspect(sources["twc"],job["url"],job["reference"])
    # Expose only whitelisted TLS failure classifications, never exception URLs/headers.
    source=sources["eduhk"]
    try:
        response=requests.get("https://www.eduhk.hk/robots.txt",headers={"User-Agent":PublicClient.agent},timeout=(15,30),allow_redirects=False)
        out("eduhk_robots",http=response.status_code,content_type=response.headers.get("content-type"))
    except requests.RequestException as error:
        message=str(error).upper()
        reasons=[x for x in ["CERTIFICATE_VERIFY_FAILED","UNSAFE_LEGACY_RENEGOTIATION_DISABLED","WRONG_VERSION_NUMBER","TLSV1_ALERT_PROTOCOL_VERSION","SSLV3_ALERT_HANDSHAKE_FAILURE","TLSV1_ALERT_INTERNAL_ERROR","UNEXPECTED_EOF_WHILE_READING","CERTIFICATE HAS EXPIRED","UNABLE TO GET LOCAL ISSUER CERTIFICATE"] if x in message]
        out("eduhk_tls",category=type(error).__name__,reasons=reasons)
    # Same anonymous CUHK public search request as the production adapter.
    source=sources["cuhk"]; client=PublicClient(source); refs=[]; sizes=[]; expected=None
    try:
        page=1
        while page<=10:
            payload={"multilineEnabled":True,"sortingSelection":{"sortBySelectionParam":"3","ascendingSortingOrder":"false"},"fieldData":{"fields":{"KEYWORD":"","JOB_TITLE":""},"valid":True},"filterSelectionParam":{"searchFilterSelections":[]},"pageNo":page}
            data=client.search_json(urljoin(source["url"],"/careersection/rest/jobboard/searchjobs?lang=en&portal=10115020119"),payload,{"tz":"GMT+08:00","tzname":"Asia/Hong_Kong"})
            paging=data["pagingData"]; rows=data["requisitionList"]; expected=int(paging["totalCount"])
            refs += [str(r["contestNo"]) for r in rows]; sizes.append(len(rows))
            out("cuhk_page",page=page,paging=paging,refs=[r["contestNo"] for r in rows])
            if page*int(paging["pageSize"])>=expected: break
            page+=1
        out("cuhk_counts",expected=expected,raw=len(refs),unique=len(set(refs)),sizes=sizes,repeated={k:v for k,v in Counter(refs).items() if v>1})
    except Exception as error:
        out("failure",source="cuhk",reason=str(error)[:300])
    # Recheck HKU list/details once through normal robots and challenge handling.
    inspect(sources["hku"],sources["hku"]["url"],"listing")
    # Official parent organization publishes school posts inline; verify access/DOM.
    parent=dict(sources["hkas"],allowed_hosts=["hkac.org.hk"],url="https://hkac.org.hk/en/about_join/")
    inspect(parent,parent["url"],"official-parent")
if __name__=="__main__": main()
