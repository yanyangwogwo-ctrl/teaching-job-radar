import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.run_scheduled import selected_sources

ROOT = Path(__file__).resolve().parents[1]


class SelectionTests(unittest.TestCase):
    def test_daily_always_checks_every_source(self):
        event = {'head_commit': {'message': 'Fix parser\n\nRecheck-Sources: hsu,hkust'}}
        self.assertEqual(selected_sources('schedule', event, 'hsu'), [])
        self.assertEqual(selected_sources('push', event), ['hsu', 'hkust'])
        self.assertEqual(selected_sources('push', {}), [])
        self.assertEqual(selected_sources('workflow_dispatch', {}, 'hsu, hsu hkust'), ['hsu', 'hkust'])
        with self.assertRaises(ValueError):
            selected_sources('workflow_dispatch', {}, 'hsu; echo secret')
        with self.assertRaises(ValueError):
            selected_sources('push', {'head_commit': {'message': 'Recheck-Sources: hsu\nRecheck-Sources: hku'}})


class PersistenceTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); self.remote = root/'remote.git'; self.owner = root/'owner'; self.bot = root/'bot'
        self.git(root, 'init', '--bare', str(self.remote))
        self.git(root, 'clone', str(self.remote), str(self.owner))
        self.git(self.owner, 'config', 'user.name', 'Test Owner')
        self.git(self.owner, 'config', 'user.email', 'owner@example.test')
        self.git(self.owner, 'checkout', '-b', 'main')
        (self.owner/'data').mkdir(); (self.owner/'dist/data').mkdir(parents=True)
        (self.owner/'data/store.json').write_text('{"baseline":true}\n')
        (self.owner/'dist/data/jobs.json').write_text('{"jobs":[]}\n')
        (self.owner/'README.md').write_text('Initial code\n')
        self.git(self.owner, 'add', '.')
        self.git(self.owner, 'commit', '-m', 'Initial state')
        self.git(self.owner, 'push', '-u', 'origin', 'main')
        self.git(root, 'clone', '-b', 'main', str(self.remote), str(self.bot))
        (self.bot/'data/store.json').write_text('{"new_jobs":15}\n')
        (self.bot/'dist/data/jobs.json').write_text('{"jobs":[15]}\n')

    def concurrent_change(self, filename, value):
        (self.owner/filename).write_text(value)
        self.git(self.owner, 'add', filename)
        self.git(self.owner, 'commit', '-m', 'Concurrent owner update')
        self.git(self.owner, 'push', 'origin', 'main')

    def persist(self):
        return subprocess.run([sys.executable, str(ROOT/'scripts/commit_data.py')], cwd=self.bot,
                              env={**os.environ, 'GITHUB_ACTIONS': 'true'}, capture_output=True, text=True)

    def test_concurrent_code_edit_and_crawl_both_survive(self):
        self.concurrent_change('README.md', 'Improved code\n')
        result = self.persist()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.remote, 'show', 'main:README.md'), 'Improved code')
        self.assertEqual(self.git(self.remote, 'show', 'main:data/store.json'), '{"new_jobs":15}')

    def test_concurrent_data_update_is_not_overwritten(self):
        self.concurrent_change('data/store.json', '{"owner_update":true}\n')
        result = self.persist()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.remote, 'show', 'main:data/store.json'), '{"owner_update":true}')
        self.assertEqual((self.bot/'data/store.json').read_text(), '{"new_jobs":15}\n')
