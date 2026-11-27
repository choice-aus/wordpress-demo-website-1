"""Candidate retirement follows promotion; next release creation follows cleanup."""

import contextlib
import copy
import json
import os
from pathlib import Path
import sys
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import release_cycle as cycle
import uat_carryover as carry
import create_release_branch as creation


class FollowupTests(unittest.TestCase):
    def setUp(self):
        self.plan = dict(repository='choice/repo', release='release-old', latest='release-old',
                         prepare=True, main='a' * 40, staging='b' * 40, release_sha='c' * 40,
                         cleanup={'release-old': 'c' * 40},
                         options=dict(next_release='release-next', remove_candidate=True))
        self.refs = dict(main='a' * 40, staging='d' * 40, candidate='d' * 40, backup='b' * 40)

    @contextlib.contextmanager
    def guarded(self, refs=None, existing=None):
        refs = self.refs if refs is None else refs
        with patch.object(cycle, 'get_latest_release', return_value='release-old'), \
             patch.object(cycle, 'remote_sha', side_effect=lambda repo, branch: refs[branch]), \
             patch.object(cycle, 'approval_environment'), \
             patch.object(cycle, 'verify_remote_repository'), \
             patch.object(cycle, 'matching_refs', return_value=existing or []), \
             patch.object(cycle, 'github') as api, \
             patch.object(cycle, 'git') as git, \
             patch.object(cycle, 'Path') as path, \
             patch.object(creation, 'matching_refs', return_value=existing or []), \
             patch.object(creation, 'remote_sha', side_effect=lambda repo, branch: refs[branch]), \
             patch.object(creation, 'wait_for_ci'), \
             patch.object(creation, 'push_ci_runs', return_value=[]), \
             patch.object(creation, 'github', side_effect=lambda *args: api(*args)), \
             patch.dict(os.environ, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1'):
            api.return_value = dict(ref='refs/heads/release-next', object=dict(sha='a' * 40))
            yield api, git, path

    def closure(self, path):
        return next(json.loads(call.args[0]) for call in path.return_value.write_text.call_args_list
                    if json.loads(call.args[0]).get('closure_complete') is True)

    def test_next_release_and_receipt_do_not_require_already_removed_candidate(self):
        with self.guarded() as (api, git, path):
            cycle.finish_cycle(self.plan, 'candidate', 'd' * 40, 'backup', candidate_removed=True)
            api.assert_called_once_with('api', 'repos/choice/repo/git/refs', '--method', 'POST',
                                        '-f', 'ref=refs/heads/release-next', '-f', 'sha=' + 'a' * 40)
            git.assert_not_called()
            receipt = self.closure(path)
            self.assertEqual(receipt['backup_sha'], 'b' * 40)
            self.assertTrue(receipt['candidate_removed'])
            self.assertEqual(receipt['schema'], 2)
            self.assertEqual(receipt['next_release_requested'], 'release-next')
            self.assertEqual(receipt['deleted_branches'], self.plan['cleanup'])

    def test_promoted_candidate_removal_uses_exact_lease_and_keeps_backup(self):
        with self.guarded() as (api, git, path):
            cycle.remove_promoted_candidate(self.plan, 'candidate', 'd' * 40, 'backup')
            git.assert_called_once_with('push', '--force-with-lease=refs/heads/candidate:' + 'd' * 40,
                                        'origin', ':refs/heads/candidate')
            api.assert_not_called()

    def test_changed_staging_backup_or_candidate_blocks_candidate_removal(self):
        for branch in ('staging', 'backup', 'candidate'):
            with self.subTest(branch=branch), self.guarded(dict(self.refs, **{branch: 'f' * 40})) as (_, git, _):
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    cycle.remove_promoted_candidate(self.plan, 'candidate', 'd' * 40, 'backup')
                git.assert_not_called()

    def test_staging_move_during_checks_blocks_candidate_removal(self):
        with self.guarded() as (_, git, _), patch.object(cycle, 'remote_sha', side_effect=[
                'd' * 40, 'b' * 40, 'd' * 40, 'f' * 40]):
            with self.assertRaisesRegex(RuntimeError, 'Staging changed'):
                cycle.remove_promoted_candidate(self.plan, 'candidate', 'd' * 40, 'backup')
            git.assert_not_called()

    def test_explicit_candidate_retention_option_is_respected_after_promotion(self):
        self.plan['options']['remove_candidate'] = False
        with patch.object(cycle, 'revalidate'), \
             patch.object(cycle, 'prepare', return_value=('candidate', 'd' * 40, 'backup')), \
             patch.object(cycle, 'reset_staging'), patch.object(cycle, 'cleanup'), \
             patch.object(cycle, 'promotion_receipt') as receipt, \
             patch.object(cycle, 'remove_promoted_candidate') as remove, \
             patch.object(cycle, 'finish_cycle') as finish:
            cycle.execute(self.plan, False)
        remove.assert_not_called()
        receipt.assert_called_once_with(self.plan, 'candidate', 'd' * 40, 'backup', False)
        finish.assert_called_once_with(self.plan, 'candidate', 'd' * 40, 'backup', False)

    def test_default_options_keep_candidate_and_do_not_create_release(self):
        self.plan['options'] = dict(next_release='', remove_candidate=False)
        with self.guarded() as (api, git, path):
            cycle.finish_cycle(self.plan, 'candidate', 'd' * 40, 'backup')
            api.assert_not_called()
            git.assert_not_called()
            receipt = self.closure(path)
            self.assertFalse(receipt['candidate_removed'])

    def test_optional_creation_failure_preserves_closure_receipt_and_records_recovery(self):
        with self.guarded() as (api, git, path):
            def fail(*args):
                self.assertTrue(self.closure(path)['closure_complete'])
                raise subprocess.CalledProcessError(1, ['gh', 'api'], stderr='Reference update failed (HTTP 422)')
            api.side_effect = fail
            with self.assertRaisesRegex(RuntimeError, 'Release closure completed; next-release setup failed'):
                cycle.finish_cycle(self.plan, 'candidate', 'd' * 40, 'backup', True)
            result = json.loads(path.return_value.write_text.call_args.args[0])
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['branch'], 'release-next')
            git.assert_not_called()

    def test_timeout_keeps_completed_closure_and_skips_branch_creation(self):
        with self.guarded() as (api, _, path), \
             patch.object(creation, 'wait_for_ci', side_effect=RuntimeError('Timed out waiting for CI')):
            with self.assertRaisesRegex(RuntimeError, 'Release closure completed; next-release setup failed'):
                cycle.finish_cycle(self.plan, 'candidate', 'd' * 40, 'backup', True)
            self.assertTrue(self.closure(path)['closure_complete'])
            self.assertEqual(json.loads(path.return_value.write_text.call_args.args[0])['status'], 'failed')
            api.assert_not_called()

    def test_staging_or_backup_move_during_wait_blocks_followup_without_losing_closure(self):
        for branch in ('staging', 'backup'):
            refs = dict(self.refs)
            with self.subTest(branch=branch), self.guarded(refs) as (api, _, path), \
                 patch.object(creation, 'wait_for_ci', side_effect=lambda *args, **kwargs:
                              refs.update({branch: 'f' * 40})):
                with self.assertRaisesRegex(RuntimeError, 'changed after cleanup'):
                    cycle.finish_cycle(self.plan, 'candidate', 'd' * 40, 'backup', True)
                self.assertTrue(self.closure(path)['closure_complete'])
                api.assert_not_called()

    def test_changed_live_refs_block_all_followup_writes(self):
        for branch in self.refs:
            refs = dict(self.refs, **{branch: 'f' * 40})
            with self.subTest(branch=branch), self.guarded(refs) as (api, git, path):
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    cycle.finish_cycle(self.plan, 'candidate', 'd' * 40, 'backup')
                api.assert_not_called()
                git.assert_not_called()
                path.return_value.write_text.assert_not_called()

    def test_existing_next_release_is_never_moved_or_adopted(self):
        with self.guarded(existing=[dict(ref='refs/heads/release-next')]) as (api, git, path):
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                cycle.finish_cycle(self.plan, 'candidate', 'd' * 40, 'backup')
            api.assert_not_called()
            git.assert_not_called()

    def test_invalid_or_completed_next_release_names_are_rejected(self):
        for branch in ('main', 'release-', 'release-old', 'release-next:bad', 'release-next/..'):
            with self.subTest(branch=branch), self.assertRaises(RuntimeError):
                cycle.validate_next_release(branch, 'release-old')

    def test_failed_release_cleanup_does_not_prevent_promoted_candidate_removal(self):
        with patch.object(cycle, 'revalidate'), \
             patch.object(cycle, 'prepare', return_value=('candidate', 'd' * 40, 'backup')), \
             patch.object(cycle, 'reset_staging'), \
             patch.object(cycle, 'promotion_receipt') as receipt, \
             patch.object(cycle, 'remove_promoted_candidate') as remove, \
             patch.object(cycle, 'cleanup', side_effect=RuntimeError('Deletion rejected')), \
             patch.object(cycle, 'finish_cycle') as finish:
            with self.assertRaisesRegex(RuntimeError, 'Deletion rejected'):
                cycle.execute(self.plan, False)
            finish.assert_not_called()
            remove.assert_called_once_with(self.plan, 'candidate', 'd' * 40, 'backup')
            self.assertEqual([call.args[-1] for call in receipt.call_args_list], [False, True])

    def test_failed_staging_promotion_keeps_candidate_and_skips_release_cleanup(self):
        with patch.object(cycle, 'revalidate'), \
             patch.object(cycle, 'prepare', return_value=('candidate', 'd' * 40, 'backup')), \
             patch.object(cycle, 'reset_staging', side_effect=RuntimeError('Promotion rejected')), \
             patch.object(cycle, 'remove_promoted_candidate') as remove, \
             patch.object(cycle, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'Promotion rejected'):
                cycle.execute(self.plan, False)
            remove.assert_not_called()
            cleanup.assert_not_called()

    def test_preparation_only_prevents_followup_even_when_requested(self):
        with patch.object(cycle, 'revalidate'), \
             patch.object(cycle, 'prepare', return_value=('candidate', 'd' * 40, 'backup')), \
             patch.object(cycle, 'reset_staging') as reset, \
             patch.object(cycle, 'cleanup') as cleanup, \
             patch.object(cycle, 'remove_promoted_candidate') as remove, \
             patch.object(cycle, 'finish_cycle') as finish:
            cycle.execute(self.plan, True)
            reset.assert_not_called()
            cleanup.assert_not_called()
            finish.assert_not_called()
            remove.assert_not_called()

    def test_deleted_release_has_specific_assessment_error(self):
        with patch.object(cycle, 'get_latest_release', return_value='release-new'), \
             patch.object(cycle, 'get_cleanup_candidates', return_value=(False, [
                 dict(branch='release-old', sha=None, status='Keep', reason='Branch already deleted or unavailable')])):
            with self.assertRaisesRegex(RuntimeError, 'already deleted or unavailable'):
                cycle.snapshot('choice/repo', 'release-old')

    def test_older_release_cannot_request_new_cycle_actions(self):
        with patch.object(cycle, 'get_latest_release', return_value='release-new'), \
             patch.object(cycle, 'get_cleanup_candidates', return_value=(True, [
                 dict(branch='release-old', sha='c' * 40, status='Cleanup candidate', reason='Verified')])), \
             patch.object(cycle, 'git', return_value='a' * 40):
            with self.assertRaisesRegex(RuntimeError, 'Older-release cleanup cannot'):
                cycle.snapshot('choice/repo', 'release-old', self.plan['options'])

    def test_older_release_normalises_default_candidate_removal_to_false(self):
        with patch.object(cycle, 'get_latest_release', return_value='release-new'), \
             patch.object(cycle, 'get_cleanup_candidates', return_value=(True, [
                 dict(branch='release-old', sha='c' * 40, status='Cleanup candidate', reason='Verified')])), \
             patch.object(cycle, 'git', return_value='a' * 40):
            plan = cycle.snapshot('choice/repo', 'release-old', dict(next_release='', remove_candidate=True))
        self.assertFalse(plan['prepare'])
        self.assertFalse(plan['options']['remove_candidate'])

    def test_assessment_reports_excluded_branches_and_reason(self):
        excluded = dict(branch='fix/DEV-215', sha='d' * 40, status='Needs review', reason='Squash differs')
        with patch.object(cycle, 'get_latest_release', return_value='release-old'), \
             patch.object(cycle, 'get_cleanup_candidates', return_value=(True, [
                 dict(branch='release-old', sha='c' * 40, status='Cleanup candidate', reason='Verified'), excluded])), \
             patch.object(cycle, 'git', return_value='a' * 40):
            plan = cycle.snapshot('choice/repo', 'release-old')
        self.assertIn(excluded, plan['assessments'])
        self.assertNotIn('fix/DEV-215', plan['cleanup'])

    def test_deployed_labelled_revision_is_not_selected_for_carryover(self):
        pr = dict(number=36, merged_at='2026-11-01T00:00:00Z', base={'ref': 'staging'},
                  head={'ref': 'feature/DEV-212', 'repo': {'full_name': 'choice/repo'}})
        def paginated(endpoint):
            return [dict(number=36, pull_request={})] if '/issues?' in endpoint else [{'sha': 'a' * 40}]
        with patch.object(carry, 'REPO', 'choice/repo'), \
             patch.object(carry, 'gh_api_paginated', side_effect=paginated), \
             patch.object(carry, 'gh_api', return_value=pr), \
             patch.object(carry, 'revision_is_released', return_value=True) as verify:
            self.assertEqual(carry.selected_carryover_prs(), {})
            verify.assert_called_once_with('choice/repo', 'feature/DEV-212', 'a' * 40)


if __name__ == '__main__':
    unittest.main()
