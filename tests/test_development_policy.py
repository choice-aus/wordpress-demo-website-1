"""Exercise branch policy and installed hooks using disposable local repositories."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/scripts'))

import branch_audit
import development_policy as policy
import staging_reset_check as check
import staging_reset_plan as plan
import uat_carryover as carry
import validate_commits as titles


class BranchPolicyTests(unittest.TestCase):
    def test_supported_types_follow_release_promotion_without_direct_main_promotion(self):
        for prefix in policy.WORK_BRANCH_PREFIXES:
            with self.subTest(prefix=prefix):
                branch = prefix + 'DEV-220-example'
                self.assertTrue(policy.is_work_branch(branch))
                self.assertIsNone(policy.promotion_error(branch, 'release-next'))
                self.assertIsNotNone(policy.promotion_error(branch, 'main'))
        self.assertIsNone(policy.promotion_error('release-next', 'main'))

    def test_unsupported_and_empty_work_names_cannot_promote_into_release(self):
        for branch in ('main', 'staging', 'develop', 'release-other', 'feature/',
                       'fix/', 'hotfix/', 'chore/', 'fixes/test', 'backup/test'):
            with self.subTest(branch=branch):
                self.assertFalse(policy.is_work_branch(branch))
                self.assertIsNotNone(policy.promotion_error(branch, 'release-next'))

    def test_audit_discovery_includes_supported_types_and_excludes_recovery_refs(self):
        branches = [prefix + 'test' for prefix in policy.WORK_BRANCH_PREFIXES]
        output = '\n'.join('origin/' + branch for branch in branches + [
            'release-next', 'main', 'staging', 'backup/test', 'staging-next-cycle-123-1'])
        with patch.object(branch_audit, 'git', side_effect=['', output]):
            self.assertEqual(branch_audit.get_branches(), sorted(branches + ['release-next']))

    def test_supported_types_can_be_preserved_and_selected_for_carryover(self):
        for prefix in policy.WORK_BRANCH_PREFIXES:
            with self.subTest(prefix=prefix):
                branch = prefix + 'test'
                self.assertEqual(check.staging_feature_branches([('abc1234', ['origin/' + branch])]),
                                 [branch])
                pr = dict(number=1, merged_at='2026-11-18T00:00:00Z', base={'ref': 'staging'},
                          head={'ref': branch, 'repo': {'full_name': 'choice/repo'}})
                def paginated(endpoint):
                    return [dict(number=1, pull_request={})] if '/issues?' in endpoint else [{'sha': 'a' * 40}]
                with patch.object(carry, 'REPO', 'choice/repo'), \
                     patch.object(carry, 'gh_api_paginated', side_effect=paginated), \
                     patch.object(carry, 'gh_api', return_value=pr):
                    self.assertEqual(carry.selected_carryover_prs()[branch]['tested_sha'], 'a' * 40)
                with patch.object(check, 'github_paginated', return_value=[{'sha': 'a' * 40}]):
                    self.assertEqual(plan.staging_feature_commits('choice/repo', [pr])['a' * 40][0]['branch'],
                                     branch)

    def test_cleanup_still_requires_current_tip_to_match_reviewed_tip_for_every_type(self):
        for prefix in policy.WORK_BRANCH_PREFIXES:
            branch = prefix + 'test'
            pr = dict(number=1, state='closed', merged_at='2026-11-18T00:00:00Z', labels=[],
                      head={'ref': branch, 'sha': 'a' * 40, 'repo': {'full_name': 'choice/repo'}},
                      base={'ref': 'release-next', 'repo': {'full_name': 'choice/repo'}},
                      merge_commit_sha='b' * 40)
            for current, expected in (('a' * 40, 'Cleanup candidate'), ('c' * 40, 'Needs review')):
                with self.subTest(branch=branch, current=current), \
                     patch.object(branch_audit, 'git', return_value=current), \
                     patch('release_evidence.verify_promoted_pr', return_value=True):
                    self.assertEqual(branch_audit.classify(branch, [pr], 'release-next', True)[0], expected)


class HookIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.hooksPath', str(self.repo / 'not-installed'))
        self.git('commit', '--allow-empty', '-m', 'Historical production title')
        self.base = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.git('config', '--unset', 'core.hooksPath')
        scripts = self.repo / '.github/scripts'
        scripts.mkdir(parents=True)
        for name in ('validate_commits.py', 'development_policy.py'):
            shutil.copy2(ROOT / '.github/scripts' / name, scripts / name)
        shutil.copytree(ROOT / '.githooks', self.repo / '.githooks')
        self.install()

    def git(self, *args, check=True):
        return subprocess.run(['git', *args], cwd=self.repo, text=True, capture_output=True, check=check)

    def install(self, check=True):
        return subprocess.run(['sh', str(ROOT / 'scripts/install-git-hooks.sh')], cwd=self.repo,
                              text=True, capture_output=True, check=check)

    def cli(self, *args, input=None):
        return subprocess.run([sys.executable, str(self.repo / '.github/scripts/validate_commits.py'), *args],
                              cwd=self.repo, text=True, input=input, capture_output=True)

    def pre_push(self, local, remote=None):
        line = f'refs/heads/fix/test {local} refs/heads/fix/test {remote or titles.ZERO_SHA}\n'
        return subprocess.run([str(self.repo / '.githooks/pre-push'), 'origin', 'unused'],
                              cwd=self.repo, text=True, input=line, capture_output=True)

    def test_installed_message_hook_blocks_bad_title_before_commit_exists(self):
        invalid = self.git('commit', '--allow-empty', '-m', 'DEV-220: invalid', check=False)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn('Expected [ABC-123] - Description', invalid.stderr)
        self.assertEqual(self.git('rev-parse', 'HEAD').stdout.strip(), self.base)
        self.git('commit', '--allow-empty', '-m', '[DEV-220] - Valid title')

    def test_pre_push_catches_commits_created_with_message_hook_bypassed(self):
        self.git('-c', 'core.hooksPath=/dev/null', 'commit', '--allow-empty', '-m', 'DEV-220: invalid')
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        for remote in (None, self.base):
            result = self.pre_push(head, remote)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('DEV-220: invalid', result.stderr)

    def test_new_branch_and_pr_range_exclude_historical_main_titles(self):
        self.git('commit', '--allow-empty', '-m', '[DEV-220] - Valid title')
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.assertEqual(self.pre_push(head).returncode, 0)
        self.assertEqual(self.cli('--base', self.base, '--head', head).returncode, 0)

    def test_missing_remote_object_fails_closed(self):
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.assertNotEqual(self.pre_push(head, 'f' * 40).returncode, 0)
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertNotEqual(self.pre_push(head).returncode, 0)

    def test_branch_deletion_does_not_validate_existing_history(self):
        self.assertEqual(self.pre_push(titles.ZERO_SHA, self.base).returncode, 0)

    def test_preservation_fetches_and_accounts_for_all_supported_branch_types(self):
        self.git('switch', '-c', 'fix/test')
        self.git('commit', '--allow-empty', '-m', '[DEV-220] - Unreleased change')
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        # Fetch from a separate local bare repository; no network or remote service writes.
        remote = self.repo / 'local-remote.git'
        self.git('clone', '--bare', '.', str(remote))
        for prefix in policy.WORK_BRANCH_PREFIXES:
            subprocess.run(['git', '--git-dir=' + str(remote), 'update-ref',
                            'refs/heads/' + prefix + 'test', head], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        def local_git(*args):
            return self.git(*args).stdout.strip()
        with patch.object(check, 'git', side_effect=local_git), \
             patch.object(check, 'is_ancestor', side_effect=lambda commit, branch:
                          self.git('merge-base', '--is-ancestor', commit, branch, check=False).returncode == 0):
            preserved, missing = check.check_preservation(head)
        self.assertEqual(missing, [])
        self.assertEqual(preserved[0][0], head[:7])
        self.assertEqual(set(preserved[0][1]),
                         {'origin/' + prefix + 'test' for prefix in policy.WORK_BRANCH_PREFIXES})

    def test_merge_titles_are_exempt_but_squash_titles_are_checked(self):
        self.git('switch', '-c', 'fix/test')
        (self.repo / 'change.txt').write_text('change\n')
        self.git('add', 'change.txt')
        self.git('commit', '-m', '[DEV-220] - Change')
        self.git('switch', 'main')
        self.git('merge', '--no-ff', 'fix/test', '-m', "Merge branch 'fix/test'")
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.assertEqual(self.cli('--range', self.base + '..' + head).returncode, 0)
        self.git('switch', '-c', 'release-squash', self.base)
        self.git('merge', '--squash', 'fix/test')
        result = self.git('commit', '-m', 'Invalid squash title', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.git('commit', '-m', '[DEV-220] - Squash change')

    def test_installer_is_repeatable_and_preserves_other_hook_configurations(self):
        self.assertEqual(self.install().returncode, 0)
        self.git('config', 'core.hooksPath', '/some/existing/hooks')
        result = self.install(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git('config', '--get', 'core.hooksPath').stdout.strip(), '/some/existing/hooks')


class TitlePolicyTests(unittest.TestCase):
    def test_format_requires_ticket_brackets_dash_and_description(self):
        for title in ('[DEV-220] - Description', '[ABC-1] - A fix'):
            titles.check_title(title)
        for title in ('DEV-220: Description', '[DEV-220] Description', '[DEV-220] - ',
                      '[DEV-220] -  ', '[dev-220] - Description', '', '[DEV-220] - A\nB'):
            with self.subTest(title=title), self.assertRaises(ValueError):
                titles.check_title(title)


if __name__ == '__main__':
    unittest.main()
