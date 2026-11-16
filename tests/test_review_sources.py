"""Regressions for source completeness and the November recruitment layouts."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from monitor.catalog import parse_catalog_page
from monitor.adapters import collect
from monitor.http import CrawlError
from monitor.intake import parse_intake
from monitor.official import collect_cuhk, collect_official, parse_cityu_list, parse_hkust_list
from monitor.model import record
from monitor.run import read_config

SOURCES = {s['id']: s for s in read_config(Path(__file__).resolve().parents[1])[0]}
HKIT_EMPTY = '''<header><a href="/programmes">Programmes</a></header>
<section class="container-xl py-5"><p class="text-secondary">Join the HKIT team! The Institute recruits academic and administrative staff from time to time.</p>
<div class="alert alert-info rounded-4 mt-3"><i></i>No open vacancies at the moment. You are welcome to send your CV to
<a href="mailto:recruit@hkit.edu.hk">recruit@hkit.edu.hk</a> for future consideration (if applicable).</div>
<p>* The information provided by the applicant will be kept strictly confidential and will only be used for recruitment purposes</p>{extra}</section>'''


class ReviewSourceTests(unittest.TestCase):
    def test_manual_source_never_sends_network_requests(self):
        client = Mock()
        batch = collect(SOURCES['hkic'], client)
        self.assertFalse(batch.complete)
        self.assertFalse(batch.listing_complete)
        self.assertEqual(batch.capability, 'manual')
        self.assertEqual(batch.severity, 'warning')
        self.assertEqual(client.mock_calls, [])
        self.assertIn('本輪沒有發送', batch.errors[0])

    def test_hkit_new_official_notice_is_verified_empty(self):
        batch = parse_intake(SOURCES['hkit'], HKIT_EMPTY.format(extra=''))
        self.assertTrue(batch.explicit_empty)
        self.assertTrue(batch.listing_complete)

    def test_hkit_retained_notice_cannot_hide_new_job_or_unknown_content(self):
        for extra in ('<a href="/lecturer">Part-time Lecturer</a>', '<iframe src="/jobs"></iframe>',
                      '<table><tr><td>Lecturer</td></tr></table>', '<div class="vacancy">Lecturer</div>',
                      '<p>New opening: Lecturer in Philosophy</p>'):
            with self.subTest(extra=extra), self.assertRaises(CrawlError):
                parse_intake(SOURCES['hkit'], HKIT_EMPTY.format(extra=extra))
        with self.assertRaises(CrawlError):
            parse_intake(SOURCES['hkit'], HKIT_EMPTY.format(extra='').replace('No open vacancies', 'Some open vacancies'))

    def test_hkas_captures_off_path_ad_but_not_navigation(self):
        html = '''<nav><a href="/job-vacancy/">Jobs</a></nav>
        <div class="post_content entry-content"><div class="wpb_text_column"><p>Hong Kong Art School</p></div>
        <div class="wpb_text_column"><p><a href="/job-vacancy/registrar/"><strong>Associate Registrar / Assistant Registrar</strong></a></p></div>
        <div class="wpb_text_column"><p><strong><a href="/drama-adjudicators-of-hong-kong-school-drama-festival/">Adjudicator for Hong Kong School Drama Festival 2026/27</a></strong></p></div>
        <div class="wpb_text_column"><p><strong><a href="/job-vacancy/school-facilities-assistant-part-time-2/?lang=zh-hant">School Facilities Assistant (Part-time)</a></strong></p></div></div>'''
        jobs, pages, empty = parse_catalog_page(SOURCES['hkas'], html)
        self.assertEqual(len(jobs), 3)
        self.assertIn('/drama-adjudicators-', jobs[1]['url'])
        self.assertFalse(empty)

    def cuhk_page(self, total=1):
        return {'pagingData': {'currentPageNo': 1, 'pageSize': 25, 'totalCount': total},
                'requisitionList': [{'contestNo': 'A', 'linkedColumn': 0, 'column': ['Lecturer', 'A', 'Humanities', '']}]}

    def test_cuhk_detail_failure_preserves_verified_listing_coverage(self):
        client = Mock()
        client.search_json.return_value = self.cuhk_page()
        client.get.side_effect = CrawlError('HTTP 504')
        batch = collect_cuhk(SOURCES['cuhk'], client)
        self.assertFalse(batch.complete)
        self.assertTrue(batch.listing_complete)

    def test_cuhk_three_missing_still_not_complete(self):
        client = Mock()
        client.search_json.return_value = self.cuhk_page(total=4)
        with patch('monitor.official.parse_cuhk_detail', side_effect=lambda job, _: job.update(detail_complete=True)):
            batch = collect_cuhk(SOURCES['cuhk'], client)
        self.assertFalse(batch.complete)
        self.assertFalse(batch.listing_complete)
        self.assertEqual(len(batch.jobs), 1)

    def test_cityu_known_detail_limit_is_not_a_listing_failure(self):
        client = Mock()
        client.get.return_value = '''<table id="MainContent_gvJobAcad"><tr><td><a id="x_hlJob_1" href="https://www.cityu.edu.hk/hro/en/job/current/academic.asp?ref=T1">Part-time Lecturer</a></td><td>Humanities</td><td>Until filled</td></tr></table>'''
        batch = collect_official(SOURCES['cityu'], client)
        self.assertTrue(batch.listing_complete)
        self.assertFalse(batch.complete)
        self.assertEqual(batch.capability, 'list_only')
        self.assertEqual(batch.severity, 'warning')
        self.assertEqual(client.get.call_count, 1)

    def test_new_pagination_cannot_silently_claim_complete(self):
        cases = [(parse_cityu_list, SOURCES['cityu'], '<table id="MainContent_gvJobAcad"><a href="javascript:Page$2">Next</a></table>'),
                 (parse_hkust_list, SOURCES['hkust'], '<div class="view-id-job_listing view-display-id-block_2"><nav class="pager"><a href="?page=1">Next</a></nav></div>')]
        for parser, source, html in cases:
            with self.subTest(source=source['id']), self.assertRaises(CrawlError):
                parser(source, html)

    def test_hkust_known_omission_does_not_hide_real_detail_failure(self):
        source = SOURCES['hkust']
        jobs = [record(source, 'Lecturer', 'https://apply.interfolio.com/197296', '1', detail_complete=False),
                record(source, 'Instructor', 'https://hrmsxprod.psft.ust.hk:8044/job/2', '2', detail_complete=False)]
        with patch('monitor.official.parse_hkust_list', return_value=jobs), patch('monitor.peoplesoft.fetch_hkust_detail', side_effect=CrawlError('HTTP 504')):
            batch = collect_official(source, Mock())
        self.assertTrue(batch.listing_complete)
        self.assertFalse(batch.complete)
        self.assertEqual(batch.severity, 'error')


if __name__ == '__main__':
    unittest.main()
