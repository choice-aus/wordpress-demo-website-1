"""Retention decisions and leased deletions against temporary local repositories."""

import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import recovery_retention as retention


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 18, tzinfo=timezone.utc)

    def case(self, ages=(1, 10, 40, 60, 90), preserved=True, protected=()):
        refs = dict(main='a' * 40, staging='a' * 40)
        cycles = []
        for number, age in enumerate(ages, 1):
            backup_sha = f'{number:040x}'
            candidate_sha = f'{number + 100:040x}'
            cycle = dict(run_id=str(number), run_attempt='1', source='closure-receipt',
                         completed_at=(self.now - timedelta(days=age)).isoformat(),
                         backup='backup/staging-before-reset-' + backup_sha[:12],
                         backup_sha=backup_sha, candidate=f'staging-next-cycle-{number}-1',
                         candidate_sha=candidate_sha)
            # Distinct realistic backup prefixes, not zero-padded integers.
            cycle['backup_sha'] = str(number) * 40
            cycle['backup'] = 'backup/staging-before-reset-' + str(number) * 12
            refs[cycle['backup']] = cycle['backup_sha']
            refs[cycle['candidate']] = candidate_sha
            cycles.append(cycle)
        proof = Mock(return_value=preserved)
        plan = retention.build_plan('choice/repo', refs, cycles, set(protected),
                                    self.now.isoformat(), proof, scope='backups')
        return plan, cycles, proof

    def test_keeps_latest_three_cycles_and_only_prunes_proven_older_refs(self):
        plan, cycles, proof = self.case()
        expected = {cycle['backup'] for cycle in cycles[3:]}
        self.assertEqual(set(plan['cleanup']), expected)
        self.assertNotIn('main', plan['cleanup'])
        self.assertNotIn('staging', plan['cleanup'])
        self.assertEqual(proof.call_count, 2)

    def test_thirty_day_minimum_overrides_cycle_count(self):
        plan, _, proof = self.case(ages=(1, 2, 3, 10))
        self.assertEqual(plan['cleanup'], {})
        proof.assert_not_called()

    def test_missing_content_proof_retains_old_recovery_refs(self):
        plan, _, _ = self.case(preserved=False)
        self.assertEqual(plan['cleanup'], {})

    def test_legacy_refs_and_failed_shared_backups_are_preserved(self):
        initial, cycles, _ = self.case()
        refs = dict(initial['refs'], **{'staging-next-cycle': 'f' * 40,
                                      'backup/staging-before-dev-214': 'e' * 40})
        protected = {cycles[-1]['backup'], cycles[-1]['candidate']}
        plan = retention.build_plan('choice/repo', refs, cycles, protected,
                                    self.now.isoformat(), lambda sha: True)
        self.assertTrue(protected.isdisjoint(plan['cleanup']))
        self.assertNotIn('staging-next-cycle', plan['cleanup'])
        self.assertNotIn('backup/staging-before-dev-214', plan['cleanup'])

    def test_changed_ref_is_not_pruned_using_old_receipt(self):
        initial, cycles, _ = self.case()
        refs = dict(initial['refs'])
        changed = cycles[-1]['backup']
        refs[changed] = 'f' * 40
        plan = retention.build_plan('choice/repo', refs, cycles, set(),
                                    self.now.isoformat(), lambda sha: True)
        self.assertNotIn(changed, plan['cleanup'])

    def test_cycle_count_cannot_be_reduced_below_agreed_minimum(self):
        with self.assertRaisesRegex(RuntimeError, 'at least 3 successful cycles'):
            retention.build_plan('choice/repo', {}, [], set(), self.now.isoformat(), lambda sha: True,
                                 keep_cycles=1, min_days=3)

    def test_demo_age_override_keeps_latest_three_cycles(self):
        normal, cycles, _ = self.case(ages=(1, 2, 3, 4))
        self.assertEqual(normal['min_days'], 30)
        self.assertEqual(normal['cleanup'], {})
        demo = retention.build_plan('choice/repo', normal['refs'], cycles, set(), self.now.isoformat(),
                                    lambda sha: True, min_days=3)
        self.assertEqual(demo['min_days'], 3)
        self.assertEqual(set(demo['cleanup']), {cycles[-1]['backup']})
        self.assertTrue(all(cycle['backup'] not in demo['cleanup'] and cycle['candidate'] not in demo['cleanup']
                            for cycle in cycles[:3]))

    def test_invalid_retention_days_fail_before_reading_remote_state(self):
        for days in (0, -1, 1.5, '3', True):
            with self.subTest(days=days), patch.object(retention, 'recovery_refs') as refs:
                with self.assertRaisesRegex(RuntimeError, 'positive whole number'):
                    retention.assess('choice/repo', self.now.isoformat(), 3, days)
                refs.assert_not_called()

    def test_execution_revalidates_recorded_demo_policy_instead_of_normal_default(self):
        normal, cycles, _ = self.case(ages=(1, 2, 3, 4))
        demo = retention.build_plan('choice/repo', normal['refs'], cycles, set(), self.now.isoformat(),
                                    lambda sha: True, min_days=3)
        with patch.dict(os.environ, GITHUB_REPOSITORY='choice/repo'), \
             patch.object(retention, 'approval_environment'), \
             patch.object(retention, 'assess', return_value=demo) as assess, \
             patch.object(retention, 'verify_remote_repository'), \
             patch.object(retention.subprocess, 'run') as run:
            retention.execute(demo)
            assess.assert_called_once_with('choice/repo', demo['assessed_at'], 3, 3, 'all', False)
            self.assertIn('--atomic', run.call_args.args[0])

    def test_changed_snapshot_blocks_all_deletion(self):
        plan, _, _ = self.case()
        current = copy.deepcopy(plan)
        current['refs']['staging'] = 'b' * 40
        with patch.dict(os.environ, GITHUB_REPOSITORY='choice/repo'), \
             patch.object(retention, 'approval_environment'), \
             patch.object(retention, 'assess', return_value=current), \
             patch.object(retention.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'changed'):
                retention.execute(plan)
            run.assert_not_called()

    def test_no_candidates_causes_no_push(self):
        plan, _, _ = self.case(ages=(1, 2, 3))
        with patch.dict(os.environ, GITHUB_REPOSITORY='choice/repo'), \
             patch.object(retention, 'approval_environment'), \
             patch.object(retention, 'assess', return_value=plan), \
             patch.object(retention.subprocess, 'run') as run:
            retention.execute(plan)
            run.assert_not_called()

    def legacy_files(self):
        self.backup_sha = 'b' * 40
        self.candidate_sha = 'a' * 40
        plan = dict(repository='choice/repo', release='release-old', prepare=True,
                    main='a' * 40, staging=self.backup_sha,
                    cleanup={'feature/old': 'c' * 40, 'release-old': 'd' * 40})
        manifest = dict(candidate='staging-next-cycle-123-1', candidate_sha=self.candidate_sha,
                        carryovers=[], failed=[])
        result = ('Staging promoted to verified candidate staging-next-cycle-123-1 at ' + self.candidate_sha
                  + '\nDeleted verified branches: feature/old, release-old\n')
        return {'release-cycle-plan.json': json.dumps(plan).encode(),
                'carryover-manifest.json': json.dumps(manifest).encode(),
                'release-cycle-result.txt': result.encode()}

    def test_existing_successful_cycle_artifacts_can_supply_legacy_proof(self):
        decoded = retention.decode_cycle('choice/repo', 123, 1, self.legacy_files(), self.now.isoformat())
        self.assertEqual(decoded['source'], 'legacy-full-success-evidence')
        self.assertEqual(decoded['backup_sha'], self.backup_sha)
        self.assertEqual(decoded['candidate_sha'], self.candidate_sha)

    def test_preparation_only_or_partial_legacy_result_is_not_success_evidence(self):
        files = self.legacy_files()
        for output in (b'Preparation only: staging reset and branch deletion disabled\n',
                       b'Staging promoted; cleanup failed\n'):
            files['release-cycle-result.txt'] = output
            with self.assertRaises(RuntimeError):
                retention.decode_cycle('choice/repo', 123, 1, files, self.now.isoformat())

    def test_receipt_run_and_sha_names_must_match_artifact(self):
        files = self.legacy_files()
        manifest = json.loads(files['carryover-manifest.json'])
        manifest['candidate'] = 'staging-next-cycle-other'
        files['carryover-manifest.json'] = json.dumps(manifest).encode()
        with self.assertRaises(RuntimeError):
            retention.decode_cycle('choice/repo', 123, 1, files, self.now.isoformat())

    def test_download_failure_does_not_authorise_deletion_or_log_signed_url(self):
        error = subprocess.CalledProcessError(1, ['gh', 'api'], stderr=b'https://storage.invalid/?sig=secret')
        with patch.object(retention.subprocess, 'check_output', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'No deletion is authorised') as raised:
                retention.read_evidence('choice/repo', dict(id=123, size_in_bytes=100))
            self.assertNotIn('sig=secret', str(raised.exception))

    def test_failed_attempt_marks_shared_backup_as_protected(self):
        files = self.legacy_files()
        metadata = [dict(id=123)]
        artifact = dict(name='release-cycle-evidence-1', expired=False, id=1,
                        created_at=self.now.isoformat(), size_in_bytes=100)
        with patch.object(retention, 'paginated', side_effect=[metadata, [artifact]]), \
             patch.object(retention, 'read_evidence', return_value=files), \
             patch.object(retention, 'github', return_value=dict(conclusion='failure')):
            cycles, protected = retention.completed_cycles('choice/repo')
        self.assertEqual(cycles, [])
        self.assertIn('staging-next-cycle-123-1', protected)
        self.assertIn('backup/staging-before-reset-' + 'b' * 12, protected)

    def cycle_files(self, run_id, preparation_only=False):
        files = self.legacy_files()
        plan = json.loads(files['release-cycle-plan.json'])
        plan['release_sha'] = 'd' * 40
        files['release-cycle-plan.json'] = json.dumps(plan).encode()
        manifest = json.loads(files['carryover-manifest.json'])
        manifest['candidate'] = f'staging-next-cycle-{run_id}-1'
        files['carryover-manifest.json'] = json.dumps(manifest).encode()
        output = files['release-cycle-result.txt'].decode().replace('123-1', str(run_id) + '-1')
        if preparation_only:
            output = 'Preparation only: staging reset and branch deletion disabled\n'
        files['release-cycle-result.txt'] = output.encode()
        return files

    def shared_hold_case(self, hold_status='completed', hold_conclusion='success',
                         hold_updated='2026-11-01T00:00:00Z', mutate_hold=None,
                         mutate_success=None, extra_hold=False):
        held = self.cycle_files(100, preparation_only=True)
        success = self.cycle_files(200)
        if mutate_hold:
            mutate_hold(held)
        if mutate_success:
            mutate_success(success)
        runs = [dict(id=100), dict(id=200)]
        files = {100: held, 200: success}
        if extra_hold:
            runs.append(dict(id=300))
            files[300] = self.cycle_files(300, preparation_only=True)

        def pages(endpoint):
            if endpoint.endswith('/runs'):
                return runs
            run_id = int(endpoint.split('/')[-2])
            return [dict(name='release-cycle-evidence-1', expired=False, id=run_id,
                         created_at=('2026-11-02T00:00:00Z' if run_id == 200 else hold_updated),
                         size_in_bytes=100)]

        def attempt(*args):
            run_id = int(args[1].split('/')[-3])
            if run_id == 300:
                return dict(status='waiting', conclusion=None, updated_at=hold_updated)
            return (dict(status='completed', conclusion='success', updated_at='2026-11-02T00:01:00Z')
                    if run_id == 200 else dict(status=hold_status, conclusion=hold_conclusion,
                                               updated_at=hold_updated))

        with patch.object(retention, 'paginated', side_effect=pages), \
             patch.object(retention, 'read_evidence', side_effect=lambda repo, artifact: files[artifact['id']]), \
             patch.object(retention, 'github', side_effect=attempt):
            return retention.completed_cycles('choice/repo')

    def test_later_verified_closure_resolves_preparation_only_or_failed_backup_hold(self):
        for conclusion in ('success', 'failure', 'cancelled'):
            with self.subTest(conclusion=conclusion):
                cycles, protected = self.shared_hold_case(hold_conclusion=conclusion)
                self.assertEqual(len(cycles), 1)
                self.assertNotIn('backup/staging-before-reset-' + 'b' * 12, protected)
                self.assertIn('staging-next-cycle-100-1', protected)

    def test_active_later_undated_or_naive_dated_use_keeps_shared_backup(self):
        cases = (dict(hold_status='in_progress', hold_conclusion=None),
                 dict(hold_status='waiting', hold_conclusion=None),
                 dict(hold_updated='2026-11-03T00:00:00Z'),
                 dict(hold_updated='2026-11-02T00:00:00Z'),
                 dict(hold_updated=None), dict(hold_updated='2026-11-01T00:00:00'))
        for case in cases:
            with self.subTest(case=case):
                _, protected = self.shared_hold_case(**case)
                self.assertIn('backup/staging-before-reset-' + 'b' * 12, protected)

    def test_hold_requires_exact_release_revision_backup_sha_and_repository(self):
        def change_plan(field, value):
            def mutate(files):
                plan = json.loads(files['release-cycle-plan.json'])
                plan[field] = value
                files['release-cycle-plan.json'] = json.dumps(plan).encode()
            return mutate
        for field, value in (('release', 'release-other'), ('release_sha', 'e' * 40),
                             ('staging', 'b' * 12 + 'e' * 28), ('repository', 'other/repo'),
                             ('release_sha', 'unknown')):
            with self.subTest(field=field, value=value):
                _, protected = self.shared_hold_case(mutate_hold=change_plan(field, value))
                self.assertIn('backup/staging-before-reset-' + 'b' * 12, protected)

    def test_any_unresolved_shared_use_keeps_backup(self):
        # A completed retry for the original hold is not proof for another active use.
        cycles, protected = self.shared_hold_case(extra_hold=True)
        self.assertEqual(len(cycles), 1)
        self.assertIn('backup/staging-before-reset-' + 'b' * 12, protected)

    def test_lifting_hold_still_keeps_latest_three_cycles(self):
        cycles, protected = self.shared_hold_case()
        cycle = cycles[0]
        refs = dict(main='a' * 40, staging='a' * 40, **{cycle['backup']: cycle['backup_sha']})
        # A resolved hold does not itself authorize deletion of a recent cycle.
        plan = retention.build_plan('choice/repo', refs, cycles, protected, self.now.isoformat(),
                                    lambda sha: True, min_days=1, scope='backups')
        self.assertEqual(plan['cleanup'], {})
        self.assertIn('latest 3', plan['assessments'][0]['reason'])

    def test_resolved_old_backup_only_deleted_outside_retention_with_exact_sha_and_content(self):
        cycles, protected = self.shared_hold_case()
        old = cycles[0]
        for number in range(3, 6):
            cycles.append(dict(old, run_id=str(number + 200),
                               backup='backup/staging-before-reset-' + str(number) * 12,
                               backup_sha=str(number) * 40,
                               candidate=f'staging-next-cycle-{number + 200}-1',
                               completed_at=f'2026-11-0{number}T00:00:00Z'))
        refs = dict(main='a' * 40, staging='a' * 40, **{old['backup']: old['backup_sha']})
        for days, content, changed in ((1, True, False), (30, True, False),
                                       (1, False, False), (1, True, True)):
            current_refs = dict(refs)
            if changed:
                current_refs[old['backup']] = 'f' * 40
            plan = retention.build_plan('choice/repo', current_refs, cycles, protected,
                                        self.now.isoformat(), lambda sha: content,
                                        min_days=days, scope='backups')
            with self.subTest(days=days, content=content, changed=changed):
                self.assertEqual(old['backup'] in plan['cleanup'], days == 1 and content and not changed)

    def test_legacy_success_without_release_revision_does_not_resolve_hold(self):
        def legacy(files):
            plan = json.loads(files['release-cycle-plan.json'])
            del plan['release_sha']
            files['release-cycle-plan.json'] = json.dumps(plan).encode()
        cycles, protected = self.shared_hold_case(mutate_success=legacy)
        self.assertEqual(len(cycles), 1)
        self.assertIn('backup/staging-before-reset-' + 'b' * 12, protected)

    def test_receipt_plan_disagreement_cannot_resolve_holds(self):
        def corrupt(files):
            plan = json.loads(files['release-cycle-plan.json'])
            files['closure-receipt.json'] = json.dumps(dict(
                schema=1, repository='choice/repo', run_id='200', run_attempt='1',
                release='release-other', main=plan['main'], original_staging=plan['staging'],
                backup_sha=plan['staging'], backup='backup/staging-before-reset-' + 'b' * 12,
                candidate='staging-next-cycle-200-1', candidate_sha='a' * 40)).encode()
        cycles, protected = self.shared_hold_case(mutate_success=corrupt)
        self.assertEqual(cycles, [])
        self.assertIn('backup/staging-before-reset-' + 'b' * 12, protected)


class LeasedRetentionIntegrationTests(unittest.TestCase):
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
        self.git('commit', '--allow-empty', '-m', '[DEV-221] - Retention fixture')
        self.sha = self.git('rev-parse', 'HEAD')
        self.remote = str(Path(self.directory.name) / 'remote.git')
        self.git('clone', '--bare', '.', self.remote)
        self.git('remote', 'add', 'origin', self.remote)
        self.branches = ['staging-next-cycle-1-1', 'backup/staging-before-reset-' + self.sha[:12]]
        for branch in self.branches:
            self.git('--git-dir=' + self.remote, 'update-ref', 'refs/heads/' + branch, self.sha)
        self.plan = dict(schema=2, repository='choice/repo', assessed_at='2026-11-18T00:00:00+00:00',
                         scope='all', include_legacy_candidates=False,
                         keep_cycles=3, min_days=30, cleanup={branch: self.sha for branch in self.branches})

    def git(self, *args):
        return subprocess.check_output(['git', *args], text=True, stderr=subprocess.DEVNULL).strip()

    def execute(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY='choice/repo'), \
             patch.object(retention, 'approval_environment'), \
             patch.object(retention, 'assess', return_value=self.plan), \
             patch.object(retention, 'verify_remote_repository'):
            retention.execute(self.plan)

    def test_atomic_deletion_works_against_local_bare_remote(self):
        self.execute()
        for branch in self.branches:
            self.assertEqual(self.git('--git-dir=' + self.remote, 'for-each-ref', '--format=%(refname)',
                                      'refs/heads/' + branch), '')
        self.assertEqual(self.git('--git-dir=' + self.remote, 'rev-parse', 'main'), self.sha)

    def test_concurrent_ref_change_rejects_whole_atomic_deletion(self):
        advanced = subprocess.check_output([
            'git', '--git-dir=' + self.remote, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit-tree', self.sha + '^{tree}', '-p', self.sha], input='Concurrent change\n', text=True).strip()
        self.git('--git-dir=' + self.remote, 'update-ref', 'refs/heads/' + self.branches[0], advanced)
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        self.assertEqual(self.git('--git-dir=' + self.remote, 'rev-parse', self.branches[0]), advanced)
        self.assertEqual(self.git('--git-dir=' + self.remote, 'rev-parse', self.branches[1]), self.sha)


if __name__ == '__main__':
    unittest.main()
