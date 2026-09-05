from __future__ import annotations

import os
import re
import time
from urllib.parse import urlsplit

import requests


def safe_link(url: str) -> str:
    parts = urlsplit(url)
    return url.replace(')', '%29').replace('(', '%28').replace(' ', '%20') if parts.scheme == 'https' and parts.hostname else ''


def plain(value: str, limit=240) -> str:
    return re.sub(r'([\\*_`~>|\[\]])', r'\\\1', str(value))[:limit]


def payload_for(event: dict, state: dict) -> dict:
    if event['kind'] == 'health':
        content = '⚠️ 職位檢索未完成｜' + plain(event['source_id']) + '\n'
        content += '\n'.join(plain(error, 220) for error in event.get('errors', [])[:4])
        content += '\n已保留歷史資料；這不代表沒有新職位。'
    else:
        job = state['jobs'][event['job_id']]
        content = '新發現相關職位｜' + plain(job['institution']) + '\n**' + plain(job['title']) + '**\n'
        content += '刊登：' + (job.get('posted_date') or '網站未提供') + '｜首見：' + job['first_seen'][:10] + '\n'
        content += '科目：' + '、'.join(s['label'] for s in job['subjects']) + '\n'
        content += '截止：' + ((job.get('deadline') or '請查看原文') if job.get('deadline_type') == 'closing' else '請查看原文／持續招聘') + '\n'
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
    sent = 0
    newest_health = {}
    for item in state['outbox'].values():
        if item['kind'] == 'health' and item['state'] == 'pending':
            candidate = (item['created_at'], item['id'])
            source_id = item['source_id']
            newest_health[source_id] = max(newest_health.get(source_id, ('', '')), candidate)
    for event in state['outbox'].values():
        if event['state'] != 'pending':
            continue
        if event['kind'] == 'new':
            job = state['jobs'].get(event['job_id'])
            if job and not job.get('detail_complete') and job['status'] == 'open':
                continue  # Defer until details recover; never lose the alert.
            if not job or not job['matches'] or job['status'] != 'open':
                event.update(state='suppressed', completed_at=now)
                save()
                continue
        else:
            if newest_health.get(event['source_id'], ('', ''))[1] != event['id']:
                event.update(state='superseded', completed_at=now)
                save()
                continue
            # Do not deliver an old outage after the source has recovered.
            if state['sources'].get(event['source_id'], {}).get('status') == 'ok':
                event.update(state='resolved', completed_at=now)
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
            if response.status_code not in (200, 201) or not response.json().get('id'):
                state['notifications'].update(status='error', last_error_at=now, error='Discord 未確認接收，訊息保留待送。')
                save()
                break
            event.update(state='sent', sent_at=now, message_id=response.json()['id'])
            state['notifications'].update(status='ok', last_success=now)
            state['notifications'].pop('error', None)
            sent += 1
            save()  # Persist each acknowledgement before attempting another.
            time.sleep(0.7)
        except (requests.RequestException, ValueError):
            state['notifications'].update(status='error', last_error_at=now, error='Discord 連線未確認；待送紀錄已保留。')
            save()
            break
    return sent
