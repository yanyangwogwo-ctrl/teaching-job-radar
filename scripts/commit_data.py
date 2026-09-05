"""Commit only the canonical state and its public exports in GitHub Actions."""
import os
import subprocess


def git(*args, **kwargs):
    return subprocess.run(['git', *args], check=True, **kwargs)


if __name__ == '__main__':
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('此步驟只供已設定的 GitHub Actions 執行。')
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git('add', '--', 'data/store.json', 'dist/data')
    changed = subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode
    if changed == 1:
        git('commit', '-m', 'Update vacancy data and delivery receipts')
        # Do not force-push or discard a concurrent user edit. If rejected,
        # leave the run failed for review instead of sending unpersisted alerts.
        git('push', 'origin', 'HEAD')
    elif changed != 0:
        raise SystemExit(changed)
