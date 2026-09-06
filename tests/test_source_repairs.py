import json
import unittest
from unittest.mock import Mock, patch

from monitor.cornerstone import anonymous_headers, listing_date, parse_lingnan_page, parse_lingnan_detail, collect_lingnan
from monitor.hsu import parse_hsu_list, parse_hsu_detail
from monitor.http import CrawlError, PublicClient
from monitor.official import parse_hksyu_list
from monitor.peoplesoft import fetch_hkust_detail, parse_hkust_detail, collect_known_hkust
from monitor.model import record
from monitor.run import fetch_source

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
    def test_known_hkust_ads_never_claim_a_complete_fresh_list(self):
        source = dict(SOURCE, id='hkust', group='hkust', adapter='hkust')
        known = [record(source, 'Lecturer', f'https://hrmsxprod.psft.ust.hk:8044/job/{i}', str(i), detail_complete=False) for i in [1, 2]]
        known.append(record(source, 'Professor', 'https://apply.interfolio.com/3', '3', detail_complete=False))
        def detail(job, client):
            if job['reference'] == '2':
                raise CrawlError('connection failed')
            job.update(detail_complete=True, description='Newly read public advertisement')
            return job
        with patch('monitor.peoplesoft.fetch_hkust_detail', side_effect=detail) as fetch:
            batch = collect_known_hkust(source, Mock(), known, 'list connection failed')
        self.assertFalse(batch.complete); self.assertEqual(batch.pages, 0)
        self.assertEqual([j['reference'] for j in batch.jobs], ['1'])
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('可能遺漏新增職位', batch.errors[0])
        self.assertFalse(known[0]['detail_complete'])
        with patch('monitor.run.collect', side_effect=CrawlError('denied', stop_source=True)), patch('monitor.peoplesoft.collect_known_hkust') as fallback:
            batch = fetch_source(source, known)
            fallback.assert_not_called(); self.assertFalse(batch.complete)

    def test_hkust_public_frame_identity_and_scoped_port(self):
        host = 'hrmsxprod.psft.ust.hk'
        source = dict(SOURCE, allowed_hosts=[host, 'example.edu'], allowed_ports={host: [8044]})
        url = f'https://{host}:8044/psp/hrmsxprod/jobs?JobOpeningId=9419'
        client = PublicClient(source); client.validate(url)
        for invalid in [url.replace(':8044', ':8443'), url.replace(host, 'example.edu')]:
            with self.assertRaises(CrawlError):
                client.validate(invalid)
        job = record(source, 'Teaching-track Faculty Position', url, '9419', detail_complete=False)
        detail = '''<span id="HRS_JO_WRK_POSTING_TITLE$0">Teaching-track Faculty Position</span>
        <span id="HRS_JO_WRK_HRS_JOB_OPENING_ID$0">9419</span><span id="Z_HRS_APPL_DW_DESCR100$0">Department of ISOM</span>
        <div id="HRS_JO_PDSC_VW_DESCRLONG$0"><p>Applicants should hold a relevant degree and teaching experience. Duties include teaching operations management and supporting undergraduate students.</p><p>The appointee will start in July 2024.</p></div>'''
        frame = f'https://{host}:8044/psc/hrmsxprod/jobs?Page=HRS_CE_JOB_DTL&JobOpeningId=9419'
        client.get = Mock(side_effect=[f'<frame name="TargetContent" src="{frame}">', detail])
        fetch_hkust_detail(job, client)
        self.assertTrue(job['detail_complete']); self.assertEqual(job['department'], 'Department of ISOM')
        self.assertIsNone(job['posted_date'])
        with self.assertRaises(CrawlError):
            parse_hkust_detail(job, detail.replace('>9419<', '>9420<'))
        client.get = Mock(return_value=f'<frame name="TargetContent" src="{frame.replace("9419", "9420")}">')
        with self.assertRaises(CrawlError):
            fetch_hkust_detail(job, client)
        self.assertEqual(client.get.call_count, 1)

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

    def test_hsu_ad_without_invitation_and_research_department(self):
        title = 'Department of Marketing - Executive Officer / Assistant Officer'
        source = dict(SOURCE, url='https://www.hsu.edu.hk/en/job-opportunities/')
        job = record(source, title, 'https://recruit.hsu.edu.hk/opening/content.php?id=3853', '3853', detail_complete=False)
        body = '<p>HSU is a liberal arts university with a critical thinking mission.</p><h2>' + title + '</h2><p>(Ref: EO/AO (MKT) 2026-08-21)</p><h3>Responsibilities</h3><p>Provide administrative services to the department, assist with daily operations, and support events and student enquiries.</p><p>Please apply on or before 6 September 2026.</p>'
        parse_hsu_detail(job, body)
        self.assertTrue(job['detail_complete']); self.assertNotIn('critical thinking', job['match_text'])
        self.assertEqual(job['deadline'], '2026-09-06')
        with self.assertRaises(CrawlError):
            parse_hsu_detail(job, body.replace(title, 'Another vacancy'))
        title = 'Research Fellow - Department of Computer Science'
        job = record(source, title, source['url'], '3861')
        parse_hsu_detail(job, body.replace('Department of Marketing - Executive Officer / Assistant Officer', title))
        self.assertEqual(job['department'], 'Department of Computer Science')


if __name__ == '__main__':
    unittest.main()
