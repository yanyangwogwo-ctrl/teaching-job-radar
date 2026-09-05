"""Display the actual workflow schedule instead of a separately edited label."""
from pathlib import Path
import re

import yaml


def schedule_description(root: Path) -> str:
    try:
        workflow = yaml.safe_load((root / '.github/workflows/daily.yml').read_text())
        events = workflow.get('on', workflow.get(True, {}))
        schedules = events['schedule']
        if len(schedules) != 1:
            raise ValueError('multiple schedules')
        item = schedules[0]
        match = re.fullmatch(r'(\d{1,2}) (\d{1,2}) \* \* \*', item['cron'])
        zone = item.get('timezone', 'UTC')
        if not match or zone not in ('UTC', 'Asia/Hong_Kong'):
            raise ValueError('unsupported schedule')
        minute, hour = map(int, match.groups())
        if not 0 <= minute < 60 or not 0 <= hour < 24:
            raise ValueError('invalid time')
        hour = (hour + (8 if zone == 'UTC' else 0)) % 24
        return f'每日約 {hour:02d}:{minute:02d}（香港時間；排程可能延遲）'
    except (OSError, KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        return '未能確認每日排程時間，請查看 GitHub 設定。'
