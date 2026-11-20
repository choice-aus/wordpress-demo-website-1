"""Source releases use verified merge SHAs and create-only version allocation."""

from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import publish_github_release as publisher

SHA = 'a' * 40
REPO = 'choice/repo'


def pr():
    return dict(number=70, merged_at='2026-11-20T00:00:00Z', merge_commit_sha=SHA, labels=[],
                head=dict(ref='release-dev-223', repo=dict(full_name=REPO)),
                base=dict(ref='main', repo=dict(full_name=REPO)))


def reserved():
    return dict(name='v1.0.1', sha=SHA, metadata=dict(automation=publisher.AUTOMATION,
                repository=REPO, merge_sha=SHA, pr=70, version='v1.0.1', bump='patch', previous_tag='v1.0.0'))


class VersionPolicyTests(unittest.TestCase):
    def test_default_patch_and_label_overrides(self):
        self.assertEqual(publisher.bump_kind([]), 'patch')
        for kind, expected in (('patch', 'v1.2.4'), ('minor', 'v1.3.0'), ('major', 'v2.0.0')):
            self.assertEqual(publisher.bump_kind([dict(name='release:' + kind)]), kind)
            self.assertEqual(publisher.increment('v1.2.3', kind), expected)

    def test_conflicting_labels_block_instead_of_guessing(self):
        with self.assertRaisesRegex(RuntimeError, 'Conflicting'):
            publisher.bump_kind([dict(name='release:minor'), dict(name='release:major')])

    def test_semver_order_is_numeric_and_nonstable_tags_are_ignored(self):
        self.assertGreater(publisher.version_tuple('v1.10.0'), publisher.version_tuple('v1.9.9'))
        for tag in ('v1.0.0-rc.1', 'v01.0.0', 'release-dev-223', '1.0.0'):
            self.assertIsNone(publisher.version_tuple(tag))


class EligibilityTests(unittest.TestCase):
    def eligible(self, pull=None, run=None, parents=3):
        pull = pr() if pull is None else pull
        run = dict(head_sha=SHA, head_branch='main', event='push', conclusion='success') if run is None else run
        with patch.object(publisher, 'is_ancestor', return_value=True), \
             patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(publisher, 'git', return_value=' '.join([SHA] * parents)), \
             patch.object(publisher, 'pages', side_effect=[[pull], [run]]):
            return publisher.release_pr(REPO, SHA)

    def test_only_exact_release_to_main_merge_with_successful_main_push_ci_is_eligible(self):
        self.assertEqual(self.eligible()['number'], 70)

    def test_feature_and_fork_promotions_are_not_releases(self):
        for head in (dict(ref='feature/DEV-223', repo=dict(full_name=REPO)),
                     dict(ref='release-test', repo=dict(full_name='outside/fork'))):
            altered = dict(pr(), head=head)
            self.assertIsNone(self.eligible(pull=altered))

    def test_failed_pr_ci_or_other_commit_ci_does_not_satisfy_main_ci(self):
        for run in (dict(head_sha=SHA, head_branch='main', event='pull_request', conclusion='success'),
                    dict(head_sha='b' * 40, head_branch='main', event='push', conclusion='success'),
                    dict(head_sha=SHA, head_branch='main', event='push', conclusion='failure')):
            with self.subTest(run=run), self.assertRaisesRegex(RuntimeError, 'Successful main push CI'):
                self.eligible(run=run)

    def test_squashed_release_to_main_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'two-parent'):
            self.eligible(parents=2)

    def test_old_history_is_not_automatically_backfilled(self):
        with patch.object(publisher, 'is_ancestor', return_value=True), \
             patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(publisher, 'pages') as api:
            self.assertIsNone(publisher.release_pr(REPO, SHA))
            api.assert_not_called()

    def test_sha_must_be_full_and_in_main(self):
        with self.assertRaisesRegex(RuntimeError, 'full immutable'):
            publisher.release_pr(REPO, 'main')
        with patch.object(publisher, 'is_ancestor', return_value=False), self.assertRaisesRegex(RuntimeError, 'not in main'):
            publisher.release_pr(REPO, SHA)


class TagAllocationTests(unittest.TestCase):
    def setUp(self):
        self.baseline = dict(name='v1.0.0', sha='b' * 40, metadata=None)

    def test_create_only_tag_reservation_targets_exact_merge_sha(self):
        with patch.object(publisher, 'version_tags', return_value=[self.baseline]), \
             patch.object(publisher, 'github', side_effect=[dict(sha='d' * 40), {}]) as api:
            result = publisher.reserve_version(REPO, SHA, pr(), 'patch')
        self.assertEqual(result['name'], 'v1.0.1')
        tag_call, ref_call = [call.args for call in api.call_args_list]
        self.assertIn('object=' + SHA, tag_call)
        self.assertIn('ref=refs/tags/v1.0.1', ref_call)
        self.assertIn('sha=' + 'd' * 40, ref_call)
        self.assertTrue(all(call[call.index('--method') + 1] == 'POST' for call in (tag_call, ref_call)))

    def test_reserved_tag_is_reused_after_publication_failure(self):
        with patch.object(publisher, 'version_tags', return_value=[self.baseline, reserved()]), \
             patch.object(publisher, 'github') as api:
            self.assertEqual(publisher.reserve_version(REPO, SHA, pr(), 'minor')['name'], 'v1.0.1')
            api.assert_not_called()

    def test_parallel_version_collision_allocates_another_version_without_moving_any_tag(self):
        other = dict(name='v1.0.1', sha='c' * 40, metadata=None)
        collision = subprocess.CalledProcessError(1, ['gh', 'api'])
        matching = [dict(ref='refs/tags/v1.0.1', object=dict(type='tag', sha='e' * 40))]
        with patch.object(publisher, 'version_tags', side_effect=[[self.baseline], [self.baseline, other]]), \
             patch.object(publisher, 'github', side_effect=[dict(sha='d' * 40), collision, matching,
                                                          dict(sha='f' * 40), {}]) as api:
            result = publisher.reserve_version(REPO, SHA, pr(), 'patch')
        self.assertEqual(result['name'], 'v1.0.2')
        self.assertEqual(result['metadata']['previous_tag'], 'v1.0.1')
        self.assertFalse(any('PATCH' in call.args or 'DELETE' in call.args for call in api.call_args_list))

    def test_network_failure_without_a_competing_ref_does_not_consume_another_version(self):
        with patch.object(publisher, 'version_tags', return_value=[self.baseline]), \
             patch.object(publisher, 'github', side_effect=[dict(sha='d' * 40),
                 subprocess.CalledProcessError(1, ['gh', 'api']), []]):
            with self.assertRaisesRegex(RuntimeError, 'without a competing ref'):
                publisher.reserve_version(REPO, SHA, pr(), 'patch')

    def test_no_baseline_does_not_guess_initial_version(self):
        with patch.object(publisher, 'version_tags', return_value=[]), \
             patch.object(publisher, 'github') as api:
            with self.assertRaisesRegex(RuntimeError, 'baseline'):
                publisher.reserve_version(REPO, SHA, pr(), 'patch')
            api.assert_not_called()

    def test_existing_draft_version_without_a_tag_is_not_appropriated(self):
        with patch.object(publisher, 'version_tags', return_value=[self.baseline]), \
             patch.object(publisher, 'github') as api:
            with self.assertRaisesRegex(RuntimeError, 'already belongs'):
                publisher.reserve_version(REPO, SHA, pr(), 'patch', {'v1.0.1'})
            api.assert_not_called()

    def test_annotated_tag_metadata_and_exact_commit_are_resolved(self):
        expected = reserved()
        ref = dict(ref='refs/tags/v1.0.1', object=dict(type='tag', sha='d' * 40))
        obj = dict(message=json.dumps(expected['metadata']), object=dict(type='commit', sha=SHA))
        with patch.object(publisher, 'github', return_value=obj):
            self.assertEqual(publisher.tag_info(REPO, ref), expected)

    def test_tag_cycles_are_rejected(self):
        ref = dict(ref='refs/tags/v1.0.1', object=dict(type='tag', sha='d' * 40))
        obj = dict(message='manual tag', object=dict(type='tag', sha='d' * 40))
        with patch.object(publisher, 'github', return_value=obj):
            with self.assertRaisesRegex(RuntimeError, 'too deep'):
                publisher.tag_info(REPO, ref)


class PublicationTests(unittest.TestCase):
    def api(self, *args):
        endpoint = args[1]
        if '/git/matching-refs/' in endpoint:
            return [dict(ref='refs/tags/v1.0.1', object=dict(type='commit', sha=SHA))]
        if endpoint.endswith('/git/ref/heads/main'):
            return dict(object=dict(sha=self.main))
        if endpoint.endswith('/releases'):
            return dict(html_url='https://github.com/choice/repo/releases/tag/v1.0.1')
        raise AssertionError(args)

    def test_publication_uses_reserved_tag_exact_sha_and_explicit_source_release_notes(self):
        self.main = SHA
        with patch.object(publisher, 'release_pr', return_value=pr()), \
             patch.object(publisher, 'pages', return_value=[]), \
             patch.object(publisher, 'version_tags', return_value=[]), \
             patch.object(publisher, 'reserve_version', return_value=reserved()), \
             patch.object(publisher, 'build_release_notes', return_value='VIP deployment is not asserted'), \
             patch.object(publisher, 'github', side_effect=self.api) as api:
            result = publisher.publish(REPO, SHA)
        self.assertEqual(result['status'], 'published')
        create = api.call_args_list[-1].args
        self.assertIn('target_commitish=' + SHA, create)
        self.assertIn('make_latest=true', create)
        self.assertTrue(any('VIP deployment is not asserted' in arg for arg in create))

    def test_older_ci_completion_cannot_deliberately_replace_latest_release(self):
        self.main = 'b' * 40
        with patch.object(publisher, 'release_pr', return_value=pr()), \
             patch.object(publisher, 'pages', return_value=[]), \
             patch.object(publisher, 'version_tags', return_value=[]), \
             patch.object(publisher, 'reserve_version', return_value=reserved()), \
             patch.object(publisher, 'build_release_notes', return_value='Clean release notes'), \
             patch.object(publisher, 'github', side_effect=self.api) as api:
            publisher.publish(REPO, SHA)
        self.assertIn('make_latest=false', api.call_args_list[-1].args)

    def test_successful_rerun_does_not_create_another_tag_or_edit_published_release(self):
        release = dict(tag_name='v1.0.1', draft=False, html_url='https://example.invalid/release')
        with patch.object(publisher, 'release_pr', return_value=pr()), \
             patch.object(publisher, 'pages', return_value=[release]), \
             patch.object(publisher, 'version_tags', return_value=[reserved()]), \
             patch.object(publisher, 'reserve_version') as reserve, patch.object(publisher, 'github') as api:
            result = publisher.publish(REPO, SHA)
        self.assertEqual(result['status'], 'already-published')
        reserve.assert_not_called()
        api.assert_not_called()

    def test_tag_movement_blocks_publication(self):
        with patch.object(publisher, 'release_pr', return_value=pr()), \
             patch.object(publisher, 'pages', return_value=[]), \
             patch.object(publisher, 'version_tags', return_value=[]), \
             patch.object(publisher, 'reserve_version', return_value=reserved()), \
             patch.object(publisher, 'github', return_value=[dict(ref='refs/tags/v1.0.1',
                 object=dict(type='commit', sha='b' * 40))]) as api:
            with self.assertRaisesRegex(RuntimeError, 'tag changed'):
                publisher.publish(REPO, SHA)
        self.assertEqual(api.call_count, 1)


if __name__ == '__main__':
    unittest.main()
