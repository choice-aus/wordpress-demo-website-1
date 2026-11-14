"""Release safety regression tests; remote services and writes are mocked."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import branch_audit
import release_cycle as cycle
import release_evidence
import staging_reset_check as check
import staging_reset_plan as plan
import uat_carryover as carry
import verify_next_cycle as verify


class GitEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, self.previous)
        self.addCleanup(self.directory.cleanup)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        Path('feature.txt').write_text('start\n')
        self.commit('base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-c', 'feature')
        self.commits = []
        for index in range(4):
            with Path('feature.txt').open('a') as output:
                output.write(f'change {index}\n')
            self.commit(f'feature {index}')
            self.commits.append(self.git('rev-parse', 'HEAD'))
        self.head = self.commits[-1]
        self.git('switch', '-c', 'release-test', self.base)
        self.git('merge', '--squash', 'feature')
        self.commit('squash')
        self.squash = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')
        self.git('merge', '--no-ff', 'release-test', '-m', 'release')
        self.main = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', self.main)
        self.git('update-ref', 'refs/remotes/origin/release-test', self.squash)
        self.pr = self.pr_data(55, 'feature/test', 'release-test', self.head, self.squash)
        self.release_pr = self.pr_data(56, 'release-test', 'main', self.squash, self.main)

    def git(self, *args):
        return subprocess.check_output(['git', *args], stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self, message):
        self.git('add', '.')
        self.git('commit', '-m', message)

    def pr_data(self, number, head_ref, base_ref, head, merge):
        return dict(number=number, merged_at='2026-11-13T06:21:06Z', state='closed',
                    head={'ref': head_ref, 'sha': head, 'repo': {'full_name': 'choice/repo'}},
                    base={'ref': base_ref, 'repo': {'full_name': 'choice/repo'}},
                    merge_commit_sha=merge)

    def test_four_commits_combined_by_squash(self):
        self.assertTrue(release_evidence.verify_promoted_pr(self.pr))
        self.assertIsNotNone(check.verify_release_chain(self.pr, [self.pr, self.release_pr]))
        self.assertTrue(branch_audit.verified_release_pr(self.release_pr, 'choice/repo', 'release-test'))

    def test_partial_squash_is_rejected(self):
        self.git('switch', '-c', 'partial', self.base)
        self.git('cherry-pick', self.commits[0])
        partial = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', partial)
        altered = dict(self.pr, merge_commit_sha=partial)
        self.assertFalse(release_evidence.verify_promoted_pr(altered))

    def test_whitespace_change_is_not_ignored(self):
        self.git('switch', '-c', 'different', self.squash)
        Path('feature.txt').write_text(Path('feature.txt').read_text().replace('change 0', 'change  0'))
        self.git('add', '.')
        self.git('commit', '--amend', '--no-edit')
        changed = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', changed)
        self.assertFalse(release_evidence.verify_promoted_pr(dict(self.pr, merge_commit_sha=changed)))

    def test_squash_must_be_in_expected_main(self):
        self.assertFalse(release_evidence.verify_promoted_pr(self.pr, self.base))

    def test_release_chain_requires_release_merge_commit(self):
        altered = dict(self.release_pr, merge_commit_sha=self.squash)
        self.assertIsNone(check.verify_release_chain(self.pr, [self.pr, altered]))
        self.assertFalse(branch_audit.verified_release_pr(altered, 'choice/repo', 'release-test'))

    def test_only_exact_promoted_commit_set_is_released(self):
        self.git('switch', 'feature')
        Path('unreleased.txt').write_text('must survive\n')
        self.commit('unreleased')
        extra = self.git('rev-parse', 'HEAD')
        prs = [self.pr, self.release_pr]
        with patch.object(plan, 'run', return_value='choice/repo'), \
             patch.object(plan, 'merged_pull_requests', return_value=prs), \
             patch.object(check, 'github_paginated', return_value=[{'sha': c} for c in self.commits]):
            results = plan.classify_staging_commits('feature')
        self.assertTrue(all(results[c] == 'verified' for c in self.commits))
        self.assertEqual(results[extra], 'no_release_evidence')
        with patch.object(check, 'github_paginated', return_value=[{'sha': self.head}]):
            self.assertEqual(plan.released_feature_commits('choice/repo', prs), {})

    def test_feature_tip_moved_after_release_is_kept(self):
        self.git('update-ref', 'refs/remotes/origin/feature/test', self.head)
        self.assertEqual(branch_audit.classify('feature/test', [self.pr], 'release-test', True)[0],
                         'Cleanup candidate')
        self.git('switch', 'feature')
        Path('extra').write_text('unreleased')
        self.commit('new')
        self.git('update-ref', 'refs/remotes/origin/feature/test', self.git('rev-parse', 'HEAD'))
        self.assertEqual(branch_audit.classify('feature/test', [self.pr], 'release-test', True)[0],
                         'Needs review')

    def test_binary_and_mode_changes_are_verified_exactly(self):
        self.git('switch', '-c', 'binary-feature', self.base)
        Path('asset.bin').write_bytes(b'\0\x01\xff')
        Path('run.sh').write_text('#!/bin/sh\nexit 0\n')
        Path('run.sh').chmod(0o755)
        self.commit('binary and mode')
        head = self.git('rev-parse', 'HEAD')
        self.git('switch', '-c', 'binary-release', self.base)
        self.git('merge', '--squash', 'binary-feature')
        self.commit('binary squash')
        squash = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', squash)
        self.assertTrue(release_evidence.verify_promoted_pr(dict(self.pr, head={'sha': head}, merge_commit_sha=squash)))
        Path('run.sh').chmod(0o644)
        self.git('add', '.')
        self.git('commit', '--amend', '--no-edit')
        changed = self.git('rev-parse', 'HEAD')
        self.git('update-ref', 'refs/remotes/origin/main', changed)
        self.assertFalse(release_evidence.verify_promoted_pr(dict(self.pr, head={'sha': head}, merge_commit_sha=changed)))

    def test_carryover_preserves_success_when_another_conflicts(self):
        self.git('branch', '-m', 'feature', 'original-feature')
        self.git('switch', '-c', 'feature/good', self.base)
        Path('good.txt').write_text('keep me\n')
        self.commit('good carryover')
        good = self.git('rev-parse', 'HEAD')
        self.git('switch', '-c', 'feature/bad', self.base)
        Path('feature.txt').write_text('conflicting replacement\n')
        self.commit('conflicting carryover')
        bad = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')
        self.git('branch', 'candidate')
        selected = {
            'feature/good': dict(number=12, branch='feature/good', tested_sha=good),
            'feature/bad': dict(number=13, branch='feature/bad', tested_sha=bad),
        }
        real_run = carry.run
        pushes = []
        def guarded_run(*args, **kwargs):
            if args[:2] == ('git', 'push'):
                pushes.append(args)
                return subprocess.CompletedProcess(args, 0, '', '')
            return real_run(*args, **kwargs)
        with tempfile.TemporaryDirectory() as remote:
            self.git('clone', '--bare', '.', remote)
            self.git('remote', 'add', 'origin', remote)
            with patch.dict(os.environ, dict(CANDIDATE_BRANCH='candidate', GITHUB_STEP_SUMMARY='summary.txt')), \
                 patch.object(carry, 'selected_carryover_prs', return_value=selected), \
                 patch.object(carry, 'run', side_effect=guarded_run):
                self.assertEqual(carry.main(), 0)
        manifest = json.loads(Path('carryover-manifest.json').read_text())
        self.assertEqual([item['pr'] for item in manifest['carryovers']], [12])
        self.assertEqual([item['pr'] for item in manifest['failed']], [13])
        self.assertIn('Merge conflict', manifest['failed'][0]['reason'])
        self.assertEqual(len(pushes), 1)
        self.assertIn('--force-with-lease=refs/heads/candidate:' + self.main, pushes[0])
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertIsNone(carry.WORKTREE)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.plan = dict(repository='choice/repo', release='release-new', latest='release-new',
                         prepare=True, main='a' * 40, staging='b' * 40,
                         release_sha='c' * 40, cleanup={'release-new': 'c' * 40, 'feature/new': 'd' * 40})

    def test_older_release_never_prepares_or_resets(self):
        older = dict(self.plan, release='release-old', prepare=False)
        with patch.object(cycle, 'revalidate'), patch.object(cycle, 'prepare') as prepare, \
             patch.object(cycle, 'reset_staging') as reset, patch.object(cycle, 'cleanup') as cleanup:
            cycle.execute(older, preparation_only=False)
            prepare.assert_not_called()
            reset.assert_not_called()
            cleanup.assert_called_once_with(older, None)

    def test_preparation_only_prevents_all_destructive_actions(self):
        for latest in (True, False):
            selected = dict(self.plan, prepare=latest)
            with patch.object(cycle, 'revalidate'), \
                 patch.object(cycle, 'prepare', return_value=('candidate', 'e' * 40, 'backup')) as prepare, \
                 patch.object(cycle, 'reset_staging') as reset, patch.object(cycle, 'cleanup') as cleanup:
                cycle.execute(selected, preparation_only=True)
                self.assertEqual(prepare.call_count, int(latest))
                reset.assert_not_called()
                cleanup.assert_not_called()

    def test_latest_release_prepares_resets_then_cleans(self):
        events = []
        with patch.object(cycle, 'revalidate'), \
             patch.object(cycle, 'prepare', side_effect=lambda p: (events.append('prepare') or ('candidate', 'e' * 40, 'backup'))), \
             patch.object(cycle, 'reset_staging', side_effect=lambda *a: events.append('reset')), \
             patch.object(cycle, 'cleanup', side_effect=lambda *a: events.append('cleanup')):
            cycle.execute(self.plan, False)
        self.assertEqual(events, ['prepare', 'reset', 'cleanup'])

    def test_every_approved_sha_and_release_eligibility_is_rechecked(self):
        for field in ('main', 'staging', 'release_sha', 'latest', 'prepare', 'cleanup'):
            changed = copy.deepcopy(self.plan)
            changed[field] = 'changed'
            with self.subTest(field=field), patch.object(cycle, 'snapshot', return_value=changed), \
                 patch.object(cycle, 'approval_environment'):
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    cycle.revalidate(self.plan)

    def test_changed_candidate_or_backup_blocks_reset(self):
        for refs in (['changed'], ['e' * 40, 'changed']):
            with patch.object(cycle, 'revalidate'), patch.object(cycle, 'open_staging_pr_count', return_value=0), \
                 patch.object(cycle, 'remote_sha', side_effect=refs), patch.object(cycle, 'git') as git:
                with self.assertRaisesRegex(RuntimeError, 'SHA changed'):
                    cycle.reset_staging(self.plan, 'candidate', 'e' * 40, 'backup')
                git.assert_not_called()

    def test_reset_uses_exact_staging_lease(self):
        with patch.object(cycle, 'revalidate'), patch.object(cycle, 'open_staging_pr_count', return_value=0), \
             patch.object(cycle, 'remote_sha', side_effect=['e' * 40, self.plan['staging']]), \
             patch.object(cycle.verify_next_cycle, 'main', return_value=0), \
             patch.object(cycle, 'verify_remote_repository'), patch.object(cycle, 'git') as git:
            cycle.reset_staging(self.plan, 'candidate', 'e' * 40, 'backup')
            git.assert_called_once_with('push', '--force-with-lease=refs/heads/staging:' + 'b' * 40,
                                        'origin', 'e' * 40 + ':refs/heads/staging')

    def test_cleanup_uses_atomic_push_and_individual_leases(self):
        with patch.object(cycle, 'revalidate'), patch.object(cycle, 'verify_remote_repository'), \
             patch.object(cycle.subprocess, 'run') as run:
            cycle.cleanup(self.plan)
        command = run.call_args.args[0]
        self.assertIn('--atomic', command)
        for branch, sha in self.plan['cleanup'].items():
            self.assertIn(f'--force-with-lease=refs/heads/{branch}:{sha}', command)
            self.assertIn(f':refs/heads/{branch}', command)

    def test_existing_backup_is_never_overwritten(self):
        with patch.object(cycle, 'github', return_value=[
            {'ref': 'refs/heads/backup/test', 'object': {'sha': 'different'}}
        ]) as api:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected existing SHA'):
                cycle.create_or_verify('choice/repo', 'backup/test', 'b' * 40)
            self.assertEqual(api.call_count, 1)

    def test_snapshot_marks_older_release_cleanup_only(self):
        with patch.object(cycle, 'get_latest_release', return_value='release-new'), \
             patch.object(cycle, 'get_cleanup_candidates', return_value=(True, [
                 dict(branch='release-old', sha='c' * 40, status='Cleanup candidate'),
                 dict(branch='feature/DEV-212', sha='d' * 40, status='Keep'),
             ])), patch.object(cycle, 'git', return_value='a' * 40):
            result = cycle.snapshot('choice/repo', 'release-old')
        self.assertFalse(result['prepare'])
        self.assertEqual(result['cleanup'], {'release-old': 'c' * 40})

    def test_failed_revalidation_prevents_any_preparation_or_deletion(self):
        with patch.object(cycle, 'revalidate', side_effect=RuntimeError('SHA changed')), \
             patch.object(cycle, 'prepare') as prepare, patch.object(cycle, 'cleanup') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'SHA changed'):
                cycle.execute(self.plan, False)
            prepare.assert_not_called()
            cleanup.assert_not_called()

    def test_missing_reviewer_rule_fails_closed(self):
        with patch.object(cycle, 'github', return_value={'protection_rules': []}):
            with self.assertRaisesRegex(RuntimeError, 'required reviewers'):
                cycle.approval_environment('choice/repo')


class CarryoverSelectionTests(unittest.TestCase):
    def test_latest_merged_revision_wins_over_higher_pr_number(self):
        old = dict(number=20, merged_at='2026-10-01', base={'ref': 'staging'},
                   head={'ref': 'feature/test', 'repo': {'full_name': 'choice/repo'}})
        new = dict(old, number=10, merged_at='2026-11-01')
        def paginated(endpoint):
            if '/issues?' in endpoint:
                return [dict(number=n, pull_request={}) for n in (20, 10)]
            return [{'sha': 'a' * 40}]
        with patch.object(carry, 'REPO', 'choice/repo'), \
             patch.object(carry, 'gh_api_paginated', side_effect=paginated), \
             patch.object(carry, 'gh_api', side_effect=[old, new]):
            self.assertEqual(carry.selected_carryover_prs()['feature/test']['number'], 10)

    def test_fork_pr_cannot_supply_carryover(self):
        fork = dict(number=20, merged_at='2026-10-01', base={'ref': 'staging'},
                    head={'ref': 'feature/test', 'repo': {'full_name': 'outsider/fork'}})
        with patch.object(carry, 'REPO', 'choice/repo'), \
             patch.object(carry, 'gh_api_paginated', return_value=[dict(number=20, pull_request={})]), \
             patch.object(carry, 'gh_api', return_value=fork):
            self.assertEqual(carry.selected_carryover_prs(), {})


class VerificationTests(unittest.TestCase):
    def run_verification(self, failed=False, changed=None, unknown=False):
        main, staging, candidate, tested = ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40)
        selected = {'feature/DEV-212': {'number': 12, 'branch': 'feature/DEV-212', 'tested_sha': tested}}
        item = {'pr': 12, 'branch': 'feature/DEV-212', 'tested_sha': tested, 'reason': 'Merge conflict: example'}
        manifest = dict(candidate='candidate', candidate_sha=candidate,
                        carryovers=[] if failed else [item], failed=[item] if failed else [])
        refs = {'origin/main': main, 'origin/staging': staging, 'origin/candidate': candidate, 'origin/backup': staging}
        if changed:
            refs[changed] = 'f' * 40
        def git(*args):
            if args[0] == 'rev-parse':
                return refs[args[1]]
            if args[0] == 'rev-list':
                return '9' * 40 if unknown else ''
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as directory:
            manifest_file = Path(directory) / 'carryover-manifest.json'
            manifest_file.write_text(json.dumps(manifest))
            with patch.dict(os.environ, dict(EXPECTED_MAIN=main, EXPECTED_STAGING=staging,
                                             CANDIDATE_BRANCH='candidate', BACKUP_BRANCH='backup')), \
                 patch('builtins.open', side_effect=lambda *a, **kw: manifest_file.open(*a[1:], **kw)), \
                 patch.object(verify.subprocess, 'run'), patch.object(verify, 'git', side_effect=git), \
                 patch.object(verify, 'selected_carryover_prs', return_value=selected), \
                 patch.object(verify, 'is_ancestor', side_effect=lambda a,b: a != '9' * 40 if unknown else True), \
                 patch.object(verify, 'classify_staging_commits', return_value={}), \
                 patch.object(verify, 'run', return_value='choice/repo'), \
                 patch.object(verify, 'merged_pull_requests', return_value=[]), \
                 patch.object(verify, 'staging_feature_commits', return_value={}), \
                 patch.object(check, 'check_merge_commits', return_value=([], [])), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                result = verify.main()
        return result, output.getvalue()

    def test_dev212_carryover_is_preserved(self):
        self.assertEqual(self.run_verification()[0], 0)

    def test_failed_carryover_blocks_reset_even_without_unresolved_commits(self):
        result, report = self.run_verification(failed=True)
        self.assertEqual(result, 1)
        self.assertIn('MANUAL FOLLOW-UP: PR #12', report)
        self.assertIn('Merge conflict: example', report)

    def test_unknown_unreleased_commit_blocks_reset(self):
        result, report = self.run_verification(unknown=True)
        self.assertEqual(result, 1)
        self.assertIn('Unaccounted staging commits: 1', report)

    def test_changed_live_refs_block_verification(self):
        for ref in ('origin/main', 'origin/staging', 'origin/candidate', 'origin/backup'):
            with self.subTest(ref=ref):
                self.assertEqual(self.run_verification(changed=ref)[0], 1)


if __name__ == '__main__':
    unittest.main()
