"""Standalone branch creation uses main and can never reset an existing ref."""

from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import create_release_branch as creation


class ReleaseBranchCreationTests(unittest.TestCase):
    def test_creates_only_requested_branch_at_exact_main_sha(self):
        sha = 'a' * 40
        with patch.object(creation, 'matching_refs', return_value=[]), \
             patch.object(creation, 'remote_sha', return_value=sha) as refs, \
             patch.object(creation, 'github', return_value=dict(
                 ref='refs/heads/release-dev-224', object=dict(sha=sha))) as api:
            result = creation.create_release_branch('choice/repo', 'release-dev-224')
        self.assertEqual(result['sha'], sha)
        self.assertEqual(result['source'], 'main')
        self.assertEqual(refs.call_count, 2)
        api.assert_called_once_with('api', 'repos/choice/repo/git/refs', '--method', 'POST',
                                    '-f', 'ref=refs/heads/release-dev-224', '-f', 'sha=' + sha)

    def test_existing_branch_is_not_moved_even_when_it_already_matches_main(self):
        with patch.object(creation, 'matching_refs', return_value=[dict(object=dict(sha='a' * 40))]), \
             patch.object(creation, 'remote_sha') as refs, patch.object(creation, 'github') as api:
            with self.assertRaisesRegex(RuntimeError, 'already exists'):
                creation.create_release_branch('choice/repo', 'release-dev-223')
            refs.assert_not_called()
            api.assert_not_called()

    def test_invalid_names_fail_before_any_remote_reads_or_writes(self):
        for branch in ('main', 'staging', 'release-', 'feature/DEV-224', 'release-next:bad',
                       'release-next/..', 'release-next\nother', '--force'):
            with self.subTest(branch=branch), patch.object(creation, 'matching_refs') as refs, \
                 patch.object(creation, 'github') as api:
                with self.assertRaises(RuntimeError):
                    creation.create_release_branch('choice/repo', branch)
                refs.assert_not_called()
                api.assert_not_called()

    def test_main_change_blocks_creation(self):
        with patch.object(creation, 'matching_refs', return_value=[]), \
             patch.object(creation, 'remote_sha', side_effect=['a' * 40, 'b' * 40]), \
             patch.object(creation, 'github') as api:
            with self.assertRaisesRegex(RuntimeError, 'Main changed'):
                creation.create_release_branch('choice/repo', 'release-dev-224')
            api.assert_not_called()

    def test_missing_or_invalid_main_sha_blocks_creation(self):
        with patch.object(creation, 'matching_refs', return_value=[]), \
             patch.object(creation, 'remote_sha', return_value='not-a-sha'), \
             patch.object(creation, 'github') as api:
            with self.assertRaisesRegex(RuntimeError, 'full commit SHA'):
                creation.create_release_branch('choice/repo', 'release-dev-224')
            api.assert_not_called()

    def test_concurrent_branch_creation_is_not_overwritten_or_retried_as_an_update(self):
        error = subprocess.CalledProcessError(1, ['gh', 'api'], stderr='Reference already exists')
        with patch.object(creation, 'matching_refs', return_value=[]), \
             patch.object(creation, 'remote_sha', return_value='a' * 40), \
             patch.object(creation, 'github', side_effect=error) as api:
            with self.assertRaises(subprocess.CalledProcessError):
                creation.create_release_branch('choice/repo', 'release-dev-224')
            self.assertEqual(api.call_count, 1)


if __name__ == '__main__':
    unittest.main()
