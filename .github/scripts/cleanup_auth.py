"""Validate cleanup credentials before enabling Git authentication on Actions."""

import json
import os
from pathlib import Path
import subprocess

from ruleset_policy import api, load_rulesets, require, validate_profile


def validate_environment(environment, policies):
    reviewers = [rule for rule in environment.get('protection_rules', [])
                 if rule.get('type') == 'required_reviewers' and rule.get('reviewers')]
    require(reviewers, 'branch-cleanup must require reviewer approval')
    policy = environment.get('deployment_branch_policy') or {}
    require(policy.get('custom_branch_policies') is True and
            policy.get('protected_branches') is False,
            'branch-cleanup must use a custom main-only deployment policy')
    require(len(policies) == 1 and policies[0].get('name') == 'main' and
            policies[0].get('type') == 'branch',
            'branch-cleanup must allow only branch main (no tags or wildcards)')


def preflight(env):
    mode = env.get('CLEANUP_AUTH_MODE', 'github-token')
    require(mode in ('github-token', 'automation-token'), 'Unknown CLEANUP_AUTH_MODE')
    require(env.get('GITHUB_ACTIONS') == 'true', 'Credential setup is Actions-only')
    if mode == 'github-token':
        require(env.get('GH_TOKEN') and env.get('GH_TOKEN') == env.get('BUILTIN_TOKEN'),
                'github-token mode must use the built-in token')
        return dict(mode=mode, warning='No protected-operation bypass in github-token mode')
    require(env.get('AUTOMATION_TOKEN'), 'BRANCH_CLEANUP_TOKEN is required; no fallback allowed')
    require(env.get('GH_TOKEN') == env['AUTOMATION_TOKEN'], 'Git/API token selection mismatch')
    require(env.get('CLEANUP_ACTOR'), 'CLEANUP_ACTOR is required')
    require(env.get('GITHUB_REF') == 'refs/heads/main', 'Automation credentials require branch main')
    repo = env.get('GITHUB_REPOSITORY', '')
    require(repo and api(f'repos/{repo}').get('default_branch') == 'main',
            'Repository default branch must be main')
    actor = api('user')
    require(actor.get('type') == 'User' and
            actor.get('login', '').casefold() == env['CLEANUP_ACTOR'].casefold(),
            'Token must belong to the expected automation user')
    environment = api(f'repos/{repo}/environments/branch-cleanup')
    policies = api(f'repos/{repo}/environments/branch-cleanup/deployment-branch-policies?per_page=100')
    require(policies.get('total_count') == len(policies.get('branch_policies', [])),
            'Incomplete deployment branch policies')
    validate_environment(environment, policies.get('branch_policies', []))
    names = validate_profile(load_rulesets(repo), repo, actor['id'])
    return dict(mode=mode, actor=dict(login=actor['login'], id=actor['id']),
                repository=repo, ref=env['GITHUB_REF'], verified_rulesets=names)


def main():
    report = preflight(os.environ)
    # No token is written to Git configuration or to the evidence report. Both
    # Git (including carryover worktrees) and gh obtain GH_TOKEN from each step.
    result = subprocess.run(['gh', 'auth', 'setup-git', '--hostname', 'github.com'],
                            text=True, capture_output=True)
    require(result.returncode == 0, 'Unable to configure GitHub CLI credential helper')
    Path('cleanup-auth-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, KeyError, ValueError) as error:
        raise SystemExit('BLOCKED: ' + str(error))
