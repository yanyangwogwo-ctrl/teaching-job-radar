"""Conservative public HTTP access: host allowlist, robots, pacing, no challenge bypass."""
from __future__ import annotations

import time
import re
import json
import hashlib
import random
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlsplit, quote

import requests

from .tls import configure_host_tls


# Failure kinds outside this program's control. They are reported as visible
# warnings (old data kept) instead of failing the daily workflow; anything else
# (format changes, unexpected status codes, configuration limits) stays an error.
#   blocked  - the site refused or challenged automated access this round
#   network  - timeout, connection, TLS or temporary 5xx failure
#   deferred - details intentionally left for a later round (request budget)
EXTERNAL_KINDS = ('blocked', 'network', 'deferred')


class CrawlError(RuntimeError):
    def __init__(self, message, *, stop_source=False, kind=None):
        super().__init__(message)
        self.stop_source = stop_source
        self.kind = kind


def cloudflare_response(response) -> bool:
    headers = {key.lower(): str(value).lower() for key, value in response.headers.items()}
    return bool(headers.get('cf-mitigated') or headers.get('cf-ray') or 'cloudflare' in headers.get('server', ''))


def tls_failure_reason(error):
    # Inspect locally but emit only fixed labels, never exception URLs,
    # query strings, headers or certificates.
    message = str(error).upper()
    reasons = (
        ('UNSAFE_LEGACY_RENEGOTIATION_DISABLED', '網站使用不相容的舊式 TLS 重新協商'),
        ('UNABLE TO GET LOCAL ISSUER CERTIFICATE', '憑證鏈未能連至本機信任的簽發者（需核對中繼憑證及信任庫）'),
        ('CERTIFICATE HAS EXPIRED', '網站憑證已過期'),
        ('HOSTNAME MISMATCH', '網站憑證的主機名稱不符'),
        ('SELF-SIGNED CERTIFICATE', '網站憑證鏈含未受信任的自簽憑證'),
        ('CERTIFICATE_VERIFY_FAILED', '網站憑證驗證失敗'),
        ('TLSV1_ALERT_PROTOCOL_VERSION', '網站 TLS 協議版本不相容'),
        ('SSLV3_ALERT_HANDSHAKE_FAILURE', '網站 TLS 握手失敗'),
        ('UNEXPECTED_EOF_WHILE_READING', '網站在 TLS 連線期間提早中斷'),
        ('WRONG_VERSION_NUMBER', '網站回應的 TLS 版本不符'),
    )
    return next((label for marker, label in reasons if marker in message), '網站 TLS 連線未完成，原因待核對')


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
        configure_host_tls(self.session, source)
        self.session.headers.update({'User-Agent': self.agent, 'Accept': 'text/html,application/json;q=0.9,*/*;q=0.5'})
        self.robots = {}
        self.robots_failure = {}
        self.last_request = {}
        self.delays = {}
        # A per-source minimum gap keeps low-volume reading under a site's
        # published or observed rate limit. It never shortens robots Crawl-delay.
        self.min_interval = max(1.1, min(120.0, float(source.get('min_request_interval', 1.1))))
        self.count = 0
        self.previous_jobs = {}
        self.cache_now = datetime.now(timezone.utc)
        self.detail_cache_hits = 0
        self.detail_fetches = 0
        self.detail_refresh_candidates = 0
        budget = source.get('max_detail_fetches')
        self.detail_budget = max(1, int(budget)) if budget else None
        self.detail_queued = 0
        self.detail_deferred = 0
        # (message, kind) for every failure raised by this client. Used to tell
        # site-side limits from program errors after an adapter has caught them.
        self.failures = []

    def _error(self, message, *, kind=None, stop_source=False):
        self.failures.append((message, kind))
        return CrawlError(message, stop_source=stop_source, kind=kind)

    def record_failure(self, message, kind):
        self.failures.append((message, kind))

    def set_previous_jobs(self, jobs, now=None):
        """Use verified parsed details, never cached lists, to reduce requests."""
        values = jobs.values() if isinstance(jobs, dict) else jobs
        self.previous_jobs = {job['id']: job for job in values
                              if job.get('source_id') == self.source['id']}
        if now:
            self.cache_now = datetime.fromisoformat(now) if isinstance(now, str) else now
            if self.cache_now.tzinfo is None:
                self.cache_now = self.cache_now.replace(tzinfo=timezone.utc)

    def reuse_detail(self, job):
        # Fingerprint the LISTING before a detail parser expands its title,
        # department or dates. A change in any listed field forces a refresh.
        fields = ('url', 'reference', 'title', 'department', 'posted_date',
                  'deadline', 'deadline_type', 'deadline_raw', 'employment_type')
        fingerprint = hashlib.sha256(json.dumps({key: job.get(key) for key in fields},
                                      sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        job['listing_fingerprint'] = fingerprint
        old = self.previous_jobs.get(job['id'], {})
        reusable = bool(old.get('detail_complete') and old.get('description')
                        and not old.get('detail_refresh_failed') and not old.get('source_closed')
                        and old.get('status') not in ('missing', 'closed')
                        and old.get('listing_fingerprint') == fingerprint)
        try:
            verified = datetime.fromisoformat(old.get('detail_verified_at', ''))
            if verified.tzinfo is None:
                verified = verified.replace(tzinfo=timezone.utc)
            age = self.cache_now - verified
            # Weekly by default. A source whose site limits request volume may
            # extend this explicitly in sites.yaml, never beyond 30 days; any
            # change to a listed field still forces an immediate refresh.
            ttl = max(1, min(30, int(self.source.get('detail_cache_days', 7))))
            reusable = reusable and timedelta(0) <= age < timedelta(days=ttl)
        except (ValueError, TypeError):
            reusable = False
        if old.get('deadline_type') == 'closing' and old.get('deadline'):
            try:
                closing = datetime.fromisoformat(old['deadline']).date()
                today = self.cache_now.astimezone(ZoneInfo('Asia/Hong_Kong')).date()
                if closing <= today + timedelta(days=3):
                    reusable = False
            except (ValueError, TypeError):
                reusable = False
        if not reusable:
            self.detail_refresh_candidates += 1
            return False
        for key in ('title', 'department', 'description', 'match_text', 'employment_type',
                    'posted_date', 'deadline', 'deadline_type', 'deadline_raw',
                    'detail_verified_at'):
            if key in old:
                job[key] = old[key]
        job.update(detail_complete=True, detail_refresh_failed=False, detail_from_cache=True)
        self.detail_cache_hits += 1
        return True

    def mark_detail_success(self, job):
        self.detail_fetches += 1
        if job.get('detail_complete') or job.get('source_closed'):
            job.update(detail_verified_at=self.cache_now.isoformat(),
                       detail_refresh_failed=False, detail_from_cache=False)

    def mark_detail_failure(self, job):
        self.detail_fetches += 1
        job.update(detail_complete=False, detail_refresh_failed=True, detail_from_cache=False)

    def _retry_delay(self, response, attempt):
        """Honor short Retry-After values; don't retry early after long ones."""
        delay = min(8.0, 2.0 ** attempt) + random.uniform(0, 0.25)
        raw = response.headers.get('Retry-After')
        if raw:
            try:
                requested = float(raw)
            except (ValueError, TypeError):
                try:
                    stamp = parsedate_to_datetime(raw)
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    requested = (stamp - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    requested = 0
            if requested > 30:
                return None
            delay = max(delay, requested)
        return delay

    def validate(self, url: str):
        bits = urlsplit(url)
        ports = self.source.get('allowed_ports', {}).get(bits.hostname, [])
        if bits.scheme != 'https' or bits.hostname not in self.source['allowed_hosts'] or bits.username or bits.password or (bits.port not in (None, 443) and bits.port not in ports):
            # Name where it tried to send us (scheme, host and port only; no
            # path, query or credentials) so a moved page can be checked.
            shown = f'{bits.scheme}://{bits.hostname}' + (f':{bits.port}' if bits.port else '') if bits.hostname else '無法辨認的網址'
            raise CrawlError(f'來源要求轉往未核准的網址（{shown}），已停止。', stop_source=True)

    def _request(self, url: str, *, robots=False, method='GET', data=None, headers=None) -> requests.Response:
        initial_host = urlsplit(url).hostname
        timeout_retries = 0
        server_retries = 0
        for _ in range(5):
            self.validate(url)
            host = urlsplit(url).hostname
            if self.count >= self.source.get('max_requests', 400):
                raise self._error('超過每來源的安全請求上限，未完成整批檢索。', stop_source=True)
            interval = max(self.delays.get(host, 1.1), self.min_interval)
            gap = (interval - (time.monotonic() - self.last_request[host])) if host in self.last_request else 0
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
                        raise self._error('公開搜尋介面重新導向，需確認新位置。', stop_source=True)
                    self.validate(target)
                    if robots:
                        if urlsplit(target).hostname != initial_host or urlsplit(target).path != '/robots.txt':
                            raise self._error('robots.txt 轉往其他位置，未能確認讀取規則。', kind='blocked', stop_source=True)
                    elif not self.allowed(target):
                        raise self._error('重新導向的路徑不允許自動讀取。', kind='blocked', stop_source=True)
                    url = target
                    continue
                chunks, length = [], 0
                for chunk in response.iter_content(65536):
                    length += len(chunk)
                    if length > 6_000_000:
                        response.close()
                        raise self._error('頁面超過安全大小上限。')
                    chunks.append(chunk)
                response._content = b''.join(chunks)
                response._content_consumed = True
                response.close()
                response.encoding = response.encoding if response.encoding and response.encoding.lower() != 'iso-8859-1' else 'utf-8'
                challenge_body = any(token in response._content.lower() for token in
                    (b'captcha', b'cf-chl-', b'just a moment', b'incapsula', b'awswaf'))
                if (method == 'GET' and response.status_code in (502, 503, 504)
                        and server_retries < 2 and not challenge_body
                        and not response.headers.get('x-amzn-waf-action')
                        and not response.headers.get('cf-mitigated')):
                    delay = self._retry_delay(response, server_retries + 1)
                    if delay is not None:
                        server_retries += 1
                        time.sleep(delay)
                        continue
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
                raise self._error(f'{host} 連線未完成（{type(error).__name__}）。', kind='network') from None
            except requests.RequestException as error:
                if response is not None:
                    response.close()
                if isinstance(error, requests.exceptions.SSLError):
                    phase = 'robots.txt' if robots else '網頁'
                    raise self._error(f'{host} {phase}：{tls_failure_reason(error)}；保留憑證與主機名稱驗證。', kind='network', stop_source=True) from None
                # One bounded retry for a dropped GET connection before headers.
                # Certificate/proxy errors, robots and POST requests stay failures.
                if (isinstance(error, requests.ConnectionError)
                        and not isinstance(error, (requests.exceptions.SSLError, requests.exceptions.ProxyError))
                        and method == 'GET' and not robots and response is None and timeout_retries == 0):
                    timeout_retries += 1
                    continue
                # Do not emit cookies, headers or request URLs in logs.
                raise self._error(f'{host} 連線未完成（{type(error).__name__}）。', kind='network') from None
        raise self._error('來源重新導向或重試次數過多。', kind='network')

    def allowed(self, url: str) -> bool:
        self.validate(url)
        bits = urlsplit(url)
        origin = f'{bits.scheme}://{bits.netloc}'
        if origin not in self.robots:
            # Fail closed even after a transient exception; never cache failure
            # as permission for the following detail request.
            self.robots[origin] = False
            try:
                response = self._request(origin + '/robots.txt', robots=True)
                unavailable_exception = (response.status_code == 403
                    and bits.hostname in self.source.get('robots_unavailable_hosts', [])
                    and not response.headers.get('x-amzn-waf-action')
                    and not response.headers.get('cf-mitigated')
                    and not any(token in response.text.lower() for token in
                        ('captcha', 'cf-chl-', 'just a moment', 'incapsula', 'awswaf')))
                if response.status_code in (404, 410) or unavailable_exception:
                    parser = RobotsPolicy('User-agent: *\nAllow: /')
                elif response.status_code == 200 and not response.text.lstrip('\ufeff \r\n\t').startswith(('<', '{', '[')) and not (response.headers.get('cf-mitigated') or response.headers.get('x-amzn-waf-action')):
                    parser = RobotsPolicy(response.text)
                else:
                    if response.status_code == 200:
                        # Usually a bot-check page served to this network instead of
                        # the rules. Name the service when it identifies itself.
                        via = '（Cloudflare 驗證頁）' if cloudflare_response(response) else ''
                        raise self._error(f'robots.txt 回傳網頁、驗證頁或其他非規則內容（HTTP 200）{via}，未能確認自動讀取規則；已保留舊資料。',
                                          kind='blocked', stop_source=True)
                    code = response.status_code
                    kind = 'blocked' if code in (401, 403, 429) else ('network' if 500 <= code < 600 else None)
                    raise self._error(f'未能確認 robots.txt（HTTP {code}），已保留舊資料。', kind=kind, stop_source=True)
                delay = parser.delay
                if delay > 30:
                    raise self._error('網站要求較長爬取間隔，需另行安排該來源。', stop_source=True)
            except CrawlError as error:
                self.robots_failure[origin] = error.kind
                raise
            self.robots[origin] = parser
            self.delays[bits.hostname] = max(1.1, delay)
        parser = self.robots[origin]
        if parser is False:
            raise self._error('robots.txt 尚未成功確認，已停止該來源。', kind=self.robots_failure.get(origin, 'network'), stop_source=True)
        return parser.can_fetch(url)

    def response(self, url: str, *, data=None, headers=None):
        if not self.allowed(url):
            raise self._error('此路徑的 robots.txt 不允許自動讀取，已停止該來源。', kind='blocked', stop_source=True)
        if data is not None and urlsplit(url).path not in self.source.get('read_only_post_paths', []):
            raise self._error('此來源未設定唯讀搜尋 POST 路徑。', stop_source=True)
        response = self._request(url, method='POST' if data is not None else 'GET', data=data, headers=headers)
        challenge = response.headers.get('x-amzn-waf-action') or response.headers.get('cf-mitigated')
        if response.status_code != 200 or challenge:
            code = response.status_code
            if challenge or code in (202, 401, 403, 429):
                # 202 is how AWS WAF delivers its browser challenge. Not retried
                # and never solved; the request budget and pacing avoid it.
                kind = 'blocked'
            elif 500 <= code < 600:
                kind = 'network'
            else:
                kind = None  # e.g. 404/410: a moved page needs a person to check.
            label = '網站要求瀏覽器驗證' if challenge or code == 202 else '網站未提供可讀頁面'
            raise self._error(f'{label}（HTTP {code}）。', kind=kind, stop_source=bool(challenge) or code in (401, 403, 429))
        text = response.text
        if '_incapsula_resource' in text.lower() and re.search(r'<body\b[^>]*>\s*</body>', text, re.I):
            raise self._error('網站回傳存取驗證頁（HTTP 200），未提供職位正文；已停止該來源並保留舊資料。', kind='blocked', stop_source=True)
        if any(token in text.lower() for token in ('<title>just a moment', '<title>access denied', 'cf-chl-', 'awswaf-captcha', 'incapsula incident id')):
            raise self._error('網站要求驗證，已停止該來源並保留舊資料。', kind='blocked', stop_source=True)
        return response

    def get(self, url: str) -> str:
        return self.response(url).text

    def get_bytes(self, url: str) -> bytes:
        return self.response(url).content

    def search_json(self, url, data, headers=None):
        return self.response(url, data=data, headers=headers).json()


def reuse_detail(client, job):
    # Fixture clients and read-only parser tests intentionally need no cache API.
    return client.reuse_detail(job) if isinstance(client, PublicClient) else False


def detail_jobs(client, jobs):
    """Fill every valid cache first, then spend requests on changed/new ads.

    A challenge on the first network read must not invalidate unrelated valid
    cached details later in the list. Cached bodies retain their old timestamp.
    """
    if not isinstance(client, PublicClient):
        return jobs
    pending = [job for job in jobs if not job.get('detail_complete') and not client.reuse_detail(job)]
    def priority(job):
        old = client.previous_jobs.get(job['id'], {})
        if not old:
            return (0, '')
        if old.get('listing_fingerprint') != job.get('listing_fingerprint'):
            return (1, '')
        if old.get('detail_refresh_failed') or not old.get('detail_complete'):
            return (2, '')
        return (3, old.get('detail_verified_at') or '')
    queue = sorted(pending, key=priority)
    if client.detail_budget is not None:
        # A site that limits request volume gets a fixed allowance per run
        # (across every call): new and changed ads first, then the oldest
        # verified bodies. The rest keep their saved text for the next round.
        remaining = max(0, client.detail_budget - client.detail_queued)
        if len(queue) > remaining:
            client.detail_deferred += len(queue) - remaining
            queue = queue[:remaining]
        client.detail_queued += len(queue)
    return queue


def mark_detail_success(client, job):
    if isinstance(client, PublicClient):
        client.mark_detail_success(job)


def mark_detail_failure(client, job):
    if isinstance(client, PublicClient):
        client.mark_detail_failure(job)
