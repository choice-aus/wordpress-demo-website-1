"""Read-only ruleset proposal and validation for the cleanup automation user.

This module deliberately has no API write operation. A repository administrator
reviews/applies the proposed payloads separately, with their own credentials.
"""

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import subprocess


SCOPES = {
    'main-protection': '~DEFAULT_BRANCH',
    'staging-protection': 'refs/heads/staging',
    'release-protection': 'refs/heads/release-*',
    'staging-reset-guard': 'refs/heads/staging',
    'release-deletion-guard': 'refs/heads/release-*',
}
GUARDS = {'staging-reset-guard': 'non_fast_forward',
          'release-deletion-guard': 'deletion'}
BASELINES = ('main-protection', 'staging-protection', 'release-protection')
PAYLOAD_KEYS = ('name', 'target', 'enforcement', 'conditions', 'rules', 'bypass_actors')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def api(endpoint):
    """GET only. Credentials stay in GH_TOKEN, never command arguments/logs."""
    result = subprocess.run(['gh', 'api', '--method', 'GET',
                             '-H', 'X-GitHub-Api-Version: 2026-03-10', endpoint],
                            text=True, capture_output=True)
    require(result.returncode == 0, 'GitHub read failed: ' + endpoint)
    return json.loads(result.stdout)


def pages(endpoint):
    items = []
    for page in range(1, 101):
        batch = api(f'{endpoint}?per_page=100&page={page}')
        require(isinstance(batch, list), 'Expected paginated GitHub list')
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise RuntimeError('GitHub pagination limit reached; refuse partial evidence')


def load_rulesets(repo):
    summaries = pages(f'repos/{repo}/rulesets')
    return [api(f'repos/{repo}/rulesets/{row["id"]}') for row in summaries
            if row.get('source_type') == 'Repository' and row.get('source') == repo]


def index_rulesets(rulesets, repo):
    indexed = {}
    for row in rulesets:
        name = row.get('name')
        if name not in SCOPES:
            continue
        require(name not in indexed, 'Duplicate ruleset: ' + name)
        require(row.get('source_type') == 'Repository' and row.get('source') == repo,
                'Ruleset must be repository-owned: ' + name)
        require(row.get('target') == 'branch' and row.get('enforcement') == 'active',
                'Ruleset must be active for branches: ' + name)
        require(row.get('conditions') == {'ref_name': {
            'include': [SCOPES[name]], 'exclude': []}}, 'Unexpected scope: ' + name)
        require('bypass_actors' in row, 'Cannot inspect bypass actors: ' + name)
        indexed[name] = row
    for name in BASELINES:
        require(name in indexed, 'Missing baseline ruleset: ' + name)
        require(indexed[name]['bypass_actors'] == [], 'Baseline bypass forbidden: ' + name)
    return indexed


def validate_baselines(indexed):
    required = {
        'main-protection': {'deletion', 'non_fast_forward', 'pull_request', 'required_status_checks'},
        'staging-protection': {'deletion'},
        'release-protection': {'non_fast_forward', 'pull_request', 'required_status_checks'},
    }
    for name, types in required.items():
        rules = indexed[name]['rules']
        require(types <= {rule['type'] for rule in rules}, 'Missing baseline rules: ' + name)
        if name == 'staging-protection':
            continue
        pr = next(rule['parameters'] for rule in rules if rule['type'] == 'pull_request')
        methods = ['merge'] if name == 'main-protection' else ['squash']
        require(pr.get('allowed_merge_methods') == methods, 'Unexpected merge methods: ' + name)
        checks = next(rule['parameters']['required_status_checks'] for rule in rules
                      if rule['type'] == 'required_status_checks')
        require({'Validate promotion path', 'PHP and theme checks'} <=
                {check['context'] for check in checks}, 'Missing promotion checks: ' + name)


def bypass(actor_id):
    require(type(actor_id) is int and actor_id > 0, 'Automation user ID must be positive')
    return [{'actor_id': actor_id, 'actor_type': 'User', 'bypass_mode': 'always'}]


def validate_profile(rulesets, repo, actor_id):
    indexed = index_rulesets(rulesets, repo)
    validate_baselines(indexed)
    for name, rule_type in GUARDS.items():
        require(name in indexed, 'Missing operational guard: ' + name)
        row = indexed[name]
        require(row['rules'] == [{'type': rule_type}], 'Guard must contain only ' + rule_type)
        require(row['bypass_actors'] == bypass(actor_id), 'Unexpected guard bypass: ' + name)
    require('non_fast_forward' not in {r['type'] for r in indexed['staging-protection']['rules']},
            'Move staging force-push restriction to staging-reset-guard')
    require('deletion' not in {r['type'] for r in indexed['release-protection']['rules']},
            'Move release deletion restriction to release-deletion-guard')
    return sorted(indexed)


def proposal(rulesets, repo, actor):
    require(actor.get('type') == 'User', 'Expected a dedicated GitHub user')
    indexed = index_rulesets(rulesets, repo)
    validate_baselines(indexed)
    operations = []
    proposed = deepcopy(rulesets)
    # Install both active guards FIRST; never disable protection during the split.
    for name, rule_type in GUARDS.items():
        payload = dict(name=name, target='branch', enforcement='active',
                       conditions={'ref_name': {'include': [SCOPES[name]], 'exclude': []}},
                       rules=[{'type': rule_type}], bypass_actors=bypass(actor['id']))
        old = indexed.get(name)
        require(not old or old['rules'] == [{'type': rule_type}],
                'Refuse to remove additional rules from existing guard: ' + name)
        operations.append(dict(method='PUT' if old else 'POST',
                               endpoint=f'repos/{repo}/rulesets' + (f'/{old["id"]}' if old else ''),
                               payload=payload))
        proposed = [row for row in proposed if row.get('name') != name]
        proposed.append(dict(payload, source_type='Repository', source=repo))
    for name, removed in (('staging-protection', 'non_fast_forward'),
                          ('release-protection', 'deletion')):
        old = indexed[name]
        if not any(rule['type'] == removed for rule in old['rules']):
            continue
        payload = {key: deepcopy(old[key]) for key in PAYLOAD_KEYS}
        payload['rules'] = [r for r in payload['rules'] if r['type'] != removed]
        operations.append(dict(method='PUT', endpoint=f'repos/{repo}/rulesets/{old["id"]}',
                               payload=payload))
        proposed = [dict(row, **payload) if row.get('name') == name else row for row in proposed]
    validate_profile(proposed, repo, actor['id'])
    return dict(repository=repo, actor=dict(login=actor['login'], id=actor['id']),
                warning='REVIEW ONLY: no settings changed. Apply in this order; re-read before applying.',
                operations=operations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--actor', required=True, help='Dedicated automation user login (not a token)')
    parser.add_argument('--output', default='cleanup-ruleset-proposal.json')
    args = parser.parse_args()
    require(args.repository and re.fullmatch(r'[\w.-]+/[\w.-]+', args.repository), 'Invalid repository')
    require(re.fullmatch(r'[A-Za-z0-9-]+', args.actor), 'Invalid GitHub login')
    actor = api('users/' + args.actor)
    plan = proposal(load_rulesets(args.repository), args.repository, actor)
    Path(args.output).write_text(json.dumps(plan, indent=2) + '\n')
    print('Read-only ruleset proposal written to ' + args.output)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, KeyError, ValueError) as error:
        raise SystemExit('BLOCKED: ' + str(error))
