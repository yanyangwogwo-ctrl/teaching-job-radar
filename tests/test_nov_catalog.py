"""HPSHCC's verified empty notice must not conceal a changed job layout."""
from pathlib import Path
import unittest

from monitor.catalog import parse_catalog_page
from monitor.http import CrawlError
from monitor.run import read_config


SOURCES, _ = read_config(Path(__file__).resolve().parents[1])
SOURCE = next(source for source in SOURCES if source['id'] == 'hpshcc')
NOTICE = 'There is no job vacancy at the moment.'


def page(content):
    return '<section class="inner-content"><div class="container"><h2 class="page-title">JOB VACANCY</h2>' + content + '</div></section>'


class HpshccEmptyNoticeTests(unittest.TestCase):
    def test_verified_notice_is_explicit_empty(self):
        html = page('<a href="/f/upload/Application.docx">Application form</a><p>&nbsp;</p><p>' + NOTICE + '</p>')
        jobs, pages, empty = parse_catalog_page(SOURCE, html)
        self.assertEqual(jobs, [])
        self.assertEqual(pages, [])
        self.assertTrue(empty)

    def test_missing_changed_or_unscoped_notice_stays_failure(self):
        for html in ['', page('<p>Service unavailable.</p>'), page('<p>No matching results.</p>'),
                     page('<p>' + NOTICE + ' Please sign in.</p>'), '<footer><p>' + NOTICE + '</p></footer>']:
            with self.subTest(html=html), self.assertRaises(CrawlError):
                parse_catalog_page(SOURCE, html)

    def test_surviving_job_links_reject_empty_after_layout_change(self):
        for href in ['/jobs/detail/lecturer', 'https://hkuspace-plk.hku.hk/jobs/detail/lecturer']:
            html = page('<p>' + NOTICE + '</p><div class="new-layout"><a href="' + href + '">Lecturer</a></div>')
            with self.subTest(href=href), self.assertRaises(CrawlError):
                parse_catalog_page(SOURCE, html)

    def test_live_rows_take_priority_over_retained_empty_notice(self):
        html = page('<p>' + NOTICE + '</p><div class="fluid-block"><h3 class="heading"><a href="/jobs/detail/lecturer">Lecturer</a></h3><p class="date">30 Nov 2026</p></div>')
        jobs, _, empty = parse_catalog_page(SOURCE, html)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['title'], 'Lecturer')
        self.assertFalse(empty)


if __name__ == '__main__':
    unittest.main()
