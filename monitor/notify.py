from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests



def safe_link(url: str) -> str:
    parts = urlsplit(url)
    return url.replace(')', '%29').replace('(', '%28').replace(' ', '%20') if parts.scheme == 'https' and parts.hostname else ''


def plain(value: str, limit=240) -> str:
    return re.sub(r'([\\*_`~>|\[\]])', r'\\\1', str(value))[:limit]


def _health_signature(health: dict) -> str:
    # Counts and timestamps change routinely and are not a new outage.
    values = {key: health.get(key) for key in ('status', 'severity', 'capability')}
    values['errors'] = sorted(set(health.get('errors', [])))
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def queue_health_event(state: dict, source: dict, health: dict, previous: dict,
                       now: str, timezone='Asia/Hong_Kong'):
    """Record health transitions; daily reminders are composed at delivery time.

    Keep events in the same durable outbox as vacancy notifications. The first
    healthy baseline needs no message, but a return to full coverage does.
    """
    signature = _health_signature(health)
    old_signature = previous.get('health_signature') or (_health_signature(previous) if previous else None)
    health['health_signature'] = signature
    health['institution'] = source.get('institution', source['id'])
    if signature == old_signature or (not previous and health.get('status') == 'ok'):
        return
    event_id = f"health:{source['id']}:{now}:{signature}"
    state['outbox'].setdefault(event_id, {
        'id': event_id, 'kind': 'health', 'source_id': source['id'],
        'institution': health['institution'], 'created_at': now, 'state': 'pending',
        'status': health.get('status'), 'severity': health.get('severity'),
        'capability': health.get('capability', 'full'), 'signature': signature,
        'change': 'recovered' if health.get('status') == 'ok' else ('changed' if previous else 'started'),
        'errors': health.get('errors', []), 'timezone': timezone,
    })


def _health_row(source_id: str, health: dict, event: dict | None = None) -> dict:
    event = event or {}
    return {'source_id': source_id,
            'institution': health.get('institution') or event.get('institution') or source_id,
            'status': health.get('status') or event.get('status') or 'error',
            'severity': health.get('severity') or event.get('severity'),
            'capability': health.get('capability') or event.get('capability') or 'full',
            'errors': health.get('errors') or event.get('errors', [])}


def prepare_health_summary(state: dict, now: str) -> bool:
    """Coalesce legacy and new health events without dropping failed sends.

    There is at most one acknowledged digest per Hong Kong calendar day.
    Further messages that day require an actual new source-state transition.
    Pending summaries are refreshed from current state so recovery never sends
    an obsolete outage; individual event acknowledgements share the same save.
    """
    outbox = state['outbox']
    newest = {}
    changed = False
    for event in list(outbox.values()):
        if event['kind'] == 'health' and event['state'] == 'pending':
            key = event['source_id']
            if key not in newest or (event['created_at'], event['id']) > (newest[key]['created_at'], newest[key]['id']):
                newest[key] = event
    transitions = []
    for event in list(outbox.values()):
        if event['kind'] != 'health' or event['state'] != 'pending':
            continue
        source_id = event['source_id']
        if newest[source_id]['id'] != event['id']:
            event.update(state='superseded', completed_at=now)
            changed = True
        elif state['sources'].get(source_id, {}).get('status') == 'ok' and event.get('change') != 'recovered':
            # Old outbox versions did not have recovery events.
            event.update(state='resolved', completed_at=now)
            changed = True
        else:
            transitions.append(event)
    timezone = state.get('notifications', {}).get('timezone', 'Asia/Hong_Kong')
    today = datetime.fromisoformat(now).astimezone(ZoneInfo(timezone)).date().isoformat()
    daily_id = 'health-digest:' + today
    daily_sent = outbox.get(daily_id, {}).get('state') == 'sent'
    problems = [_health_row(source_id, health, newest.get(source_id))
                for source_id, health in state['sources'].items() if health.get('status') not in ('ok', None)]
    # Retain diagnostics from a legacy health event even when its source has not
    # yet been loaded into this snapshot.
    problems += [_health_row(event['source_id'], {}, event) for event in transitions
                 if event['source_id'] not in state['sources'] and event.get('change') != 'recovered']
    problems.sort(key=lambda row: (row['severity'] != 'error', row['institution']))
    recoveries = [_health_row(event['source_id'], state['sources'].get(event['source_id'], {}), event)
                  for event in transitions if event.get('change') == 'recovered']
    event_ids = sorted(event['id'] for event in transitions)
    # Pending aggregates from previous attempts are replaced by an up-to-date
    # aggregate, not blindly sent after the source has changed again.
    for event in list(outbox.values()):
        if event['kind'] in ('health_digest', 'health_changes') and event['state'] == 'pending':
            event.update(state='superseded', completed_at=now)
            changed = True
    if not problems and not recoveries:
        return changed
    if daily_sent and not transitions:
        return changed
    aggregate_id = daily_id if not daily_sent else 'health-changes:' + hashlib.sha256('|'.join(event_ids).encode()).hexdigest()[:20]
    # A repeated delivery attempt after a confirmed ack is a no-op.
    if outbox.get(aggregate_id, {}).get('state') == 'sent':
        return changed
    if daily_sent:
        changed_sources = {event['source_id'] for event in transitions}
        problems = [row for row in problems if row['source_id'] in changed_sources]
    outbox[aggregate_id] = {
        'id': aggregate_id, 'kind': 'health_changes' if daily_sent else 'health_digest',
        'created_at': now, 'state': 'pending', 'date': today,
        'events': event_ids, 'problems': problems, 'recoveries': recoveries,
    }
    return True


def payload_for(event: dict, state: dict) -> dict:
    if event['kind'] in ('health_digest', 'health_changes'):
        daily = event['kind'] == 'health_digest'
        problems, recoveries = event.get('problems', []), event.get('recoveries', [])
        content = ('職位檢索每日摘要' if daily else '職位來源狀態更新') + '｜' + event['date'] + '\n'
        content += f"未完整來源 {len(problems)} 個；恢復完整 {len(recoveries)} 個。\n"
        rows = []
        for row in recoveries:
            rows.append('✅ ' + plain(row['institution'], 55) + '：已恢復完整檢索')
        capability_labels = {'manual': '需手動查看', 'pending': '尚未接入',
                             'list_only': '只有清單', 'partial': '部分內容未接入'}
        for row in problems:
            label = capability_labels.get(row.get('capability')) or ('暫時未能讀取' if row.get('status') == 'unreachable' else None)
            reason = '；'.join(row.get('errors', [])[:1]) or '尚未取得完整資料'
            rows.append(('⚠️ ' if row.get('severity') == 'error' else 'ℹ️ ') + plain(row['institution'], 45)
                        + '：' + (label + '；' if label else '') + plain(reason, 85))
        for index, row in enumerate(rows):
            if len(content) + len(row) > 1740:
                content += f'另有 {len(rows) - index} 個來源，請查看網站「來源狀態」。\n'
                break
            content += row + '\n'
        content += '已保留歷史資料；未完成檢索不代表沒有新職位。'
    elif event['kind'] == 'health':
        # Kept for compatibility with saved outbox viewers; deliver aggregates it.
        recovered = event.get('change') == 'recovered'
        content = ('✅ 職位檢索已恢復｜' if recovered else '⚠️ 職位檢索未完成｜') + plain(event['source_id']) + '\n'
        content += '\n'.join(plain(error, 220) for error in event.get('errors', [])[:4])
        if not recovered:
            content += '\n已保留歷史資料；這不代表沒有新職位。'
    else:
        job = state['jobs'][event['job_id']]
        content = '新發現相關職位｜' + plain(job['institution']) + '\n**' + plain(job['title']) + '**\n'
        content += '刊登：' + (job.get('posted_date') or '網站未提供') + '｜首見：' + job['first_seen'][:10] + '\n'
        content += '科目：' + '、'.join(s['label'] for s in job['subjects']) + '\n'
        content += '截止：' + ((job.get('deadline') or '請查看原文') if job.get('deadline_type') == 'closing' else '請查看原文／持續招聘') + '\n'
        if job.get('match_basis') == 'title':
            content += '（院校網站只提供清單，以上按標題／部門判斷，請打開原文核實。）\n'
        content += safe_link(job['url']) + '\n相關度只反映科目及聘用類型，請自行核對學歷和經驗要求。'
    return {'content': content[:1900], 'allowed_mentions': {'parse': []}}


def deliver(state: dict, now: str, save) -> int:
    webhook = os.environ.get('DISCORD_WEBHOOK_URL', '').strip()
    if not webhook:
        state['notifications']['status'] = 'not-configured'
        save()
        return 0
    bits = urlsplit(webhook)
    if bits.scheme != 'https' or bits.hostname not in ('discord.com', 'discordapp.com') or not re.fullmatch(r'/api/webhooks/\d+/[A-Za-z0-9_.-]+', bits.path) or bits.username or bits.password:
        raise ValueError('Discord webhook 格式不正確；沒有發送任何訊息。')
    if prepare_health_summary(state, now):
        save()
    sent = 0
    # Vacancy notifications take precedence over the single health summary.
    pending = sorted((event for event in state['outbox'].values()
                      if event['state'] == 'pending' and event['kind'] != 'health'),
                     key=lambda event: (event['kind'] != 'new', event['created_at'], event['id']))
    for event in pending:
        if event['kind'] == 'new':
            job = state['jobs'].get(event['job_id'])
            if job and not job.get('detail_complete') and not job.get('detail_unavailable') and job['status'] == 'open':
                continue  # Defer until details recover; never lose the alert.
            if not job or not job['matches'] or job['status'] != 'open':
                event.update(state='suppressed', completed_at=now)
                save()
                continue
        try:
            response = requests.post(webhook, params={'wait': 'true'}, json=payload_for(event, state), timeout=(10, 25))
            if response.status_code == 429:
                retry = min(30.0, max(1.0, float(response.json().get('retry_after', 5))))
                state['notifications'].update(status='rate-limited', last_error_at=now)
                save()
                # Leave pending for next run instead of a tight retry loop.
                time.sleep(min(retry, 2))
                break
            acknowledgement = response.json() if response.status_code in (200, 201) else {}
            if not isinstance(acknowledgement, dict) or not acknowledgement.get('id'):
                state['notifications'].update(status='error', last_error_at=now, error='Discord 未確認接收，訊息保留待送。')
                save()
                break
            event.update(state='sent', sent_at=now, message_id=acknowledgement['id'])
            for event_id in event.get('events', []):
                child = state['outbox'].get(event_id)
                if child and child['state'] == 'pending':
                    child.update(state='sent', sent_at=now, message_id=acknowledgement['id'], summary_id=event['id'])
            state['notifications'].update(status='ok', last_success=now)
            state['notifications'].pop('error', None)
            sent += 1
            save()  # Persist summary + covered transitions together before another send.
            time.sleep(0.7)
        except (requests.RequestException, ValueError):
            state['notifications'].update(status='error', last_error_at=now, error='Discord 連線未確認；待送紀錄已保留。')
            save()
            break
    return sent
