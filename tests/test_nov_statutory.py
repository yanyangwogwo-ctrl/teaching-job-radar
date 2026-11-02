import html
import json
import unittest
from unittest.mock import Mock

from monitor.http import CrawlError
from monitor.model import record
from monitor.statutory import collect_hkapa, parse_hkapa, parse_thei_detail, parse_eduhk_list


SOURCE = {'id': 'hkapa', 'group': 'hkapa', 'institution': 'HKAPA',
          'url': 'https://www.hkapa.edu/job-opportunity'}
BODY = 'Teach humanities and ethics. Relevant academic qualifications and teaching experience are required. ' * 3


def page(refs, *, number=1, total=3, language='', incomplete=(), **overrides):
    base = 'https://www.hkapa.edu/' + language + 'job-opportunity'
    size = 2
    last = (total + size - 1) // size
    items = {'total': total, 'per_page': size, 'current_page': number,
             'last_page': last, 'next_page_url': f'{base}?page={number + 1}' if number < last else ''}
    items.update(overrides)
    state = {'component': 'job-listing', 'props': {'pageData': {'items': items}}}
    panels = []
    for ref in refs:
        body = 'Please refer to Chinese version.' if ref in incomplete else BODY + ref
        panels.append(f'''<section class="job-detail__inner">
            <h3 class="job-detail__title">Part-time Lecturer {ref}</h3>
            <a class="job-detail__btnlink" href="/job-opportunity/{ref}">Details</a>
            <p class="job-detail__ref"><strong>{ref}</strong></p>
            <div class="job-content">{body}</div>
            <p class="job-detail__type">Part-time</p>
            <p class="job-detail__dl">Closing date: 30 November 2026</p>
        </section>''')
    label = '' if language else f'Showing {(number - 1) * size + 1}-{min(number * size, total)} of {total} job openings'
    return f'<div id="app" data-page="{html.escape(json.dumps(state), quote=True)}">{label}{"".join(panels)}</div>'


class HkapaPaginationTests(unittest.TestCase):
    def test_all_pages_and_differently_ordered_language_counterparts(self):
        client = Mock()
        client.get.side_effect = [page(['A', 'B'], incomplete=['B']), page(['C'], number=2, incomplete=['C']),
                                  page(['C', 'A'], language='tch/'), page(['B'], number=2, language='tch/')]
        result = collect_hkapa(SOURCE, client)
        self.assertTrue(result.complete, result.errors)
        self.assertEqual([job['reference'] for job in result.jobs], ['A', 'B', 'C'])
        self.assertTrue(all(job['detail_complete'] for job in result.jobs))
        self.assertEqual(result.pages, 4)
        self.assertEqual([call.args[0] for call in client.get.call_args_list], [SOURCE['url'], SOURCE['url'] + '?page=2',
                          'https://www.hkapa.edu/tch/job-opportunity', 'https://www.hkapa.edu/tch/job-opportunity?page=2'])
        self.assertTrue(result.jobs[1]['description'].endswith('B'))
        self.assertTrue(result.jobs[2]['description'].endswith('C'))

    def test_later_page_failure_retains_successful_jobs(self):
        client = Mock()
        client.get.side_effect = [page(['A', 'B']), CrawlError('Temporary connection failure')]
        result = collect_hkapa(SOURCE, client)
        self.assertFalse(result.complete)
        self.assertEqual([job['reference'] for job in result.jobs], ['A', 'B'])
        self.assertIn('Temporary connection failure', result.errors)

    def test_language_recovery_does_not_hide_list_failure(self):
        client = Mock()
        client.get.side_effect = [page(['A', 'B'], incomplete=['B']), CrawlError('Second English page failed'),
                                  page(['C', 'A'], language='tch/'), page(['B'], number=2, language='tch/')]
        result = collect_hkapa(SOURCE, client)
        self.assertFalse(result.complete)
        self.assertTrue(all(job['detail_complete'] for job in result.jobs))
        self.assertIn('Second English page failed', result.errors)

    def test_blocked_later_page_stops_language_requests(self):
        client = Mock()
        client.get.side_effect = [page(['A', 'B'], incomplete=['B']), CrawlError('Blocked', stop_source=True)]
        result = collect_hkapa(SOURCE, client)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.jobs), 2)
        self.assertEqual(client.get.call_count, 2)

    def test_duplicate_refs_or_changed_total_do_not_pass_coverage(self):
        for second in (page(['B'], number=2), page(['C', 'D'], number=2, total=4)):
            with self.subTest(second=second):
                client = Mock()
                client.get.side_effect = [page(['A', 'B']), second]
                result = collect_hkapa(SOURCE, client)
                self.assertFalse(result.complete)
                self.assertEqual(len(result.jobs), 2)

    def test_inconsistent_paging_and_cross_origin_links_rejected(self):
        invalid = [page(['A']), page(['A', 'B'], current_page=2),
                   page(['A', 'B'], next_page_url='https://other.edu/jobs?page=2'),
                   page(['A', 'B'], next_page_url='https://www.hkapa.edu/search?page=2'),
                   page(['A', 'B'], last_page=3), page(['A', 'A'])]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(CrawlError):
                parse_hkapa(SOURCE, content)

    def test_single_page_cannot_claim_complete_when_next_page_exists(self):
        self.assertFalse(parse_hkapa(SOURCE, page(['A', 'B'])).complete)
        self.assertTrue(parse_hkapa(SOURCE, page(['A', 'B'], total=2)).complete)
        self.assertTrue(parse_hkapa(SOURCE, page(['A', 'B'], total=2, next_page_url=None)).complete)
        with self.assertRaises(CrawlError):
            parse_hkapa(SOURCE, page(['A', 'B'], next_page_url=None))


class TheiChineseReferenceTests(unittest.TestCase):
    def job(self):
        return record(dict(SOURCE, id='thei', group='thei', institution='THEi'), '二級機械工',
                      'https://thei.edu.hk/afmo_mechanical/')

    def test_chinese_label_with_fullwidth_colon_preserves_identity(self):
        job = self.job()
        original_id = job['id']
        content = '<div class="elementor-widget-theme-post-title">二級機械工</div><div class="elementor-widget-theme-post-content"><p>參考編號： AFMO/MECHII/07/26</p><p>' + BODY + '</p></div>'
        parse_thei_detail(job, content)
        self.assertEqual(job['reference'], 'AFMO/MECHII/07/26')
        self.assertEqual(job['id'], original_id)
        self.assertTrue(job['detail_complete'])
        self.assertIsNone(job['posted_date'])

    def test_missing_reference_or_short_body_still_rejected(self):
        for body in (BODY, '參考編號： AFMO/MECHII/07/26'):
            with self.subTest(body=body), self.assertRaises(CrawlError):
                parse_thei_detail(self.job(), '<div class="elementor-widget-theme-post-title">二級機械工</div><div class="elementor-widget-theme-post-content">' + body + '</div>')


class EduhkBlankReferenceTests(unittest.TestCase):
    def source(self):
        return dict(SOURCE, id='eduhk', group='eduhk', institution='EdUHK',
                    url='https://www.eduhk.hk/en/current-openings')

    def listing(self, reference='Ref:'):
        return '<section><a href="/cms/f/career/33618/SZEDC_AA_Job%20Ad.pdf">Administrative Assistant</a><p>EdUHK Shenzhen Educational Development Centre</p><p>' + reference + '</p><p>Ad Date:</p><p>28 May 2026</p><p>Close Date:</p><p>N/A</p></section>'

    def test_explicit_blank_reference_uses_pdf_identity_and_actual_ad_date(self):
        source = self.source()
        batch, _ = parse_eduhk_list(source, self.listing())
        job = batch.jobs[0]
        expected = record(source, job['title'], job['url'])
        self.assertEqual(job['reference'], '')
        self.assertEqual(job['id'], expected['id'])
        self.assertEqual(job['posted_date'], '2026-05-28')
        self.assertIsNone(job['deadline'])
        self.assertEqual(job['department'], 'EdUHK Shenzhen Educational Development Centre')

    def test_missing_label_or_unrecognised_ref_remains_failure(self):
        for reference in ('', 'Ref: new format'):
            with self.subTest(reference=reference), self.assertRaises(CrawlError):
                parse_eduhk_list(self.source(), self.listing(reference))

    def test_existing_numeric_reference_retained(self):
        batch, _ = parse_eduhk_list(self.source(), self.listing('Ref: 2600123'))
        self.assertEqual(batch.jobs[0]['reference'], '2600123')


if __name__ == '__main__':
    unittest.main()
