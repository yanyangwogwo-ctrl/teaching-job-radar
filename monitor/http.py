"""Conservative public HTTP access: host allowlist, robots, pacing, no challenge bypass."""
from __future__ import annotations

import time
import re
from urllib.parse import urljoin, urlsplit, quote

import requests


class CrawlError(RuntimeError):
    def __init__(self, message, *, stop_source=False):
        super().__init__(message)
        self.stop_source = stop_source


def robot_path(value):
    # RFC 9309 comparison: decode unreserved octets, retain encoded reserved
    # characters, and compare non-ASCII text as percent-encoded UTF-8.
    value = quote(value, safe="/%?&=:+,;@!$'()*~-._")
    def normalize(match):
        character = chr(int(match.group(1), 16))
        return character if character.isascii() and (character.isalnum() or character in '-._~') else '%' + match.group(1).upper()
    return re.sub(r'%([0-9a-fA-F]{2})', normalize, value)


class RobotsPolicy:
    def __init__(self, text, agent='TeachingJobRadar'):
        groups, agents, rules, delay, started = [], [], [], 1.1, False
        for raw in text.lstrip('\ufeff').splitlines() + ['User-agent: __end__']:
            line = raw.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (part.strip() for part in line.split(':', 1))
            key = key.lower()
            if key == 'user-agent':
                if started:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, started = [], [], 1.1, False
                agents.append(value.lower())
            elif agents and key in ('allow', 'disallow', 'crawl-delay'):
                started = True
                if key == 'crawl-delay':
                    try:
                        delay = max(1.1, float(value))
                    except ValueError:
                        raise CrawlError('robots.txt 的爬取間隔無法確認。', stop_source=True) from None
                elif value:
                    rules.append((key == 'allow', robot_path(value)))
        scored = [(max((len(a) if a != '*' else 0 for a in names if a == '*' or a in agent.lower()), default=-1), rules, delay) for names, rules, delay in groups]
        best = max((score for score, _, _ in scored), default=-1)
        self.rules = [rule for score, rules, _ in scored if score == best and score >= 0 for rule in rules]
        self.delay = max((delay for score, _, delay in scored if score == best and score >= 0), default=1.1)

    def can_fetch(self, url):
        bits = urlsplit(url)
        path = robot_path((bits.path or '/') + ('?' + bits.query if bits.query else ''))
        matches = []
        for allow, pattern in self.rules:
            ending = pattern.endswith('$')
            body = pattern[:-1] if ending else pattern
            regex = '^' + '.*'.join(re.escape(p) for p in body.split('*')) + ('$' if ending else '')
            if re.search(regex, path):
                matches.append((len(body.replace('*', '')), allow))
        return max(matches, default=(0, True))[1]


class PublicClient:
    agent = 'TeachingJobRadar/0.1 (personal academic vacancy monitor; daily)'

    def __init__(self, source: dict):
        self.source = source
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': self.agent, 'Accept': 'text/html,application/json;q=0.9,*/*;q=0.5'})
        self.robots = {}
        self.last_request = {}
        self.delays = {}
        self.count = 0

    def validate(self, url: str):
        bits = urlsplit(url)
        if bits.scheme != 'https' or bits.hostname not in self.source['allowed_hosts'] or bits.username or bits.password or bits.port not in (None, 443):
            raise CrawlError('來源要求轉往未核准的網址，已停止。', stop_source=True)

    def _request(self, url: str, *, robots=False, method='GET', data=None, headers=None) -> requests.Response:
        initial_host = urlsplit(url).hostname
        timeout_retries = 0
        for _ in range(5):
            self.validate(url)
            host = urlsplit(url).hostname
            if self.count >= self.source.get('max_requests', 400):
                raise CrawlError('超過每來源的安全請求上限，未完成整批檢索。', stop_source=True)
            gap = self.delays.get(host, 1.1) - (time.monotonic() - self.last_request.get(host, 0))
            if gap > 0:
                time.sleep(gap)
            self.count += 1
            self.last_request[host] = time.monotonic()
            response = None
            try:
                response = self.session.request(method, url, json=data, headers=headers, timeout=(15, 40), allow_redirects=False, stream=True)
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(url, response.headers.get('Location', ''))
                    response.close()
                    if method != 'GET':
                        raise CrawlError('公開搜尋介面重新導向，需確認新位置。', stop_source=True)
                    self.validate(target)
                    if robots:
                        if urlsplit(target).hostname != initial_host or urlsplit(target).path != '/robots.txt':
                            raise CrawlError('robots.txt 轉往其他位置，未能確認讀取規則。', stop_source=True)
                    elif not self.allowed(target):
                        raise CrawlError('重新導向的路徑不允許自動讀取。', stop_source=True)
                    url = target
                    continue
                chunks, length = [], 0
                for chunk in response.iter_content(65536):
                    length += len(chunk)
                    if length > 6_000_000:
                        response.close()
                        raise CrawlError('頁面超過安全大小上限。')
                    chunks.append(chunk)
                response._content = b''.join(chunks)
                response._content_consumed = True
                response.close()
                response.encoding = response.encoding if response.encoding and response.encoding.lower() != 'iso-8859-1' else 'utf-8'
                return response
            except requests.Timeout as error:
                if response is not None:
                    response.close()
                # Retry a public GET connection/header timeout once. Pacing, host validation
                # and request caps still run again. Never retry a challenge,
                # denied robots response or a search POST this way.
                if method == 'GET' and not robots and response is None and timeout_retries == 0:
                    timeout_retries += 1
                    continue
                raise CrawlError(f'{host} 連線未完成（{type(error).__name__}）。') from None
            except requests.RequestException as error:
                if response is not None:
                    response.close()
                # Do not emit cookies, headers or request URLs in logs.
                raise CrawlError(f'{host} 連線未完成（{type(error).__name__}）。') from None
        raise CrawlError('來源重新導向或重試次數過多。')

    def allowed(self, url: str) -> bool:
        self.validate(url)
        bits = urlsplit(url)
        origin = f'{bits.scheme}://{bits.netloc}'
        if origin not in self.robots:
            # Fail closed even after a transient exception; never cache failure
            # as permission for the following detail request.
            self.robots[origin] = False
            response = self._request(origin + '/robots.txt', robots=True)
            if response.status_code in (404, 410):
                parser = RobotsPolicy('User-agent: *\nAllow: /')
            elif response.status_code == 200 and not response.text.lstrip('\ufeff \r\n\t').startswith(('<', '{', '[')) and not (response.headers.get('cf-mitigated') or response.headers.get('x-amzn-waf-action')):
                parser = RobotsPolicy(response.text)
            else:
                raise CrawlError(f'未能確認 robots.txt（HTTP {response.status_code}），已保留舊資料。', stop_source=True)
            delay = parser.delay
            if delay > 30:
                raise CrawlError('網站要求較長爬取間隔，需另行安排該來源。', stop_source=True)
            self.robots[origin] = parser
            self.delays[bits.hostname] = max(1.1, delay)
        parser = self.robots[origin]
        if parser is False:
            raise CrawlError('robots.txt 尚未成功確認，已停止該來源。', stop_source=True)
        return parser.can_fetch(url)

    def response(self, url: str, *, data=None, headers=None):
        if not self.allowed(url):
            raise CrawlError('此路徑的 robots.txt 不允許自動讀取，已停止該來源。', stop_source=True)
        if data is not None and urlsplit(url).path not in self.source.get('read_only_post_paths', []):
            raise CrawlError('此來源未設定唯讀搜尋 POST 路徑。', stop_source=True)
        response = self._request(url, method='POST' if data is not None else 'GET', data=data, headers=headers)
        challenge = response.headers.get('x-amzn-waf-action') or response.headers.get('cf-mitigated')
        if response.status_code != 200 or challenge:
            raise CrawlError(f'網站未提供可讀頁面（HTTP {response.status_code}）。', stop_source=bool(challenge) or response.status_code in (401, 403, 429))
        text = response.text
        if any(token in text.lower() for token in ('<title>just a moment', '<title>access denied', 'cf-chl-', 'awswaf-captcha', 'incapsula incident id')):
            raise CrawlError('網站要求驗證，已停止該來源並保留舊資料。', stop_source=True)
        return response

    def get(self, url: str) -> str:
        return self.response(url).text

    def get_bytes(self, url: str) -> bytes:
        return self.response(url).content

    def search_json(self, url, data, headers=None):
        return self.response(url, data=data, headers=headers).json()
