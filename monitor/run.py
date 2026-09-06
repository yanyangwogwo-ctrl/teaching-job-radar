from __future__ import annotations

import argparse
import json
import os
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .adapters import Batch, collect
from .http import PublicClient, CrawlError
from .notify import deliver
from .store import load_store, atomic_json, reconcile, export, expire_jobs
from .rules import evaluate


def read_config(root: Path):
    with (root / 'config/sites.yaml').open(encoding='utf-8') as handle:
        source_config = yaml.safe_load(handle)
    with (root / 'config/preferences.yaml').open(encoding='utf-8') as handle:
        prefs = yaml.safe_load(handle)
    sources = source_config['sources']
    if len({s['id'] for s in sources}) != len(sources):
        raise ValueError('來源 ID 不可重複。')
    for source in sources:
        if not all(source.get(k) for k in ('id', 'group', 'url', 'adapter', 'allowed_hosts')):
            raise ValueError('來源設定不完整。')
    return sources, prefs


def fetch_source(source: dict, cached_jobs=()) -> Batch:
    client = PublicClient(source)
    try:
        return collect(source, client)
    except CrawlError as error:
        if source['adapter'] == 'hkust' and not error.stop_source and cached_jobs:
            from .peoplesoft import collect_known_hkust
            return collect_known_hkust(source, client, cached_jobs, str(error))
        return Batch(complete=False, errors=[str(error)])
    except Exception as error:
        # Parser failure is explicitly visible; never convert it to success/zero.
        return Batch(complete=False, errors=[f'讀取模組未完成（{type(error).__name__}），需要檢查。'])


def main():
    parser = argparse.ArgumentParser(description='香港大專官方職位監察')
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--sources', nargs='*', help='限定來源 ID；未選到的來源不改動')
    parser.add_argument('--notify-only', action='store_true', help='發送已持久化的待送紀錄，不重新爬取')
    parser.add_argument('--export-only', action='store_true')
    parser.add_argument('--notify', action='store_true', help='明確啟用外部 Discord 發送；預設不發送')
    args = parser.parse_args()
    sources, prefs = read_config(args.root)
    if args.sources and set(args.sources) - {s['id'] for s in sources}:
        parser.error('包含不存在的來源 ID。')
    path = args.root / 'data' / 'store.json'
    state = load_store(path)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    problems = False
    if not args.notify_only and not args.export_only:
        selected = [s for s in sources if s['enabled'] and (not args.sources or s['id'] in args.sources)]
        if not selected:
            parser.error('沒有啟用的來源，未執行檢索。')
        summaries = []
        cached_hkust = copy.deepcopy([job for job in state['jobs'].values() if job['source_id'] == 'hkust'])
        # Sources run in parallel; each site's requests remain paced/serial.
        with ThreadPoolExecutor(max_workers=3) as pool:
            future_map = {pool.submit(fetch_source, s, cached_hkust if s['adapter'] == 'hkust' else ()): s for s in selected}
            for future in as_completed(future_map):
                source = future_map[future]
                batch = future.result()
                summary = reconcile(state, source, batch, now, prefs)
                summaries.append(summary)
                problems |= summary['status'] != 'ok'
                atomic_json(path, state)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
        state['runs'].append({'started_at': now, 'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'sources': summaries, 'status': 'partial' if problems else 'ok'})
        atomic_json(path, state)
    for job in state['jobs'].values():
        job.update(evaluate(job, prefs))
        if not job.get('detail_complete') and not job.get('description'):
            job['matches'] = False
    expire_jobs(state, now, prefs)
    atomic_json(path, state)
    if args.notify or args.notify_only:
        if prefs['notifications']['enabled']:
            sent = deliver(state, now, lambda: atomic_json(path, state))
            print(json.dumps({'notifications_sent': sent, 'notification_status': state['notifications'].get('status', 'no-pending')}), flush=True)
            problems |= state['notifications'].get('status') in ('error', 'rate-limited')
    export(state, sources, prefs, args.root, now)
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
