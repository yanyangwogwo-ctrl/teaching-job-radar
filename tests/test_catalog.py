import unittest
from unittest.mock import Mock

from monitor.catalog import collect_catalog, parse_catalog_page, parse_catalog_detail, source_date
from monitor.intake import parse_intake
from monitor.statutory import parse_hkmu_detail
from monitor.yccece import parse_yccece_list, parse_yccece_detail
from monitor.model import record
import json
from urllib.parse import quote
from monitor.http import CrawlError


SOURCE = {'id': 'college', 'group': 'college', 'institution': 'College',
          'url': 'https://example.edu/careers', 'adapter': 'catalog_html',
          'selectors': {'row': 'li.job', 'title': 'a', 'link': 'a',
                        'posted_date': 'time', 'next': 'a.next',
                        'detail_body': 'article', 'empty': '.no-vacancies'}}


def listing(number, more=''):
    return f'<li class="job"><a href="/ad/{number}">Part-time Lecturer in Ethics</a><time datetime="2026-09-01">1 Sept 2026</time></li>{more}'


class CatalogTests(unittest.TestCase):
    def test_complete_list_retained_when_first_detail_is_denied(self):
        client = Mock()
        client.get.side_effect = [listing(1, '<a class="next" href="?page=2">2</a>'),
                                  listing(2), CrawlError('denied', stop_source=True)]
        result = collect_catalog(SOURCE, client)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(result.pages, 2)
        self.assertTrue(all(not j['detail_complete'] for j in result.jobs))
        self.assertEqual(client.get.call_count, 3)

    def test_pagination_deduplication_and_real_detail(self):
        client = Mock()
        client.get.side_effect = [listing(1, '<a class="next" href="?page=2">2</a>'),
            listing(1) + listing(2, '<a class="next" href="/careers">1</a>'),
            '<article>Teach ethics and AI literacy. ' + 'Teaching experience is required. ' * 5 + '</article>',
            '<article>Teach philosophy. ' + 'Relevant degree and teaching experience are required. ' * 4 + '</article>']
        result = collect_catalog(SOURCE, client)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.jobs), 2)
        self.assertTrue(all(j['detail_complete'] and j['posted_date'] == '2026-09-01' for j in result.jobs))
        self.assertEqual(result.jobs[0]['employment_type'], 'part-time')

    def test_missing_list_never_zero_and_date_locale_is_explicit(self):
        with self.assertRaises(CrawlError):
            parse_catalog_page(SOURCE, '<html>New website</html>')
        jobs, pages, empty = parse_catalog_page(SOURCE, '<p class="no-vacancies">No vacancies</p>')
        self.assertTrue(empty)
        self.assertEqual(jobs, [])
        self.assertIsNone(source_date('02/09/2026', SOURCE))
        self.assertEqual(source_date('02/09/2026', dict(SOURCE, date_order='DMY')), '2026-09-02')
        self.assertIsNone(source_date('32/09/2026', dict(SOURCE, date_order='DMY')))

    def test_real_world_date_and_secure_hkct_link(self):
        self.assertEqual(source_date('2026-\u200e9-23', SOURCE), '2026-09-23')
        source = dict(SOURCE, id='hkct', url='https://www.hkct.edu.hk/en/abouthkct/join-us')
        jobs, _, _ = parse_catalog_page(source, '<li class="job"><a href="/abouthkct/join-us/lecturer">Lecturer</a></li>')
        self.assertEqual(jobs[0]['url'], 'https://www.hkct.edu.hk/tc/abouthkct/join-us/lecturer')
        source = dict(SOURCE, selectors=dict(SOURCE['selectors'], detail_posted='meta[itemprop="datePosted"]'), detail_posted_format='successfactors_utc')
        job = record(source, 'Lecturer', 'https://example.edu/ad')
        parse_catalog_detail(source, job, '<meta itemprop="datePosted" content="Mon Sep 14 16:00:00 UTC 2026"><article>' + 'Teach ethics. ' * 15 + '</article>')
        self.assertEqual(job['posted_date'], '2026-09-15')

    def test_changed_empty_notices_trigger_warning(self):
        html = '<div class="pagecontent">' + ''.join(f'<h4 id="{category}">Jobs</h4><div class="alert">There are currently no vacancies.</div>' for category in ['Academic', 'Management', 'Admin', 'General']) + '</div>'
        self.assertTrue(parse_intake({'id':'hkit'}, html).explicit_empty)
        with self.assertRaises(CrawlError):
            parse_intake({'id':'hkit'}, html.replace('There are currently no vacancies.', '<a href="/new">Part-time Lecturer</a>', 1))
        with self.assertRaises(CrawlError):
            parse_intake({'id':'cuscs'}, '<main><table>Part-time Instructor examples</table><table>New vacancy</table></main>')

    def test_hkmu_qualification_and_mixed_portal_not_employment(self):
        fields = {'reqlistitem.contestnumber':'2600ABC', 'reqlistitem.title':'Temporary Research Assistant',
                  'reqlistitem.description':'University introduction.',
                  'reqlistitem.qualification':'<p>Major Duties: Study AI ethics.</p><p>' + 'Research experience is required. ' * 6 + '</p>',
                  'reqlistitem.jobfield':'Temporary/Part-time R&D', 'reqlistitem.unpostingdate':'01/Oct/2026, 11:59:00 PM'}
        html = 'descRequisition: {_hlid: ' + repr(list(fields)) + '}\n' + "fillList('requisitionDescriptionInterface', 'descRequisition', " + repr(['!*!'+quote(v) for v in fields.values()]) + ');'
        job = record(SOURCE, 'Temporary Research Assistant', 'https://example.edu/ad', '2600ABC')
        parse_hkmu_detail(job, html)
        self.assertEqual(job['employment_type'], 'unknown')
        self.assertIn('AI ethics', job['match_text'])
        self.assertNotIn('University introduction', job['match_text'])
        self.assertEqual(job['deadline'], '2026-10-01')

    def test_yccece_all_links_identity_and_unsupported_body(self):
        state = '{type:a,key:"DetailLink-1",content:{title:"Part-time Lecturer",link:"?job=lecturer"}}'
        html = '<script>window.__NUXT__=' + state + '</script>'
        jobs = parse_yccece_list(SOURCE, html)
        self.assertEqual(len(jobs), 1)
        with self.assertRaises(CrawlError):
            parse_yccece_list(SOURCE, html.replace('</script>', ',"?job=missing"</script>'))
        payload = {'data': {'category':'job','key':'lecturer','title':'Part-time Lecturer','time':'2026-09-17',
                  'components': json.dumps([{'type':'CustomText','content':{'text':{'content':'<p>'+'Teach ethics. ' * 15 + '</p>'}}}])}}
        parse_yccece_detail(jobs[0], payload)
        self.assertIsNone(jobs[0]['posted_date'])
        payload['data']['key'] = 'another-job'
        with self.assertRaises(CrawlError):
            parse_yccece_detail(jobs[0], payload)


if __name__ == '__main__':
    unittest.main()
