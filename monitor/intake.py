"""Official teaching intake pages with currently explicit no-vacancy notices.

If the notice changes, report a changed page for inspection rather than silently
returning an empty list. Evergreen instructor examples are not current openings.
"""
from .adapters import Batch, soup_of
from .http import CrawlError
from .model import clean_text


def parse_intake(source, html):
    soup = soup_of(html)
    if source['id'] == 'cuscs':
        tables = soup.select('main table')
        notices = [table for table in tables if 'Currently, there are no openings for teaching positions' in table.get_text(' ', strip=True)]
        if len(tables) == 2 and len(notices) == 1 and not notices[0].select('a[href]'):
            return Batch(pages=1, explicit_empty=True)
    elif source['id'] == 'hkit':
        content = soup.select_one('.pagecontent')
        if content:
            for category in ['Academic', 'Management', 'Admin', 'General']:
                heading = content.find(id=category)
                notice = heading.find_next_sibling() if heading else None
                if not notice or 'alert' not in notice.get('class', []) or clean_text(notice.get_text(' ', strip=True)) != 'There are currently no vacancies.':
                    break
            else:
                return Batch(pages=1, explicit_empty=True)
    raise CrawlError('官方空缺頁的「暫無職位」標示已改變，可能有新招聘；請查看官方頁面並更新讀取規則。')


def collect_intake(source, client):
    return parse_intake(source, client.get(source['url']))
