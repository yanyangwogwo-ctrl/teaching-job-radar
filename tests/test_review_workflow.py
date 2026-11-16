"""Schedule observability without inventing exact GitHub trigger timestamps."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from scripts.run_scheduled import execution_environment, schedule_timing


class ScheduleTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.github/workflows').mkdir(parents=True)
        self.workflow = self.root / '.github/workflows/daily.yml'
        self.workflow.write_text("on:\n  schedule:\n    - cron: '17 0 * * *'\n")
        self.now = datetime(2026, 11, 16, 6, 41, 0, tzinfo=timezone.utc)

    def test_scheduled_run_records_monitor_start_and_latest_slot(self):
        result = schedule_timing('schedule', self.root, self.now)
        self.assertEqual(result['expected_at'], '2026-11-16T00:17:00+00:00')
        self.assertEqual(result['started_at'], '2026-11-16T06:41:00+00:00')
        self.assertEqual(result['delay_seconds'], 6 * 3600 + 24 * 60)
        self.assertEqual(result['timing_basis'], 'latest-daily-slot-before-monitor-start')

    def test_hong_kong_schedule_and_day_boundary(self):
        self.workflow.write_text("on:\n  schedule:\n    - cron: '17 8 * * *'\n      timezone: Asia/Hong_Kong\n")
        early = datetime(2026, 11, 16, 0, 10, tzinfo=timezone.utc)
        result = schedule_timing('schedule', self.root, early)
        self.assertEqual(result['expected_at'], '2026-11-15T00:17:00+00:00')
        self.assertEqual(result['delay_seconds'], 23 * 3600 + 53 * 60)

    def test_manual_and_push_runs_do_not_inherit_a_delay(self):
        env = {'PATH': '/usr/bin', 'RADAR_SCHEDULE_EXPECTED_AT': 'stale', 'RADAR_SCHEDULE_DELAY_SECONDS': '999'}
        for event in ('workflow_dispatch', 'push', None):
            result = execution_environment(event, self.root, self.now, env)
            self.assertEqual(result, {'PATH': '/usr/bin'})

    def test_unrecognised_schedule_does_not_invent_times(self):
        for value in ("on:\n  schedule:\n    - cron: '*/15 * * * *'\n", "on:\n  schedule:\n    - cron: '17 0 * * *'\n    - cron: '17 1 * * *'\n", "on:\n  schedule:\n    - cron: '17 0 * * *'\n      timezone: unknown\n"):
            self.workflow.write_text(value)
            self.assertEqual(schedule_timing('schedule', self.root, self.now), {})

    def test_expected_environment_is_machine_readable(self):
        result = execution_environment('schedule', self.root, self.now, {'PATH': '/usr/bin'})
        self.assertEqual(result['RADAR_SCHEDULE_DELAY_SECONDS'], '23040')
        self.assertEqual(result['RADAR_SCHEDULE_TIMING_BASIS'], 'latest-daily-slot-before-monitor-start')
        self.assertEqual(result['PATH'], '/usr/bin')


if __name__ == '__main__':
    unittest.main()
