"""Official teaching intake pages with currently explicit no-vacancy notices.

If the notice changes, report a changed page for inspection rather than silently
returning an empty list. Evergreen instructor examples are not current openings.
"""
from .adapters import Batch, soup_of
from .http import CrawlError
from .model import clean_text


def _hkit_empty(soup):
    # November 2026 redesign: one explicitly empty recruitment section replaces
    # the four old categories. Mailto is the general CV invitation, not an ad.
    intro_text = 'Join the HKIT team! The Institute recruits academic and administrative staff from time to time.'
    privacy_text = '* The information provided by the applicant will be kept strictly confidential and will only be used for recruitment purposes'
    for content in soup.select('section.container-xl'):
        intro = content.find('p', class_='text-secondary')
        if not intro or clean_text(intro.get_text(' ', strip=True)) != intro_text:
            continue
        notices = content.select('.alert.alert-info')
        expected = 'No open vacancies at the moment. You are welcome to send your CV to recruit@hkit.edu.hk for future consideration (if applicable).'
        if (len(notices) == 1 and ' '.join(notices[0].get_text(' ', strip=True).split()) == expected
                and ' '.join(content.get_text(' ', strip=True).split()) == ' '.join((intro_text, expected, privacy_text))
                and all(a.get('href', '').lower() == 'mailto:recruit@hkit.edu.hk' for a in content.select('a[href]'))
                and not content.select('table, form, iframe, .job, .vacancy, [data-job-id]')):
            return True
    # Retain the known older layout, but never allow a leftover empty notice to
    # conceal a new linked vacancy in the same recruitment content.
    content = soup.select_one('.pagecontent')
    if content and not content.select('a[href], table, form, iframe'):
        for category in ['Academic', 'Management', 'Admin', 'General']:
            heading = content.find(id=category)
            notice = heading.find_next_sibling() if heading else None
            if not notice or 'alert' not in notice.get('class', []) or clean_text(notice.get_text(' ', strip=True)) != 'There are currently no vacancies.':
                return False
        return True
    return False


def parse_intake(source, html):
    soup = soup_of(html)
    if source['id'] == 'cuscs':
        tables = soup.select('main table')
        notices = [table for table in tables if 'Currently, there are no openings for teaching positions' in table.get_text(' ', strip=True)]
        if len(tables) == 2 and len(notices) == 1 and not notices[0].select('a[href]'):
            return Batch(pages=1, explicit_empty=True)
    elif source['id'] == 'hkit' and _hkit_empty(soup):
        return Batch(pages=1, explicit_empty=True, listing_complete=True)
    raise CrawlError('官方空缺頁的「暫無職位」標示已改變，可能有新招聘；請查看官方頁面並更新讀取規則。')


def collect_intake(source, client):
    return parse_intake(source, client.get(source['url']))
