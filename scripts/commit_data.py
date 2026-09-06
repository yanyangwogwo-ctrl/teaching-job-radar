"""Commit only the canonical state and its public exports in GitHub Actions."""
import os
import subprocess


def git(*args, **kwargs):
    return subprocess.run(['git', *args], check=True, **kwargs)


def push_data(base, branch):
    for attempt in range(3):
        result = subprocess.run(['git', 'push', 'origin', f'HEAD:refs/heads/{branch}'])
        if result.returncode == 0:
            return
        git('fetch', '--no-tags', 'origin', branch)
        target = git('rev-parse', 'FETCH_HEAD', capture_output=True, text=True).stdout.strip()
        if target == base or subprocess.run(['git', 'merge-base', '--is-ancestor', base, target]).returncode:
            raise RuntimeError('資料未能推送，或遠端基準已改變；沒有覆寫遠端資料。')
        overlap = git('diff', '--name-only', base, target, '--', 'data/store.json', 'dist/data', capture_output=True, text=True).stdout.strip()
        if overlap:
            raise RuntimeError('遠端亦有職位資料更新；已保留本輪備份，需核對後再合併。')
        # Reapply only our data commit on top of concurrent code-only edits.
        # Never auto-merge two copies of the canonical job state or force-push.
        try:
            git('rebase', '--onto', target, base)
        except subprocess.CalledProcessError:
            subprocess.run(['git', 'rebase', '--abort'])
            raise
        base = target
    raise RuntimeError('遠端持續更新，資料仍未能推送；本輪備份已保留。')


if __name__ == '__main__':
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('此步驟只供已設定的 GitHub Actions 執行。')
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    base = git('rev-parse', 'HEAD', capture_output=True, text=True).stdout.strip()
    branch = git('branch', '--show-current', capture_output=True, text=True).stdout.strip()
    if not branch:
        raise SystemExit('必須在指定分支保存資料，不能使用 detached HEAD。')
    git('add', '--', 'data/store.json', 'dist/data')
    changed = subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode
    if changed == 1:
        git('commit', '-m', 'Update vacancy data and delivery receipts')
        push_data(base, branch)
    elif changed != 0:
        raise SystemExit(changed)
