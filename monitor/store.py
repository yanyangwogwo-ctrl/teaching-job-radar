"""One atomic canonical snapshot; CSV and public JSON are derived exports.

The scheduler commits data/store.json to its repository between runs. No runner
cache, browser localStorage, or expiring Actions artifact is a database.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .adapters import Batch
from .model import content_hash
from .rules import evaluate


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


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
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
    last_count = previous.get('last_complete_count', 0)
    unique = {job['id']: job for job in batch.jobs}
    if not unique and not batch.explicit_empty:
        batch.complete = False
        batch.errors.append('未能確認零職位結果。')
    if last_count >= 5 and len(unique) < last_count * (1 - prefs['suspicious_drop_ratio']) and not batch.explicit_empty:
        batch.complete = False
        batch.errors.append('職位數量大幅減少，暫不判定舊職位消失。')
    today = datetime.fromisoformat(now).astimezone(ZoneInfo(prefs['timezone'])).date().isoformat()
    existing_source_jobs = any(job['source_id'] == source['id'] for job in state['jobs'].values())
    baseline = not (previous.get('baseline_started') or previous.get('baseline_completed') or existing_source_jobs)
    new_count = 0
    for key, incoming in unique.items():
        old = state['jobs'].get(key)
        job = dict(incoming)
        if old and not job.get('detail_complete'):
            for field in ('title', 'description', 'match_text', 'employment_type'):
                job[field] = old.get(field, job.get(field))
            if not job.get('department'):
                job['department'] = old.get('department', '')
            if not job.get('posted_date'):
                job['posted_date'] = old.get('posted_date')
            if job.get('deadline_type') in ('unknown', 'screening-or-closing'):
                for field in ('deadline', 'deadline_type', 'deadline_raw'):
                    job[field] = old.get(field)
        job.update(evaluate(job, prefs))
        if not incoming.get('detail_complete') and not old:
            job['matches'] = False
        job['content_hash'] = content_hash(job)
        job['first_seen'] = old['first_seen'] if old else now
        job['last_seen'] = now
        job['missing_count'] = 0
        job['status'] = 'closed' if job.get('deadline_type') == 'closing' and job.get('deadline') and job['deadline'] < today else 'open'
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
        if job['eligible_new_notification'] and job['matches'] and job['detail_complete'] and job['status'] == 'open' and state['outbox'].get(event_id, {}).get('state') not in ('pending', 'sent'):
            state['outbox'][event_id] = {'id': event_id, 'kind': 'new', 'job_id': key, 'created_at': now, 'state': 'pending'}
    if batch.complete:
        for key, old in list(state['jobs'].items()):
            if old['source_id'] != source['id'] or key in unique:
                continue
            before = dict(old)
            old['missing_count'] = old.get('missing_count', 0) + 1
            if old.get('deadline_type') == 'closing' and old.get('deadline') and old['deadline'] < today:
                old['status'] = 'closed'
            elif old['missing_count'] >= prefs['missing_after_successes']:
                old['status'] = 'missing'
            if old['status'] != before['status']:
                old['changed_at'] = now
                add_history(state, old, 'status', now, before)
    health = {**previous, 'id': source['id'], 'last_attempt': now, 'status': 'ok' if batch.complete else ('partial' if unique else 'error'),
              'count': len(unique), 'pages': batch.pages, 'errors': list(dict.fromkeys(batch.errors))[:8],
              'baseline_started': bool(unique) or existing_source_jobs or previous.get('baseline_started', False)}
    if batch.complete:
        health.update(last_success=now, last_complete_count=len(unique), baseline_completed=True)
    state['sources'][source['id']] = health
    if not batch.complete:
        fingerprint = hashlib.sha256('|'.join(health['errors']).encode()).hexdigest()[:10]
        event_id = f"health:{source['id']}:{today}:{fingerprint}"
        state['outbox'].setdefault(event_id, {'id': event_id, 'kind': 'health', 'source_id': source['id'], 'created_at': now, 'state': 'pending', 'errors': health['errors']})
    return {'source_id': source['id'], 'status': health['status'], 'count': len(unique), 'new_records': new_count, 'baseline': baseline}


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
        if not source['enabled']:
            entry['status'] = 'pending'
        health.append(entry)
    metadata = {
        'generated_at': now, 'schema_version': 1, 'data_kind': 'official-live-crawl',
        'timezone': prefs['timezone'], 'schedule': prefs['schedule_description'],
        'automation_active': os.environ.get('GITHUB_ACTIONS') == 'true',
        'discord_configured': bool(os.environ.get('DISCORD_WEBHOOK_URL')),
        'heartbeat_configured': bool(os.environ.get('HEARTBEAT_URL')),
        'notifications': state.get('notifications', {}),
        'stale_after_hours': prefs['stale_after_hours'],
        'notification_rule': prefs['notifications'], 'subjects': prefs['subjects'],
        'last_run': state['runs'][-1] if state['runs'] else None,
    }
    # Internal hashes/outbox delivery data are not needed by the public UI.
    public_jobs = [{k: v for k, v in job.items() if k not in ('content_hash', 'match_text', 'eligible_new_notification')} for job in jobs]
    atomic_json(data_dir / 'jobs.json', {'meta': metadata, 'sources': health, 'jobs': public_jobs})
    atomic_json(data_dir / 'history.json', state['history'])
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
