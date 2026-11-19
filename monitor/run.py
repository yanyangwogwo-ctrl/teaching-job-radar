from __future__ import annotations

import argparse
import json
import os
import copy
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .adapters import Batch, collect
from .http import PublicClient, CrawlError, EXTERNAL_KINDS
from .notify import deliver
from .store import load_store, atomic_json, reconcile, export, expire_jobs, schedule_execution, fresh_store
from .rules import evaluate

# A source that fails on the program side of the classification is read once
# more at the end of the round. A list caught mid-update, or a detail page
# that errors for a minute, then clears in the same run instead of raising an
# alert today and a recovery notice tomorrow. A site that asked for a slower
# pace (deferred details) or a browser check (blocked) is left alone.
RETRY_GAP_SECONDS = 180
# A retry must be expected to finish this long after the round began, which
# leaves the 45-minute workflow time to save, notify and commit.
RETRY_BUDGET_SECONDS = 30 * 60
NO_RETRY_KINDS = frozenset({'blocked', 'deferred'})
SEVERITY_RANK = {'ok': 0, 'warning': 1, 'error': 2}

# Indirection so tests can run the retry path without waiting.
clock = time.monotonic
sleep = time.sleep


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


def external_only(errors, failures) -> bool:
    """True when every error traces back to a site-side failure of this client.

    Adapters often catch a CrawlError and keep only its text, so the client's
    own record of (message, kind) is the evidence. Any message that cannot be
    traced (a parser or layout error, an adapter's own diagnosis) keeps the
    conservative default: it needs a person to look at it.
    """
    if not errors:
        return False
    known = [(message, kind) for message, kind in failures if message]
    for error in errors:
        kinds = {kind for message, kind in known if message == error or message in error}
        if not kinds or not kinds <= set(EXTERNAL_KINDS):
            return False
    return True


def client_failures(client) -> list:
    failures = getattr(client, 'failures', [])
    return list(failures) if isinstance(failures, list) else []


def fetch_source(source: dict, cached_jobs=(), now=None) -> Batch:
    client = PublicClient(source)
    client.set_previous_jobs(cached_jobs, now)
    raised_kind = None
    try:
        batch = collect(source, client)
    except CrawlError as error:
        raised_kind = getattr(error, 'kind', None)
        if source['adapter'] == 'hkust' and not error.stop_source and cached_jobs:
            from .peoplesoft import collect_known_hkust
            try:
                batch = collect_known_hkust(source, client, cached_jobs, str(error))
            except Exception as fallback_error:
                batch = Batch(complete=False, listing_complete=False, severity='error',
                              errors=[str(error), f'已知職位補充讀取未完成（{type(fallback_error).__name__}）。'])
        else:
            capability = source.get('capability', 'pending' if source['adapter'] == 'availability_check' else 'full')
            # An intentionally manual/unimplemented source is a visible known
            # limitation, and so is a site that blocked or dropped this round.
            # Failures of implemented parsing stay errors.
            outside = getattr(error, 'kind', None) in EXTERNAL_KINDS or external_only([str(error)], client_failures(client))
            severity = 'warning' if capability in ('manual', 'pending') or outside else 'error'
            batch = Batch(complete=False, listing_complete=False, capability=capability,
                          severity=severity, errors=[str(error)])
    except Exception as error:
        # Parser failure is explicitly visible; never convert it to success/zero.
        batch = Batch(complete=False, listing_complete=False, severity='error',
                      errors=[f'讀取模組未完成（{type(error).__name__}），需要檢查。'])
    deferred = getattr(client, 'detail_deferred', 0)
    deferred = deferred if isinstance(deferred, int) else 0
    failures = client_failures(client)
    if deferred:
        message = (f'詳情分批更新：網站限制讀取頻率，本輪最多讀取 {client.detail_budget} 則詳情，'
                   f'另有 {deferred} 則保留上次內容，留待下一輪更新。')
        failures.append((message, 'deferred'))
        batch.errors.append(message)
        batch.complete = False
    batch.external_failure = external_only(batch.errors, failures)
    kinds = {kind for _, kind in failures if kind}
    if raised_kind:
        kinds.add(raised_kind)
    batch.failure_kinds = sorted(kinds)
    batch.metrics.update(request_count=client.count,
                         detail_cache_hits=client.detail_cache_hits,
                         detail_fetches=client.detail_fetches)
    if deferred:
        batch.metrics['detail_deferred'] = deferred
    return batch


def preview(state: dict, source: dict, batch: Batch, now: str, prefs: dict) -> dict:
    """The summary reconcile() would give, worked out on a copy of this
    source's own records. Nothing in `state` or `batch` changes."""
    shadow = fresh_store()
    shadow['jobs'] = {key: copy.deepcopy(job) for key, job in state['jobs'].items()
                      if job.get('source_id') == source['id']}
    if source['id'] in state['sources']:
        shadow['sources'][source['id']] = copy.deepcopy(state['sources'][source['id']])
    return reconcile(shadow, source, copy.deepcopy(batch), now, prefs)


def retry_reason(batch: Batch, outlook: dict) -> str | None:
    """Why this attempt deserves one more read this round, or None."""
    if outlook['capability'] in ('manual', 'pending'):
        return None
    kinds = set(getattr(batch, 'failure_kinds', None) or [])
    if kinds & NO_RETRY_KINDS:
        return None
    if outlook['severity'] == 'error':
        return 'error'
    # Nothing could be read because connections failed; a later attempt may
    # get through. A refusal or browser check is 'blocked' and stops above.
    if outlook['status'] == 'unreachable' and 'network' in kinds:
        return 'network'
    return None


def attempt_rank(outlook: dict) -> tuple:
    return (SEVERITY_RANK.get(outlook['severity'], 2), not outlook['listing_complete'],
            -outlook['detail_complete_count'], -outlook['count'])


def retry_cache(cached_jobs, first: Batch) -> list:
    """Details the first attempt read in full count as fresh for the second,
    so the retry asks the site only for what is still missing."""
    merged = {job['id']: job for job in cached_jobs if job.get('id')}
    for job in first.jobs:
        if job.get('id') and job.get('detail_complete') and job.get('description'):
            merged[job['id']] = copy.deepcopy(job)
    return list(merged.values())


def retry_when_due(source: dict, cached_jobs, first: Batch, due: float, deadline: float,
                   duration: float, now: str) -> Batch | None:
    """Wait out the gap, then read the source once more. None when the
    attempt would not be expected to finish within the round's budget."""
    start = max(due, clock())
    if start + duration > deadline:
        return None
    pause = start - clock()
    if pause > 0:
        sleep(pause)
    return fetch_source(source, retry_cache(cached_jobs, first), now)


def choose_attempt(first: Batch, first_view: dict, second: Batch, second_view: dict) -> Batch:
    """Keep whichever attempt read more and say on the status page what happened."""
    if attempt_rank(second_view) <= attempt_rank(first_view):
        chosen = second
        if second_view['severity'] == 'ok':
            note = '第一次讀取未完成；本輪尾段自動重試，已完整讀取。'
        elif second_view['severity'] == 'warning':
            note = '第一次讀取未完成；本輪尾段自動重試後有改善，但仍有部分未完成。'
        else:
            note = '第一次讀取未完成；本輪尾段已自動重試一次，仍未完成。'
        if first.errors:
            note += f'第一次的錯誤：{first.errors[0][:140]}'
    else:
        chosen = first
        note = '本輪尾段已自動重試一次，結果未有改善，以下為第一次的結果。'
    chosen.notes.append(note)
    chosen.metrics['retry_attempts'] = 1
    chosen.metrics['first_attempt_request_count'] = first.metrics.get('request_count', 0)
    return chosen


def crawl(state: dict, selected: list[dict], now: str, prefs: dict, save) -> list[dict]:
    """Read every selected source, then once more any that failed on the
    program side. Each source is reconciled exactly once, with the attempt
    that read more, so a failure the retry clears raises no alert."""
    cached = {s['id']: copy.deepcopy([job for job in state['jobs'].values() if job['source_id'] == s['id']])
              for s in selected}
    summaries = []
    deadline = clock() + RETRY_BUDGET_SECONDS

    def settle(source, batch):
        summary = reconcile(state, source, batch, now, prefs)
        summaries.append(summary)
        save()
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    def timed_fetch(source):
        began = clock()
        batch = fetch_source(source, cached[source['id']], now)
        return batch, clock() - began

    waiting = []
    # Sources run in parallel; each site's requests remain paced/serial.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(timed_fetch, s): s for s in selected}
        for future in as_completed(futures):
            source = futures[future]
            batch, duration = future.result()
            view = preview(state, source, batch, now, prefs)
            reason = retry_reason(batch, view)
            if not reason:
                settle(source, batch)
                continue
            waiting.append((clock() + RETRY_GAP_SECONDS, source, batch, view, duration))
            print(json.dumps({'source_id': source['id'], 'retry': reason, 'first_errors': batch.errors[:3]},
                             ensure_ascii=False), flush=True)
    if waiting:
        waiting.sort(key=lambda item: item[0])
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(retry_when_due, source, cached[source['id']], batch, due, deadline, duration, now):
                       (source, batch, view) for due, source, batch, view, duration in waiting}
            for future in as_completed(futures):
                source, first, first_view = futures[future]
                second = future.result()
                if second is None:
                    first.notes.append('本輪時間不足，未有自動重試。')
                    settle(source, first)
                else:
                    settle(source, choose_attempt(first, first_view, second, preview(state, source, second, now, prefs)))
    return summaries


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
        summaries = crawl(state, selected, now, prefs, lambda: atomic_json(path, state))
        problems = any(summary['severity'] == 'error' for summary in summaries)
        warnings = any(summary['severity'] == 'warning' for summary in summaries)
        state['runs'].append({'started_at': now, 'finished_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                              'sources': summaries, 'status': 'partial' if problems else ('degraded' if warnings else 'ok'),
                              'schedule_execution': schedule_execution()})
        atomic_json(path, state)
    for job in state['jobs'].values():
        job.update(evaluate(job, prefs))
        # Wait for a body that is only temporarily missing. A source that never
        # publishes one (CityU, HKUST Interfolio) is judged on its list row.
        if not job.get('detail_complete') and not job.get('description') and not job.get('detail_unavailable'):
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
