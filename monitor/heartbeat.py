"""Optional external missed-run monitor. No service is created automatically."""
import os
import sys
from urllib.parse import urlsplit

import requests


def main():
    url = os.environ.get('HEARTBEAT_URL', '').strip()
    if not url:
        print('獨立漏跑監察尚未設定。')
        return 0
    bits = urlsplit(url)
    if bits.scheme != 'https' or bits.hostname not in ('hc-ping.com', 'healthchecks.io') or bits.username or bits.password or bits.query or bits.fragment:
        print('漏跑監察網址格式未能確認。', file=sys.stderr)
        return 1
    try:
        response = requests.get(url, timeout=(10, 20), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('heartbeat unconfirmed')
        print('已回報本輪排程有執行；各來源故障另由 Discord 提示。')
        return 0
    except (requests.RequestException, ValueError):
        print('漏跑監察未確認接收。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
