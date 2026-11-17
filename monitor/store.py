"""One atomic canonical snapshot; CSV and public JSON are derived exports.

The scheduler commits data/store.json to its repository between runs. No runner
cache, browser localStorage, or expiring Actions artifact is a database.
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters import Batch
from .model import content_hash
from .rules import evaluate
from .schedule import schedule_description
from .notify import queue_health_event


def fresh_store() -> dict:
    return {'version': 1, 'jobs': {}, 'sources': {}, 'history': [], 'outbox': {}, 'runs': [], 'notifications': {}}


def load_store(path: Path) -> dict:
    if not path.exists():
        return fresh_store()
    with path.open(encoding='utf-8') as handle:
        state = json.load(handle)
    if state.get('version') != 1 or not isinstance(state.get('jobs'), dict):
        raise ValueError('資料格式不符合預期；未覆蓋原有資料。')
    return state


def atomic_json(path: Path, value, *, compact=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=None if compact else 2,
                      separators=(',', ':') if compact else None, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def add_history(state: dict, job: dict, kind: str, now: str, before: dict | None = None):
    item = {'job_id': job['id'], 'kind': kind, 'at': now, 'title': job['title'], 'source_id': job['source_id']}
    if before:
        fields = ['title', 'department', 'employment_type', 'posted_date', 'deadline', 'deadline_type', 'description', 'status']
        item['changes'] = {key: {'before': before.get(key), 'after': job.get(key)} for key in fields if before.get(key) != job.get(key)}
    state['history'].append(item)


def reconcile(state: dict, source: dict, batch: Batch, now: str, prefs: dict) -> dict:
    previous = state['sources'].get(source['id'], {})
    last_count = previous.get('last_listing_count', previous.get('last_complete_count', 0))
    listing_complete = batch.complete if batch.listing_complete is None else batch.listing_complete
    capability = batch.capability or source.get('capability', 'pending' if source.get('adapter') == 'availability_check' else 'full')
    guarded_listing = False
    # Every error came from the site side (challenge, refusal, timeout, request
    # budget). Old records are kept either way; this only decides whether a
    # person needs to change the program.
    external = bool(getattr(batch, 'external_failure', False))
    unique = {job['id']: job for job in batch.jobs}
    if not unique and not batch.explicit_empty:
        batch.complete = False
        listing_complete = False
        if capability not in ('manual', 'pending'):
            guarded_listing = not external
            batch.errors.append('未能確認零職位結果。')
    # A shrinking list only matters when it claims to be complete; an
    # unfinished list already blocks retirement and explains its own size.
    if listing_complete and last_count >= 5 and len(unique) < last_count * (1 - prefs['suspicious_drop_ratio']) and not batch.explicit_empty:
        batch.complete = False
        listing_complete = False
        guarded_listing = True
        batch.errors.append('職位數量大幅減少，暫不判定舊職位消失。')
    # Completeness can never be promoted by a configured capability or by the
    # presence of cached bodies. It describes this round's listing evidence.
    if not listing_complete:
        batch.complete = False
    batch.listing_complete = bool(listing_complete)
    today = datetime.fromisoformat(now).astimezone(ZoneInfo(prefs['timezone'])).date().isoformat()
    existing_source_jobs = any(job['source_id'] == source['id'] for job in state['jobs'].values())
    baseline = not (previous.get('baseline_started') or previous.get('baseline_completed') or existing_source_jobs)
    new_count = 0
    for key, incoming in unique.items():
        old = state['jobs'].get(key)
        job = dict(incoming)
        if old and not job.get('detail_complete'):
            # Only a verified live advertisement may undo an explicit closure.
            job['source_closed'] = bool(job.get('source_closed') or old.get('source_closed'))
            for field in ('title', 'description', 'match_text', 'employment_type'):
                job[field] = old.get(field, job.get(field))
            if not job.get('department'):
                job['department'] = old.get('department', '')
            if not job.get('posted_date'):
                job['posted_date'] = old.get('posted_date')
            if job.get('deadline_type') in ('unknown', 'screening-or-closing'):
                for field in ('deadline', 'deadline_type', 'deadline_raw'):
                    job[field] = old.get(field)
            for field in ('detail_verified_at', 'listing_fingerprint'):
                if not job.get(field) and old.get(field):
                    job[field] = old[field]
            # Preserve successful verification age while making an attempted
            # failed refresh retryable; an old body is not a fresh success.
            job['detail_refresh_failed'] = bool(job.get('detail_refresh_failed') or old.get('detail_refresh_failed'))
        job.update(evaluate(job, prefs))
        if not incoming.get('detail_complete') and not old and not incoming.get('detail_unavailable'):
            job['matches'] = False
        job['content_hash'] = content_hash(job)
        job['first_seen'] = old['first_seen'] if old else now
        job['last_seen'] = now
        job['missing_count'] = 0
        dated_closed = job.get('deadline_type') == 'closing' and job.get('deadline') and job['deadline'] < today
        job['status'] = 'closed' if job.get('source_closed') or dated_closed else 'open'
        job['eligible_new_notification'] = old.get('eligible_new_notification', False) if old else not baseline
        job['changed_at'] = old.get('changed_at', now) if old else now
        if not old:
            new_count += 1
            add_history(state, job, 'baseline' if baseline else 'new', now)
        elif old.get('content_hash') != job['content_hash'] or old.get('status') != job['status']:
            job['changed_at'] = now
            add_history(state, job, 'updated', now, old)
        state['jobs'][key] = job
        event_id = 'new:' + key
        readable = job['detail_complete'] or job.get('detail_unavailable')
        if job['eligible_new_notification'] and job['matches'] and readable and job['status'] == 'open' and state['outbox'].get(event_id, {}).get('state') not in ('pending', 'sent'):
            state['outbox'][event_id] = {'id': event_id, 'kind': 'new', 'job_id': key, 'created_at': now, 'state': 'pending'}
    if listing_complete:
        for key, old in list(state['jobs'].items()):
            if old['source_id'] != source['id'] or key in unique:
                continue
            before = dict(old)
            old['missing_count'] = old.get('missing_count', 0) + 1
            if old.get('source_closed') or (old.get('deadline_type') == 'closing' and old.get('deadline') and old['deadline'] < today):
                old['status'] = 'closed'
            elif old['missing_count'] >= prefs['missing_after_successes']:
                old['status'] = 'missing'
            if old['status'] != before['status']:
                old['changed_at'] = now
                add_history(state, old, 'status', now, before)
    detail_count = sum(bool(job.get('detail_complete')) for job in unique.values())
    if guarded_listing or (not listing_complete and capability not in ('manual', 'pending') and not external):
        severity = 'error'
    elif batch.severity == 'error':
        severity = 'error'
    elif external and not batch.complete:
        severity = 'warning'
    elif batch.severity in ('ok', 'warning'):
        severity = batch.severity
    elif batch.complete:
        severity = 'ok'
    elif capability in ('manual', 'pending') or (listing_complete and capability in ('list_only', 'partial')):
        severity = 'warning'
    else:
        severity = 'error'
    if severity == 'ok' and not batch.complete:
        severity = 'warning' if listing_complete else 'error'
    status = 'ok' if batch.complete else ('partial' if unique or listing_complete else 'error')
    if severity == 'warning' and capability in ('manual', 'pending'):
        status = capability
    elif severity == 'warning' and external and not listing_complete and not unique:
        # Nothing could be read this round because of the site; distinct from
        # a program failure, which stays 'error'.
        status = 'unreachable'
    health = {**previous, 'id': source['id'], 'last_attempt': now, 'status': status,
              'count': len(unique), 'pages': batch.pages, 'errors': list(dict.fromkeys(batch.errors))[:8],
              'listing_complete': bool(listing_complete), 'capability': capability, 'severity': severity,
              'detail_complete_count': detail_count, 'detail_total_count': len(unique),
              'detail_coverage': round(detail_count / len(unique), 4) if unique else (1.0 if batch.explicit_empty and batch.complete else None),
              'metrics': dict(batch.metrics),
              'run_notes': list(dict.fromkeys(getattr(batch, 'notes', []) or []))[:4],
              'external_failure': external,
              'baseline_started': bool(unique) or existing_source_jobs or previous.get('baseline_started', False)}
    if listing_complete:
        health.update(last_listing_success=now, last_listing_count=len(unique))
    if batch.complete:
        health.update(last_success=now, last_complete_count=len(unique), baseline_completed=True)
    state['sources'][source['id']] = health
    queue_health_event(state, source, health, previous, now, timezone=prefs['timezone'])
    return {'source_id': source['id'], 'status': health['status'], 'severity': severity,
            'capability': capability, 'listing_complete': bool(listing_complete),
            'detail_complete_count': detail_count, 'detail_total_count': len(unique),
            'detail_coverage': health['detail_coverage'], 'metrics': dict(batch.metrics),
            'count': len(unique), 'new_records': new_count, 'baseline': baseline}


def expire_jobs(state, now, prefs):
    """A trusted closing date still applies when today's source is unreachable."""
    today = datetime.fromisoformat(now).astimezone(ZoneInfo(prefs['timezone'])).date().isoformat()
    for job in state['jobs'].values():
        if job.get('deadline_type') == 'closing' and job.get('deadline') and job['deadline'] < today and job['status'] != 'closed':
            before = dict(job)
            job.update(status='closed', changed_at=now)
            add_history(state, job, 'status', now, before)


CSV_FIELDS = ['institution', 'title', 'department', 'employment_type', 'posted_date', 'deadline', 'deadline_type', 'deadline_raw',
              'url', 'reference', 'subjects', 'score', 'matches', 'first_seen', 'last_seen', 'status', 'description']

PUBLIC_FEED_DAYS = 30


def public_job_date(job: dict, timezone: str):
    """Mirror the UI's advertised-date-first policy, never a refresh timestamp."""
    posted = job.get('posted_date')
    if posted:
        try:
            return datetime.fromisoformat(posted).date()
        except (TypeError, ValueError):
            return None
    try:
        return datetime.fromisoformat(job['first_seen']).astimezone(ZoneInfo(timezone)).date()
    except (KeyError, TypeError, ValueError):
        return None


def schedule_execution():
    started = os.environ.get('RADAR_SCHEDULE_STARTED_AT')
    expected = os.environ.get('RADAR_SCHEDULE_EXPECTED_AT')
    if not started or not expected:
        return None
    try:
        delay = max(0, int(float(os.environ['RADAR_SCHEDULE_DELAY_SECONDS'])))
    except (KeyError, TypeError, ValueError):
        return None
    return {'expected_at': expected, 'started_at': started, 'delay_seconds': delay,
            'timing_basis': os.environ.get('RADAR_SCHEDULE_TIMING_BASIS', 'unknown'),
            'trigger': os.environ.get('RADAR_SCHEDULE_TRIGGER', 'github-schedule')}


def safe_cell(value) -> str:
    value = str(value if value is not None else '')
    # Prevent an advertised title/description from becoming an Excel formula.
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value


def export(state: dict, sources: list[dict], prefs: dict, root: Path, now: str):
    jobs = list(state['jobs'].values())
    data_dir = root / 'dist' / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    health = []
    for source in sources:
        entry = {k: source[k] for k in ('id', 'institution', 'name', 'url', 'enabled', 'notes')}
        entry.update(state['sources'].get(source['id'], {'status': 'pending' if not source['enabled'] else 'unverified', 'count': 0, 'errors': []}))
        entry.setdefault('capability', source.get('capability', 'pending' if source.get('adapter') == 'availability_check' else 'full'))
        # Older snapshots have no independent listing proof. Do not infer it
        # from a nonzero result count or from the configured source capability.
        entry.setdefault('listing_complete', entry['status'] == 'ok')
        entry.setdefault('severity', 'ok' if entry['status'] == 'ok' else ('warning' if entry['capability'] in ('manual', 'pending') else 'error'))
        if not source['enabled']:
            entry['status'] = 'pending'
        health.append(entry)
    metadata = {
        'generated_at': now, 'schema_version': 1, 'data_kind': 'official-live-crawl',
        'timezone': prefs['timezone'], 'schedule': schedule_description(root),
        'automation_active': os.environ.get('GITHUB_ACTIONS') == 'true',
        'discord_configured': bool(os.environ.get('DISCORD_WEBHOOK_URL')),
        'heartbeat_configured': bool(os.environ.get('HEARTBEAT_URL')),
        'notifications': state.get('notifications', {}),
        'stale_after_hours': prefs['stale_after_hours'],
        'notification_rule': prefs['notifications'], 'subjects': prefs['subjects'],
        'last_run': state['runs'][-1] if state['runs'] else None,
        'schedule_execution': schedule_execution() or (state['runs'][-1].get('schedule_execution') if state['runs'] else None),
        'total_jobs': len(jobs),
    }
    # Internal hashes/outbox delivery data are not needed by the public UI.
    private_fields = ('content_hash', 'match_text', 'eligible_new_notification', 'listing_fingerprint')
    public_jobs = [{k: v for k, v in job.items() if k not in private_fields} for job in jobs]
    today = datetime.fromisoformat(now).astimezone(ZoneInfo(prefs['timezone'])).date()
    cutoff = today - timedelta(days=PUBLIC_FEED_DAYS - 1)
    recent_jobs = [job for job in public_jobs if (public_job_date(job, prefs['timezone']) or cutoff) >= cutoff]
    archive = {'url': 'jobs-archive.json', 'count': len(public_jobs), 'generation_id': now, 'generated_at': now}
    # Publish the archive first. A reader that crosses publication generations
    # checks generated_at and retries instead of silently mixing snapshots.
    atomic_json(data_dir / 'jobs-archive.json', {
        'meta': {**metadata, 'feed_scope': 'full', 'generation_id': now},
        'sources': health, 'jobs': public_jobs}, compact=True)
    atomic_json(data_dir / 'jobs.json', {
        'meta': {**metadata, 'feed_scope': 'recent', 'feed_days': PUBLIC_FEED_DAYS,
                 'generation_id': now, 'archive': archive},
        'sources': health, 'jobs': recent_jobs}, compact=True)
    atomic_json(data_dir / 'history.json', state['history'], compact=True)
    csv_path = data_dir / 'jobs.csv'
    descriptor, name = tempfile.mkstemp(prefix='jobs.', dir=data_dir)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for job in sorted(jobs, key=lambda j: (j['institution'], j['id'])):
                row = {field: job.get(field, '') for field in CSV_FIELDS}
                row['subjects'] = ', '.join(s['label'] for s in job.get('subjects', []))
                writer.writerow({k: safe_cell(v) for k, v in row.items()})
        os.replace(name, csv_path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
