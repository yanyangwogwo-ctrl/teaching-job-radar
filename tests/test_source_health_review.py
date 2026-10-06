"""Regression cases from the 2026-10-06 official-source audit."""
import copy
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
from monitor.adapters import Batch, collect
from monitor.catalog import parse_catalog_page, parse_catalog_detail, read_details
from monitor.http import PublicClient, CrawlError, tls_failure_reason
from monitor.model import record
from monitor.official import collect_cuhk
from monitor.run import read_config
from monitor.store import fresh_store, reconcile

SOURCES, PREFS = read_config(Path(__file__).resolve().parents[1])
SOURCES = {s['id']: s for s in SOURCES}
DAY = '2026-10-06T06:00:00+00:00'
TWC = SOURCES['twc']

def vacancy(reference='1307', **fields):
    return record(TWC, 'Executive Officer', 'https://careers.twc.edu.hk/job/Executive-Officer-1307/1367155666/',
                  reference, **fields)

def filled_page(title='Executive Officer'):
    return '<title>' + title + ' Job Details | TWC</title><div class="jobDisplay"><div class="content"><div class="job"><p><strong>Sorry, this position has been filled.</strong></p></div></div></div>'

class SourceHealthReviewTests(unittest.TestCase):
    def test_cice_new_date_attribute_and_open_ended_row(self):
        html = '''<table class="post_display_table"><tr><th>Department/Unit</th><th>Post</th><th>Closing Date</th><th>Reference No</th></tr>
        <tr class="empty" style="display:none"><td colspan="4">/</td></tr>
        <tr><td class="department-cell" data-department="DSS"></td><td><a href="/doc/job/AMA_DSS-01_2026.pdf">Assistant Manager</a></td>
        <td class="closing-date" data-close-date="2026-10-13"></td><td>AMA/DSS/01/2026</td></tr>
        <tr><td></td><td><a href="/doc/job/AA_C_05_2026.pdf">行政助理</a></td>
        <td class="closing-date" data-close-date="2026-09-23"></td><td>AA/C/05/2026</td></tr>
        <tr><td></td><td><a href="/doc/job/Workman_C_01_2026.pdf">二級服務員</a></td>
        <td class="closing-date">Until job vacancy filled</td><td>Workman/C/01/2026</td></tr></table>'''
        jobs, pages, empty = parse_catalog_page(SOURCES['cice'], html)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0]['deadline'], '2026-10-13')
        self.assertEqual(jobs[1]['deadline'], '2026-09-23')
        self.assertEqual(jobs[2]['deadline_type'], 'until-filled')
        self.assertEqual(jobs[0]['department'], '')
        self.assertEqual(jobs[0]['reference'], 'AMA/DSS/01/2026')
        self.assertEqual(jobs[0]['url'], 'https://www.cice.edu.hk/doc/job/AMA_DSS-01_2026.pdf')
        with self.assertRaises(CrawlError):
            parse_catalog_page(SOURCES['cice'], html.replace('2026-10-13', 'invalid'))

    def test_explicit_filled_notice_is_not_a_parser_failure(self):
        job = vacancy(detail_complete=False)
        parse_catalog_detail(TWC, job, filled_page())
        self.assertTrue(job['source_closed'])
        self.assertFalse(job['detail_complete'])
        self.assertIsNone(job['deadline'])
        for html in [filled_page('Different post'), filled_page().replace('position has been filled', 'website is unavailable'),
                     filled_page().replace('jobDisplay', 'unrelated')]:
            with self.assertRaises(CrawlError):
                parse_catalog_detail(TWC, vacancy(detail_complete=False), html)
        with self.assertRaises(CrawlError):
            parse_catalog_detail(dict(TWC,id='another'), vacancy(detail_complete=False), filled_page())

    def test_mixed_live_and_filled_details_complete_the_check(self):
        client = Mock()
        client.get.side_effect = [filled_page(), '<article itemprop="description">' + 'Teaching ethics to undergraduates. ' * 6 + '</article>']
        result = read_details(TWC, client, Batch(jobs=[vacancy(detail_complete=False), vacancy('1308',detail_complete=False)]))
        self.assertTrue(result.complete)
        self.assertTrue(result.jobs[0]['source_closed'])
        self.assertTrue(result.jobs[1]['detail_complete'])

    def test_closure_survives_partial_reads_and_missing_but_can_reopen(self):
        state = fresh_store()
        original = vacancy(description='Verified original advertisement. ' * 6,posted_date='2026-09-20')
        reconcile(state,TWC,Batch(jobs=[original]),DAY,PREFS)
        closed = vacancy(source_closed=True,detail_complete=False)
        reconcile(state,TWC,Batch(jobs=[closed]),DAY,PREFS)
        job = state['jobs'][original['id']]
        self.assertEqual(job['status'],'closed')
        self.assertEqual(job['description'],original['description'])
        self.assertEqual(job['posted_date'],'2026-09-20')
        self.assertIsNone(job['deadline'])
        self.assertFalse(any(e['kind']=='new' for e in state['outbox'].values()))
        reconcile(state,TWC,Batch(jobs=[vacancy(detail_complete=False)],complete=False),DAY,PREFS)
        self.assertEqual(state['jobs'][original['id']]['status'],'closed')
        for _ in range(3):
            reconcile(state,TWC,Batch(explicit_empty=True),DAY,PREFS)
        self.assertEqual(state['jobs'][original['id']]['status'],'closed')
        reopened = copy.deepcopy(closed)
        parse_catalog_detail(TWC,reopened,'<article itemprop="description">'+'A verified newly reopened vacancy. '*6+'</article>')
        reconcile(state,TWC,Batch(jobs=[reopened]),DAY,PREFS)
        self.assertEqual(state['jobs'][original['id']]['status'],'open')
        self.assertFalse(state['jobs'][original['id']].get('source_closed'))

    def test_tls_reports_fixed_reason_without_exception_secrets_or_retry(self):
        error = requests.exceptions.SSLError('UNSAFE_LEGACY_RENEGOTIATION_DISABLED https://example.edu/?token=do-not-print')
        self.assertEqual(tls_failure_reason(error),'網站使用不相容的舊式 TLS 重新協商')
        client = PublicClient(SOURCES['eduhk'])
        client.session.request = Mock(side_effect=error)
        with self.assertRaises(CrawlError) as caught:
            client._request('https://www.eduhk.hk/robots.txt',robots=True)
        self.assertTrue(caught.exception.stop_source)
        self.assertIn('robots.txt',str(caught.exception))
        self.assertNotIn('do-not-print',str(caught.exception))
        self.assertEqual(client.session.request.call_count,1)
        self.assertNotIn('verify',client.session.request.call_args.kwargs)

    def test_pending_adapter_reports_both_implementation_and_network_failure(self):
        source = SOURCES['nangyan']
        client=Mock(); client.get.side_effect=CrawlError('ConnectTimeout')
        with self.assertRaises(CrawlError) as caught:
            collect(source,client)
        self.assertIn('職位讀取尚未接入',str(caught.exception))
        self.assertIn('ConnectTimeout',str(caught.exception))

    def test_cuhk_mismatch_keeps_complete_bodies_and_exposes_actual_page_sizes(self):
        source=SOURCES['cuhk']; client=Mock()
        def page(number,refs):
            return {'pagingData':{'currentPageNo':number,'pageSize':2,'totalCount':3},
                    'requisitionList':[{'contestNo':ref,'linkedColumn':0,'column':['Lecturer',ref,'Humanities','']} for ref in refs]}
        client.search_json.side_effect=[page(1,['A','B']),page(2,[])]
        with patch('monitor.official.parse_cuhk_detail',side_effect=lambda job,html:job.update(detail_complete=True,description='Verified detail')):
            result=collect_cuhk(source,client)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.jobs),2)
        self.assertTrue(all(j['detail_complete'] for j in result.jobs))
        self.assertIn('每頁 2、0 列',result.errors[0])
        self.assertIn('合共 2 列、2 個不重複職位',result.errors[0])

if __name__=='__main__': unittest.main()
