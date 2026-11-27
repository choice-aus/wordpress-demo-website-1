"""Temporary candidates are retired by purpose, separately from backup retention."""

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import recovery_retention as retention
import release_cycle as cycle


class CandidateInventoryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, tzinfo=timezone.utc).isoformat()
        self.candidate = 'staging-next-cycle-123-1'
        self.backup = 'backup/staging-before-reset-' + 'b' * 12
        self.refs = dict(main='a' * 40, staging='a' * 40,
                         **{self.candidate: 'a' * 40, self.backup: 'b' * 40})

    def plan(self, states, preserved=True, include_legacy=False, protected=(), scope='all'):
        proof = Mock(return_value=preserved)
        plan = retention.build_plan('choice/repo', self.refs, [], set(protected), self.now,
                                    proof, candidate_states=states, scope=scope,
                                    include_legacy_candidates=include_legacy)
        return plan, proof

    def test_redundant_completed_candidates_ignore_age_cycle_count_and_old_failure_flags(self):
        for conclusion in ('success', 'failure', 'cancelled', 'timed_out'):
            with self.subTest(conclusion=conclusion):
                plan, proof = self.plan({self.candidate: dict(legacy=False, status='completed', conclusion=conclusion)},
                                        protected=(self.candidate, self.backup))
                self.assertEqual(plan['cleanup'], {self.candidate: 'a' * 40})
                self.assertNotIn(self.backup, plan['cleanup'])
                proof.assert_called_once_with('a' * 40)

    def test_active_and_waiting_candidates_are_kept_even_when_content_is_redundant(self):
        for status in ('in_progress', 'queued', 'waiting', 'pending'):
            with self.subTest(status=status):
                plan, proof = self.plan({self.candidate: dict(legacy=False, status=status)})
                self.assertEqual(plan['cleanup'], {})
                proof.assert_not_called()

    def test_latest_successful_cycle_keeps_backup_but_not_redundant_candidate(self):
        completed = dict(run_id='123', run_attempt='1', completed_at=self.now,
                         backup=self.backup, backup_sha='b' * 40,
                         candidate=self.candidate, candidate_sha='a' * 40)
        plan = retention.build_plan('choice/repo', self.refs, [completed], set(), self.now,
                                    lambda sha: True, candidate_states={self.candidate: dict(
                                        legacy=False, status='completed', conclusion='success')})
        self.assertEqual(plan['cleanup'], {self.candidate: 'a' * 40})
        backup = next(item for item in plan['assessments'] if item['branch'] == self.backup)
        self.assertIn('latest 3 successful cycles', backup['reason'])

    def test_preparation_with_unique_content_is_kept_even_with_explicit_legacy_review(self):
        for state in (dict(legacy=False, status='completed'), dict(legacy=True, status='unknown')):
            plan, proof = self.plan({self.candidate: state}, preserved=False, include_legacy=True)
            self.assertEqual(plan['cleanup'], {})
            proof.assert_called_once_with('a' * 40)

    def test_open_pr_protects_completed_candidate(self):
        plan, proof = self.plan({self.candidate: dict(legacy=False, status='completed', open_prs=[99])})
        self.assertEqual(plan['cleanup'], {})
        proof.assert_not_called()

    def test_legacy_cleanup_needs_explicit_review_and_preservation_proof(self):
        self.refs['staging-next-cycle'] = 'c' * 40
        default, proof = self.plan({})
        self.assertEqual(default['cleanup'], {})
        proof.assert_not_called()
        reviewed, proof = self.plan({}, include_legacy=True, scope='candidates')
        self.assertEqual(set(reviewed['cleanup']), {self.candidate, 'staging-next-cycle'})
        self.assertNotIn(self.backup, reviewed['refs'])
        self.assertTrue(reviewed['include_legacy_candidates'])

    def test_legacy_candidate_kept_while_any_known_preparation_is_active(self):
        plan, proof = self.plan({self.candidate: dict(legacy=True, status='unknown', active_preparations=True)},
                                include_legacy=True)
        self.assertEqual(plan['cleanup'], {})
        proof.assert_not_called()

    def test_candidate_only_assessment_does_not_read_expiring_artifacts(self):
        with patch.object(retention, 'recovery_refs', return_value=self.refs), \
             patch.object(retention, 'completed_cycles') as artifacts, \
             patch.object(retention, 'candidate_run_states', return_value={
                 self.candidate: dict(legacy=False, status='completed')}), \
             patch.object(retention, 'preservation_checker', return_value=lambda sha: True):
            plan = retention.assess('choice/repo', self.now, 3, 30, 'candidates', True)
        artifacts.assert_not_called()
        self.assertEqual(plan['cleanup'], {self.candidate: 'a' * 40})
        self.assertNotIn(self.backup, plan['refs'])

    def test_backup_only_assessment_does_not_query_candidate_states(self):
        with patch.object(retention, 'recovery_refs', return_value=self.refs), \
             patch.object(retention, 'completed_cycles', return_value=([], set())), \
             patch.object(retention, 'candidate_run_states') as states, \
             patch.object(retention, 'preservation_checker', return_value=lambda sha: True):
            plan = retention.assess('choice/repo', self.now, 3, 30, 'backups')
        states.assert_not_called()
        self.assertNotIn(self.candidate, plan['refs'])
        self.assertEqual(plan['cleanup'], {})

    def test_older_completed_attempt_can_be_retired_without_touching_active_retry(self):
        refs = {self.candidate: 'a' * 40, 'staging-next-cycle-123-2': 'b' * 40,
                'staging-next-cycle': 'c' * 40}
        root_run = dict(id=123, run_attempt=2, status='in_progress', conclusion=None)
        with patch.object(retention, 'paginated', return_value=[root_run]) as history, \
             patch.object(retention, 'get_open_prs', return_value=[]), \
             patch.object(retention, 'github', return_value=dict(status='completed', conclusion='failure')) as api:
            states = retention.candidate_run_states('choice/repo', refs)
        self.assertEqual(states[self.candidate]['status'], 'completed')
        self.assertEqual(states['staging-next-cycle-123-2']['status'], 'in_progress')
        self.assertTrue(states['staging-next-cycle']['active_preparations'])
        history.assert_called_once_with('repos/choice/repo/actions/runs')
        api.assert_called_once_with('api', 'repos/choice/repo/actions/runs/123/attempts/1')

    def test_retired_preparation_workflow_history_keeps_active_and_legacy_candidates(self):
        runs = [dict(id=123, run_attempt=1, status='waiting', conclusion=None,
                     path='.github/workflows/prepare-next-uat-cycle.yml'),
                dict(id=456, run_attempt=1, status='in_progress', conclusion=None,
                     path='.github/workflows/ci.yml')]
        with patch.object(retention, 'paginated', return_value=runs) as history, \
             patch.object(retention, 'get_open_prs', return_value=[]):
            states = retention.candidate_run_states('choice/repo', {
                self.candidate: 'a' * 40, 'staging-next-cycle': 'b' * 40})
        history.assert_called_once_with('repos/choice/repo/actions/runs')
        self.assertEqual(states[self.candidate]['status'], 'waiting')
        self.assertFalse(states[self.candidate]['legacy'])
        self.assertTrue(states['staging-next-cycle']['active_preparations'])

    def test_unrelated_active_ci_does_not_hold_legacy_candidate(self):
        runs = [dict(id=456, run_attempt=1, status='in_progress', conclusion=None,
                     path='.github/workflows/ci.yml')]
        with patch.object(retention, 'paginated', return_value=runs), \
             patch.object(retention, 'get_open_prs', return_value=[]):
            states = retention.candidate_run_states('choice/repo', {'staging-next-cycle': 'a' * 40})
        self.assertFalse(states['staging-next-cycle']['active_preparations'])

    def test_completed_retired_workflow_candidate_remains_identifiable(self):
        runs = [dict(id=123, run_attempt=1, status='completed', conclusion='success',
                     path='.github/workflows/prepare-next-uat-cycle.yml@main')]
        with patch.object(retention, 'paginated', return_value=runs), \
             patch.object(retention, 'get_open_prs', return_value=[]):
            states = retention.candidate_run_states('choice/repo', {self.candidate: 'a' * 40})
        self.assertFalse(states[self.candidate]['legacy'])
        self.assertEqual(states[self.candidate]['status'], 'completed')

    def test_summary_leads_with_counts_and_separates_candidate_and_backup_reasons(self):
        plan, _ = self.plan({self.candidate: dict(legacy=False, status='completed')})
        report = retention.report(plan)
        self.assertIn('1 branch eligible for deletion; 1 retained', report)
        self.assertIn('Temporary candidates: 1 delete, 0 keep', report)
        self.assertIn('Backups: 0 delete, 1 keep', report)
        self.assertIn('**DELETE** `' + self.candidate + '`', report)


class PromotedCandidateLeaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.previous = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, self.previous)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('commit', '--allow-empty', '-m', '[DEV-222] - Original staging fixture')
        self.original = self.git('rev-parse', 'HEAD')
        self.candidate = 'staging-next-cycle-123-1'
        self.backup = 'backup/staging-before-reset-' + self.original[:12]
        self.git('switch', '-c', self.candidate)
        Path('carryover.txt').write_text('preserve this verified staging content\n')
        self.git('add', 'carryover.txt')
        self.git('commit', '-m', '[DEV-222] - Candidate fixture')
        self.sha = self.git('rev-parse', 'HEAD')
        self.git('branch', 'staging', self.sha)
        self.git('branch', self.backup, self.original)
        self.remote = str(Path(self.directory.name) / 'remote.git')
        self.git('clone', '--bare', '.', self.remote)
        self.git('--git-dir=' + self.remote, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        self.git('remote', 'add', 'origin', self.remote)
        self.plan = dict(repository='choice/repo', staging=self.original)

    def git(self, *args):
        return subprocess.check_output(['git', *args], text=True, stderr=subprocess.DEVNULL).strip()

    def retire(self):
        with patch.object(cycle, 'remote_sha', side_effect=lambda repo, branch:
                          self.git('--git-dir=' + self.remote, 'rev-parse', 'refs/heads/' + branch)), \
             patch.object(cycle, 'approval_environment'), patch.object(cycle, 'verify_remote_repository'):
            cycle.remove_promoted_candidate(self.plan, self.candidate, self.sha, self.backup)

    def test_candidate_name_removed_while_content_stays_on_staging_and_backup_survives(self):
        self.retire()
        self.assertEqual(self.git('--git-dir=' + self.remote, 'for-each-ref', '--format=%(refname)',
                                  'refs/heads/' + self.candidate), '')
        self.assertEqual(self.git('--git-dir=' + self.remote, 'rev-parse', 'staging'), self.sha)
        self.assertEqual(self.git('--git-dir=' + self.remote, 'rev-parse', self.backup), self.original)
        self.assertEqual(self.git('--git-dir=' + self.remote, 'show', 'staging:carryover.txt'),
                         'preserve this verified staging content')

    def test_candidate_move_immediately_before_push_rejects_lease(self):
        real_git = cycle.git
        def move_then_push(*args):
            if args[0] == 'push':
                self.git('--git-dir=' + self.remote, 'update-ref', 'refs/heads/' + self.candidate, self.original)
            return real_git(*args)
        with patch.object(cycle, 'git', side_effect=move_then_push):
            with self.assertRaises(subprocess.CalledProcessError):
                self.retire()
        self.assertEqual(self.git('--git-dir=' + self.remote, 'rev-parse', self.candidate), self.original)
        self.assertEqual(self.git('--git-dir=' + self.remote, 'rev-parse', 'staging'), self.sha)


if __name__ == '__main__':
    unittest.main()
