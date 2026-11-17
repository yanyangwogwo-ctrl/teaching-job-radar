"""Daily runs cover all sources; owners can request a targeted repair run.

Two things can start the daily run:
  * GitHub's own `schedule` event (the cron line in daily.yml), and
  * an outside timer (for example cron-job.org) calling the workflow_dispatch
    API with input reason=schedule. GitHub's scheduler has been starting runs
    hours late, so the outside timer gives the punctual run and the cron line
    remains as a backup.
Whichever arrives second sees that a full run already happened since today's
slot and exits without crawling, so the sites are not read twice a day.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml

# A full run this long before today's slot still counts as today's run (an
# outside timer set a little earlier than the cron line, or a recent push).
SLOT_TOLERANCE_HOURS = 2
# Used only when the cron line cannot be read.
FALLBACK_WINDOW_HOURS = 12


def selected_sources(event_name, event, manual=''):
    text = ''
    if event_name == 'workflow_dispatch':
        text = manual.strip()
    elif event_name == 'push':
        # Explicit commit trailer for repairs; never applied to daily schedules.
        trailers = re.findall(r'^Recheck-Sources:\s*([^\r\n]+)$', event.get('head_commit', {}).get('message', ''), re.M)
        if len(trailers) > 1:
            raise ValueError('只可指定一行 Recheck-Sources。')
        text = trailers[0].strip() if trailers else ''
    if not text:
        return []
    if not re.fullmatch(r'[a-z0-9-]+(?:[ ,]+[a-z0-9-]+)*', text):
        raise ValueError('指定來源只可包含來源 ID、空格及逗號。')
    return list(dict.fromkeys(re.split(r'[ ,]+', text)))


def trigger_kind(event_name, environ):
    """'schedule' for GitHub's cron or an outside timer; otherwise the event."""
    if event_name == 'schedule':
        return 'schedule'
    if event_name == 'workflow_dispatch' and environ.get('RADAR_TRIGGER', '').strip().lower() == 'schedule':
        return 'schedule'
    return event_name


def trigger_label(event_name, environ):
    if event_name == 'schedule':
        return 'github-schedule'
    if trigger_kind(event_name, environ) == 'schedule':
        return 'external-schedule'
    return event_name or 'manual'


def schedule_timing(event_name, root, now):
    """Measure time since the latest configured daily slot, not GitHub queue time.

    GitHub does not include the intended firing timestamp in schedule events.
    A delay longer than one day cannot be reconstructed from the cron alone.
    Manual runs deliberately have no scheduled-time estimate. An outside timer
    should use the same daily time as the cron line, so the same slot applies.
    """
    if event_name != 'schedule':
        return {}
    try:
        workflow = yaml.safe_load((root / '.github/workflows/daily.yml').read_text())
        events = workflow.get('on', workflow.get(True, {}))
        schedules = events['schedule']
        if len(schedules) != 1:
            return {}
        item = schedules[0]
        match = re.fullmatch(r'(\d{1,2}) (\d{1,2}) \* \* \*', item['cron'])
        zone = item.get('timezone', 'UTC')
        if not match or zone not in ('UTC', 'Asia/Hong_Kong'):
            return {}
        minute, hour = map(int, match.groups())
        if not 0 <= minute < 60 or not 0 <= hour < 24 or now.tzinfo is None:
            return {}
        local_now = now.astimezone(ZoneInfo(zone))
        expected = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if expected > local_now:
            expected -= timedelta(days=1)
        return {
            'expected_at': expected.astimezone(timezone.utc).isoformat(timespec='seconds'),
            'started_at': now.astimezone(timezone.utc).isoformat(timespec='seconds'),
            'delay_seconds': max(0, int((local_now - expected).total_seconds())),
            'timing_basis': 'latest-daily-slot-before-monitor-start',
        }
    except (OSError, KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        return {}


def execution_environment(event_name, root, now, environ):
    # Discard inherited timing from an earlier invocation before setting this run.
    result = {key: value for key, value in environ.items() if not key.startswith('RADAR_SCHEDULE_')}
    timing = schedule_timing(trigger_kind(event_name, environ), root, now)
    for key, value in timing.items():
        result['RADAR_SCHEDULE_' + key.upper()] = str(value)
    if timing:
        result['RADAR_SCHEDULE_TRIGGER'] = trigger_label(event_name, environ)
    return result


def recent_full_run(root, now):
    """A run that checked every enabled source since today's daily slot.

    "Today's slot" is the latest configured daily time at or before now, less
    a small tolerance. A run from before it (yesterday's, or an evening code
    push) never suppresses today's scheduled run. Targeted repair runs (fewer
    sources) never count. Any doubt returns None so the crawl goes ahead.
    """
    try:
        timing = schedule_timing('schedule', root, now)
        if timing:
            since = datetime.fromisoformat(timing['expected_at']) - timedelta(hours=SLOT_TOLERANCE_HOURS)
        else:
            since = now - timedelta(hours=FALLBACK_WINDOW_HOURS)
        sources = yaml.safe_load((root / 'config/sites.yaml').read_text(encoding='utf-8'))['sources']
        enabled = {source['id'] for source in sources if source.get('enabled', True)}
        state = json.loads((root / 'data/store.json').read_text(encoding='utf-8'))
        for run in reversed(state.get('runs', [])):
            started = datetime.fromisoformat(run['started_at'])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started < since:
                return None
            if started > now:
                continue
            covered = {summary.get('source_id') for summary in run.get('sources', [])}
            if enabled and enabled <= covered:
                return run
    except (OSError, KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        return None
    return None


if __name__ == '__main__':
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    event_name = os.environ.get('GITHUB_EVENT_NAME')
    root = Path(__file__).resolve().parents[1]
    now = datetime.now(timezone.utc)
    selected = selected_sources(event_name, event, os.environ.get('RADAR_SOURCE_IDS', ''))
    if trigger_kind(event_name, os.environ) == 'schedule' and not selected:
        previous = recent_full_run(root, now)
        if previous:
            print(f"已於 {previous['started_at']} 完成完整檢索；此排程屬同日後備觸發，略過重複檢索。", flush=True)
            raise SystemExit(0)
    # monitor.run validates every ID against sites.yaml. No shell interpolation.
    command = [sys.executable, '-m', 'monitor.run']
    if selected:
        command += ['--sources', *selected]
    environment = execution_environment(event_name, root, now, os.environ)
    raise SystemExit(subprocess.run(command, check=False, env=environment).returncode)
