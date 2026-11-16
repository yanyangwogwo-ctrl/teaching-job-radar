"""Daily runs cover all sources; owners can request a targeted repair run."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import yaml


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


def schedule_timing(event_name, root, now):
    """Measure time since the latest configured daily slot, not GitHub queue time.

    GitHub does not include the intended firing timestamp in schedule events.
    A delay longer than one day cannot be reconstructed from the cron alone.
    Manual runs deliberately have no scheduled-time estimate.
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
    for key, value in schedule_timing(event_name, root, now).items():
        result['RADAR_SCHEDULE_' + key.upper()] = str(value)
    return result


if __name__ == '__main__':
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    selected = selected_sources(os.environ.get('GITHUB_EVENT_NAME'), event, os.environ.get('RADAR_SOURCE_IDS', ''))
    # monitor.run validates every ID against sites.yaml. No shell interpolation.
    command = [sys.executable, '-m', 'monitor.run']
    if selected:
        command += ['--sources', *selected]
    environment = execution_environment(os.environ.get('GITHUB_EVENT_NAME'), Path(__file__).resolve().parents[1], datetime.now(timezone.utc), os.environ)
    raise SystemExit(subprocess.run(command, check=False, env=environment).returncode)
