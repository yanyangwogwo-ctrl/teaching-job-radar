"""Daily runs cover all sources; owners can request a targeted repair run."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


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


if __name__ == '__main__':
    event_path = os.environ.get('GITHUB_EVENT_PATH')
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    selected = selected_sources(os.environ.get('GITHUB_EVENT_NAME'), event, os.environ.get('RADAR_SOURCE_IDS', ''))
    # monitor.run validates every ID against sites.yaml. No shell interpolation.
    command = [sys.executable, '-m', 'monitor.run']
    if selected:
        command += ['--sources', *selected]
    raise SystemExit(subprocess.run(command, check=False).returncode)
