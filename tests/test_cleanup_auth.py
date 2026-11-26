"""Protected cleanup must fail before configuring credentials on unsafe setups."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import cleanup_auth as auth
import ruleset_policy as policy


REPO = 'choice-aus/wordpress-demo-website-1'
ACTOR = dict(login='demo-automation', id=123, type='User')


def baseline():
    result = []
    for number, name in enumerate(policy.BASELINES, 1):
        rules = [{'type': 'deletion'}] if name != 'release-protection' else []
        if name != 'staging-protection':
            rules += [dict(type='non_fast_forward'), dict(type='pull_request', parameters={
                'allowed_merge_methods': ['merge'] if name == 'main-protection' else ['squash'],
                'required_approving_review_count': 2}), dict(type='required_status_checks', parameters={
                    'strict_required_status_checks_policy': True,
                    'required_status_checks': [dict(context=context, integration_id=15368) for context in
                                               ('Validate promotion path', 'PHP and theme checks')]})]
        result.append(dict(id=number, name=name, target='branch', enforcement='active',
                           source_type='Repository', source=REPO, bypass_actors=[], rules=rules,
                           conditions={'ref_name': {'include': [policy.SCOPES[name]], 'exclude': []}}))
    return result


def profile():
    rows = baseline()
    for name, rule in policy.GUARDS.items():
        rows.append(dict(id=len(rows) + 1, name=name, target='branch', enforcement='active',
                         source_type='Repository', source=REPO, rules=[dict(type=rule)],
                         bypass_actors=policy.bypass(ACTOR['id']),
                         conditions={'ref_name': {'include': [policy.SCOPES[name]], 'exclude': []}}))
    return rows


def environment():
    return dict(protection_rules=[dict(type='required_reviewers', reviewers=[dict(type='User')])],
                deployment_branch_policy=dict(custom_branch_policies=True, protected_branches=False))


class RulesetPolicyTests(unittest.TestCase):
    def test_accepts_split_guards_with_no_bypass_for_normal_protections(self):
        self.assertEqual(len(policy.validate_profile(profile(), REPO, ACTOR['id'])), 5)

    def test_rejects_each_baseline_bypass(self):
        for name in policy.BASELINES:
            rows = profile()
            next(row for row in rows if row['name'] == name)['bypass_actors'] = policy.bypass(123)
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, 'Baseline bypass'):
                policy.validate_profile(rows, REPO, 123)

    def test_rejects_broad_wrong_or_disabled_guards(self):
        mutations = (
            lambda row: row.update(enforcement='disabled'),
            lambda row: row['conditions']['ref_name'].update(include=['~ALL']),
            lambda row: row['conditions']['ref_name'].update(exclude=['refs/heads/main']),
            lambda row: row.update(bypass_actors=[dict(actor_type='RepositoryRole', actor_id=5, bypass_mode='always')]),
            lambda row: row.update(bypass_actors=policy.bypass(999)),
            lambda row: row.update(bypass_actors=[dict(actor_type='User', actor_id=123, bypass_mode='exempt')]),
            lambda row: row['rules'].append(dict(type='pull_request')),
            lambda row: row.pop('bypass_actors'),
        )
        for name in policy.GUARDS:
            for mutation in mutations:
                rows = profile()
                mutation(next(row for row in rows if row['name'] == name))
                with self.subTest(name=name, mutation=mutation), self.assertRaises(RuntimeError):
                    policy.validate_profile(rows, REPO, 123)

    def test_missing_guards_baselines_and_checks_block_execution(self):
        for missing in policy.SCOPES:
            with self.subTest(missing=missing), self.assertRaises(RuntimeError):
                policy.validate_profile([row for row in profile() if row['name'] != missing], REPO, 123)
        rows = profile()
        rows[0]['rules'] = [rule for rule in rows[0]['rules'] if rule['type'] != 'required_status_checks']
        with self.assertRaisesRegex(RuntimeError, 'Missing baseline rules'):
            policy.validate_profile(rows, REPO, 123)

    def test_duplicate_names_and_wrong_repository_fail_closed(self):
        rows = profile()
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            policy.validate_profile(rows + [deepcopy(rows[0])], REPO, 123)
        rows[0]['source'] = 'another/repo'
        with self.assertRaisesRegex(RuntimeError, 'repository-owned'):
            policy.validate_profile(rows, REPO, 123)

    def test_proposal_installs_guards_first_and_preserves_all_normal_checks(self):
        rows = baseline()
        rows[1]['rules'].append(dict(type='non_fast_forward'))
        rows[2]['rules'].append(dict(type='deletion'))
        original = deepcopy(rows)
        plan = policy.proposal(rows, REPO, ACTOR)
        self.assertEqual(rows, original)
        ops = plan['operations']
        self.assertEqual([op['payload']['name'] for op in ops],
                         ['staging-reset-guard', 'release-deletion-guard',
                          'staging-protection', 'release-protection'])
        self.assertEqual([op['method'] for op in ops], ['POST', 'POST', 'PUT', 'PUT'])
        self.assertTrue(all(op['payload']['enforcement'] == 'active' for op in ops))
        release = ops[-1]['payload']
        self.assertEqual(release['rules'], original[2]['rules'][:-1])
        self.assertEqual(release['bypass_actors'], [])
        self.assertNotIn('main-protection', [op['payload']['name'] for op in ops])

    def test_proposal_handles_current_relaxed_rules_without_weakening_baselines(self):
        ops = policy.proposal(baseline(), REPO, ACTOR)['operations']
        self.assertEqual(len(ops), 2)
        self.assertTrue(all(op['method'] == 'POST' for op in ops))

    def test_existing_guards_are_updated_in_place_and_bot_identity_rejected(self):
        ops = policy.proposal(profile(), REPO, ACTOR)['operations']
        self.assertTrue(all(op['method'] == 'PUT' for op in ops))
        with self.assertRaisesRegex(RuntimeError, 'dedicated GitHub user'):
            policy.proposal(baseline(), REPO, dict(ACTOR, type='Bot'))

    def test_proposal_refuses_to_remove_additional_guard_restrictions(self):
        rows = profile()
        rows[-1]['rules'].append(dict(type='pull_request'))
        with self.assertRaisesRegex(RuntimeError, 'additional rules'):
            policy.proposal(rows, REPO, ACTOR)

    def test_pagination_does_not_omit_rulesets_beyond_first_page(self):
        with patch.object(policy, 'api', side_effect=[[{}] * 100, [dict(id=101)]]) as api:
            self.assertEqual(len(policy.pages('repos/test/repo/rulesets')), 101)
        self.assertIn('page=2', api.call_args.args[0])

    def test_failed_api_read_does_not_print_captured_credentials(self):
        result = subprocess.CompletedProcess([], 1, stdout='', stderr='fake-test-secret')
        with patch.object(policy.subprocess, 'run', return_value=result) as run:
            with self.assertRaises(RuntimeError) as failure:
                policy.api('repos/test/repo')
        self.assertNotIn('fake-test-secret', str(failure.exception))
        self.assertIn('GET', run.call_args.args[0])


class CleanupAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(GITHUB_ACTIONS='true', GITHUB_REF='refs/heads/main', GITHUB_REPOSITORY=REPO,
                        CLEANUP_AUTH_MODE='automation-token', CLEANUP_ACTOR=ACTOR['login'],
                        AUTOMATION_TOKEN='fake-test-secret', GH_TOKEN='fake-test-secret', BUILTIN_TOKEN='builtin')

    def api_response(self, endpoint):
        if endpoint == 'user':
            return ACTOR
        if endpoint == f'repos/{REPO}':
            return dict(default_branch='main')
        if 'deployment-branch-policies' in endpoint:
            return dict(total_count=1, branch_policies=[dict(type='branch', name='main')])
        if endpoint.endswith('/environments/branch-cleanup'):
            return environment()
        self.fail('Unexpected API: ' + endpoint)

    def test_automation_mode_reports_identity_but_no_credentials(self):
        with patch.object(auth, 'api', side_effect=self.api_response), \
             patch.object(auth, 'load_rulesets', return_value=profile()):
            report = auth.preflight(self.env)
        self.assertEqual(report['actor'], dict(login=ACTOR['login'], id=123))
        self.assertNotIn(self.env['GH_TOKEN'], json.dumps(report))

    def test_bad_mode_missing_token_mismatch_nonmain_and_nonactions_fail_before_reads(self):
        for change in (dict(CLEANUP_AUTH_MODE='typo'), dict(AUTOMATION_TOKEN=''), dict(GH_TOKEN='builtin'),
                       dict(CLEANUP_ACTOR=''), dict(GITHUB_REF='refs/heads/feature/DEV-225'),
                       dict(GITHUB_REF='refs/tags/main'), dict(GITHUB_ACTIONS='false')):
            with self.subTest(change=change), patch.object(auth, 'api') as api, \
                 self.assertRaises(RuntimeError):
                auth.preflight(dict(self.env, **change))
            api.assert_not_called()

    def test_wrong_user_and_app_tokens_are_rejected(self):
        for actor in (dict(ACTOR, login='wrong-user'), dict(ACTOR, type='Bot')):
            def fake_api(endpoint):
                return actor if endpoint == 'user' else self.api_response(endpoint)
            with self.subTest(actor=actor), patch.object(auth, 'api', side_effect=fake_api), \
                 patch.object(auth, 'load_rulesets') as rules, self.assertRaisesRegex(RuntimeError, 'expected automation'):
                auth.preflight(self.env)
            rules.assert_not_called()

    def test_legacy_mode_uses_only_builtin_token_and_needs_no_service_account(self):
        env = dict(GITHUB_ACTIONS='true', GH_TOKEN='builtin', BUILTIN_TOKEN='builtin')
        with patch.object(auth, 'api') as api:
            self.assertEqual(auth.preflight(env)['mode'], 'github-token')
        api.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, 'built-in token'):
            auth.preflight(dict(env, GH_TOKEN='different'))

    def test_environment_requires_reviewer_and_exact_main_branch_policy(self):
        auth.validate_environment(environment(), [dict(type='branch', name='main')])
        for policies in ([], [dict(type='tag', name='main')], [dict(type='branch', name='*')],
                         [dict(type='branch', name='main'), dict(type='branch', name='feature/*')]):
            with self.subTest(policies=policies), self.assertRaises(RuntimeError):
                auth.validate_environment(environment(), policies)
        for change in (dict(protection_rules=[]), dict(deployment_branch_policy=None),
                       dict(deployment_branch_policy=dict(protected_branches=True))):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                auth.validate_environment(dict(environment(), **change), [dict(type='branch', name='main')])

    def test_preflight_failure_never_configures_git_or_writes_a_report(self):
        with patch.object(auth, 'preflight', side_effect=RuntimeError('blocked')), \
             patch.object(auth.subprocess, 'run') as run, patch.object(auth.Path, 'write_text') as write:
            with self.assertRaises(RuntimeError):
                auth.main()
        run.assert_not_called()
        write.assert_not_called()

    def test_git_helper_receives_no_secret_in_arguments_and_report_contains_no_secret(self):
        report = dict(mode='automation-token', actor=dict(login=ACTOR['login'], id=123))
        with patch.object(auth, 'preflight', return_value=report), \
             patch.object(auth.subprocess, 'run', return_value=type('Result', (), {'returncode': 0})()) as run, \
             patch.object(auth.Path, 'write_text') as write, patch('builtins.print'):
            auth.main()
        self.assertEqual(run.call_args.args[0], ['gh', 'auth', 'setup-git', '--hostname', 'github.com'])
        self.assertNotIn('fake-test-secret', write.call_args.args[0])

    @unittest.skipUnless(shutil.which('gh') and shutil.which('git'), 'GitHub CLI and Git required')
    def test_real_git_helper_reads_each_steps_token_without_persisting_it(self):
        # Entirely local: no real token, network request, developer configuration
        # or remote ref is used. Exercise gh rather than mocking credential lookup.
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, HOME=directory, XDG_CONFIG_HOME=directory,
                       GH_CONFIG_DIR=directory, GIT_CONFIG_GLOBAL=directory + '/gitconfig',
                       GIT_CONFIG_NOSYSTEM='1', GH_HOST='github.com', GH_TOKEN='fake-first-token')
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(auth, 'preflight', return_value=dict(mode='automation-token')), \
                 patch.object(auth.Path, 'write_text'), patch('builtins.print'):
                auth.main()
            for token in ('fake-first-token', 'fake-second-token'):
                result = subprocess.run(['git', 'credential', 'fill'], env=dict(env, GH_TOKEN=token),
                                        input='protocol=https\nhost=github.com\n\n',
                                        text=True, capture_output=True, check=True)
                self.assertIn('password=' + token, result.stdout)
                self.assertNotIn(token, Path(directory + '/gitconfig').read_text())


if __name__ == '__main__':
    unittest.main()
