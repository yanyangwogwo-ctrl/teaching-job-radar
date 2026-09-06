import json
import unittest
from unittest.mock import Mock

from monitor.cornerstone import anonymous_headers, listing_date, parse_lingnan_page, parse_lingnan_detail, collect_lingnan
from monitor.hsu import parse_hsu_list, parse_hsu_detail
from monitor.http import CrawlError, PublicClient
from monitor.official import parse_hksyu_list

SOURCE = {'id': 'lingnan', 'group': 'lingnan', 'institution': 'Lingnan',
          'url': 'https://lingnan.csod.com/ux/ats/careersite/4/home?c=lingnan&lang=en-US',
          'allowed_hosts': ['lingnan.csod.com', 'uk.api.csod.com'],
          'read_only_post_paths': ['/rec-job-search/external/jobs']}
BOOTSTRAP = 'csod.context=' + json.dumps({'user': -103, 'corp': 'lingnan',
    'endpoints': {'cloud': 'https://uk.api.csod.com/'}, 'token': 'test-public-session'}) + ';'


def page(keys, total=None):
    return {'status': 'Success', 'data': {'totalCount': len(keys) if total is None else total,
        'requisitions': [{'requisitionId': key, 'displayJobTitle': 'Part-time Lecturer',
            'postingEffectiveDate': '3/24/2026', 'postingExpirationDate': '-',
            'externalDescription': 'INTERNAL JOB DESCRIPTION'} for key in keys]}}


def advertisement(key):
    return {'status': 200, 'data': [{'items': [{'fields': {'id': key, 'title': 'Part-time Lecturer',
        'description': 'INTERNAL JOB DESCRIPTION', 'ad': '''<p>University critical thinking introduction.</p>
        <p>Applications are now invited for the following<br>post:</p><p>Part-time Lecturer</p>
        <p>Teach philosophy for full-time academic programmes. Applicants need a relevant degree and teaching experience.</p>
        <p>Please submit your application by 15 April 2026.</p><p>Review of applications continues until the post is filled.</p>'''}}]}]}


class LingnanTests(unittest.TestCase):
    def test_anonymous_bootstrap_and_no_session_header_leak(self):
        for replacement in [{'user': 123}, {'corp': 'another'}, {'endpoints': {'cloud': 'https://example.com/'}}]:
            context = json.loads(BOOTSTRAP.split('=', 1)[1][:-1]); context.update(replacement)
            with self.assertRaises(CrawlError):
                anonymous_headers('csod.context=' + json.dumps(context))
        client = PublicClient(SOURCE)
        responses = [Mock(status_code=404), Mock(status_code=200, text='{}', headers={})]
        client._request = Mock(side_effect=responses)
        client.response('https://uk.api.csod.com/rec-job-search/external/jobs', data={}, headers=anonymous_headers(BOOTSTRAP))
        self.assertNotIn('headers', client._request.call_args_list[0].kwargs)
        self.assertNotIn('Authorization', client.session.headers)
        self.assertEqual(client._request.call_args_list[1].kwargs['headers']['Authorization'], 'Bearer test-public-session')

    def test_ad_identity_body_dates_and_employment(self):
        jobs, total = parse_lingnan_page(SOURCE, page([2947])); job = jobs[0]
        self.assertEqual(job['description'], '')
        self.assertFalse(job['detail_complete'])
        with self.assertRaises(CrawlError):
            parse_lingnan_detail(job, advertisement(3054))
        parse_lingnan_detail(job, advertisement(2947))
        self.assertTrue(job['detail_complete'])
        self.assertNotIn('critical thinking introduction', job['match_text'])
        self.assertEqual(job['employment_type'], 'part-time')
        self.assertEqual(job['deadline'], '2026-04-15')
        self.assertEqual(job['reference'], '2947')
        self.assertEqual(job['posted_date'], '2026-03-24')
        self.assertEqual(listing_date('1/6/2026'), '2026-01-06')
        self.assertEqual(listing_date('7/23/2026'), '2026-07-23')
        self.assertIsNone(listing_date('-'))
        broken = advertisement(2947); broken['data'][0]['items'][0]['fields']['ad'] = ''
        with self.assertRaises(CrawlError):
            parse_lingnan_detail(job, broken)

    def client(self, pages):
        client = Mock(); client.get.return_value = BOOTSTRAP
        client.search_json.side_effect = pages
        client.response.side_effect = lambda url, **kwargs: Mock(json=lambda: advertisement(int(url.split('/JobRequisitions/')[1].split('?')[0])))
        return client

    def test_all_pages_and_details_and_explicit_empty(self):
        client = self.client([page(list(range(1,26)), 26), page([26], 26)])
        result = collect_lingnan(SOURCE, client)
        self.assertTrue(result.complete); self.assertEqual(len(result.jobs), 26)
        self.assertTrue(all(j['detail_complete'] for j in result.jobs))
        self.assertEqual(client.search_json.call_args.args[1]['pageNumber'], 2)
        self.assertIsNone(client.search_json.call_args.args[1]['postingsWithinDays'])
        result = collect_lingnan(SOURCE, self.client([page([])]))
        self.assertTrue(result.complete); self.assertTrue(result.explicit_empty)

    def test_incomplete_lists_and_denial_preserve_partial_records(self):
        for second in [page([], 26), page([1], 26), page([26], 27)]:
            result = collect_lingnan(SOURCE, self.client([page(list(range(1,26)), 26), second]))
            self.assertFalse(result.complete); self.assertFalse(result.explicit_empty)
            self.assertEqual(len(result.jobs), 25)
        client = self.client([page([1,2])]); client.response.side_effect = CrawlError('denied', stop_source=True)
        result = collect_lingnan(SOURCE, client)
        self.assertFalse(result.complete); self.assertEqual(len(result.jobs), 2)
        self.assertEqual(client.response.call_count, 1)
        with self.assertRaises(CrawlError):
            parse_lingnan_page(SOURCE, {'status': 'Failure', 'data': {'totalCount': 0, 'requisitions': []}})


class OfficialHTMLTests(unittest.TestCase):
    def test_hksyu_updated_english_attachment_without_filename_date(self):
        html = '''<div class="accordion"><div class="card"><div class="card-header">Academic Posts</div>
        <table class="table-striped"><tbody><tr><td>Counselling and Psychology</td><td><a href="/assets/careers/counpsy/2026/May/counpsy_ap_ft_2026-05-28.pdf">Assistant Professor</a></td><td>FT</td><td>Until filled</td></tr></tbody></table></div></div>'''
        source = dict(SOURCE, url='https://www.hksyu.edu/en/snippets/external-vacancy')
        jobs = parse_hksyu_list(source, html)
        self.assertEqual(len(jobs), 1); self.assertIn('/May/', jobs[0]['url'])
        self.assertIsNone(jobs[0]['posted_date'])

    def test_hsu_public_links_boilerplate_and_explicit_deadline(self):
        source = dict(SOURCE, url='https://www.hsu.edu.hk/en/job-opportunities/')
        html = '<a href="https://recruit.hsu.edu.hk/opening/content.php?id=3859">Executive Officer</a><a href="https://recruit.hsu.edu.hk/login.php">Login</a>'
        job = parse_hsu_list(source, html)[0]
        detail = '''<p>Our liberal arts University teaches critical thinking and AI literacy.</p>
        <p>The University now invites applications for the following position:</p>
        <h2>Registry - Executive Officer (Ref: EO2026-08-01)</h2>
        <p>Applicants should have experience supporting office administration and providing registry services to students.</p>
        <p>Please apply on or before 20 September 202 6.</p><p>Review of applications continues until the post is filled.</p>'''
        parse_hsu_detail(job, detail)
        self.assertEqual(job['title'], 'Registry - Executive Officer')
        self.assertEqual(job['department'], 'Registry')
        self.assertNotIn('AI literacy', job['match_text'])
        self.assertIsNone(job['posted_date'])
        self.assertEqual(job['deadline'], '2026-09-20')
        with self.assertRaises(CrawlError):
            parse_hsu_detail(job, '<h1>System maintenance</h1>')
        with self.assertRaises(CrawlError):
            parse_hsu_list(source, '<h1>System maintenance</h1>')


if __name__ == '__main__':
    unittest.main()
