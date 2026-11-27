"""CI triggered by a PAT staging reset must settle before release creation."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import release_checks as checks

SHA = 'a' * 40


def run(number=1, status='completed', conclusion='success', **extra):
    return dict(id=number, head_sha=SHA, event='push', run_attempt=1,
                status=status, conclusion=conclusion, **extra)


class ReleaseChecksTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0
        self.polls = []
        self.guard = Mock()
        now = patch.object(checks.time, 'monotonic', side_effect=lambda: self.clock)
        sleep = patch.object(checks.time, 'sleep', side_effect=self.advance)
        now.start()
        sleep.start()
        self.addCleanup(now.stop)
        self.addCleanup(sleep.stop)

    def advance(self, seconds):
        self.clock += seconds

    def wait(self, snapshots, **bounds):
        snapshots = iter(snapshots)
        current = []
        def state(*args):
            nonlocal current
            current = next(snapshots, current)
            self.polls.append(current)
            return current
        with patch.object(checks, 'push_ci_runs', side_effect=state):
            checks.wait_for_ci('choice/repo', SHA, self.guard,
                               timeout=30, interval=5, quiet_period=10, **bounds)

    def test_completed_main_ci_does_not_hide_new_active_staging_ci(self):
        self.wait([[run(), run(2, 'in_progress', None)], [run(), run(2)], [run(), run(2)], [run(), run(2)]])
        self.assertEqual(len(self.polls), 4)
        self.assertEqual(self.guard.call_count, 4)
        self.assertEqual(self.clock, 15)

    def test_late_arriving_ci_resets_quiet_interval(self):
        self.wait([[run()], [run(), run(2, 'queued', None)], [run(), run(2)]])
        self.assertEqual(self.clock, 20)
        self.assertEqual(self.guard.call_count, 5)

    def test_active_missing_or_never_settled_ci_times_out(self):
        for snapshots in ([[]], [[run(2, 'in_progress', None)]]):
            self.clock = 0
            with self.subTest(snapshots=snapshots), self.assertRaisesRegex(RuntimeError, 'Timed out'):
                self.wait(snapshots)
            self.assertEqual(self.clock, 30)

    def test_latest_ci_failure_aborts_without_waiting_or_ignoring_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'ended failure'):
            self.wait([[run(), run(2, conclusion='failure')]])
        self.assertEqual(self.clock, 0)

    def test_revalidation_failure_during_wait_stops_further_polling(self):
        self.guard.side_effect = [None, RuntimeError('Main changed')]
        with self.assertRaisesRegex(RuntimeError, 'Main changed'):
            self.wait([[run(2, 'in_progress', None)]])
        self.assertEqual(len(self.polls), 1)

    def test_network_or_authentication_error_is_not_ready(self):
        failure = subprocess.CalledProcessError(1, ['gh', 'api'])
        with patch.object(checks, 'push_ci_runs', side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                checks.wait_for_ci('choice/repo', SHA, self.guard)
        self.assertEqual(self.clock, 0)

    def test_workflow_history_is_paginated_and_filters_exact_sha_and_event(self):
        batches = [dict(workflow_runs=[run(i) for i in range(100)]),
                   dict(workflow_runs=[run(101), dict(run(102), head_sha='b' * 40),
                                       dict(run(103), event='pull_request')])]
        with patch.object(checks, 'github', side_effect=batches) as api:
            result = checks.push_ci_runs('choice/repo', SHA)
        self.assertEqual(len(result), 101)
        self.assertIn('page=2', api.call_args.args[1])


if __name__ == '__main__':
    unittest.main()
