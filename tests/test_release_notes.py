"""Release entries come from deployed squashes, excluding promotion merges and attribution."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.github/scripts'))
import release_notes as notes


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        previous = os.getcwd()
        os.chdir(self.directory.name)
        self.addCleanup(os.chdir, previous)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Example Author')
        self.git('config', 'user.email', 'author@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.hooksPath', '/dev/null')
        Path('base.txt').write_text('baseline\n')
        self.commit('[DEV-201] - Baseline')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('tag', 'v1.0.0')
        self.git('switch', '-c', 'feature/DEV-224')
        Path('notes.txt').write_text('notes\n')
        self.commit('[DEV-224] - Feature implementation detail')
        Path('tests.txt').write_text('tests\n')
        self.commit('[DEV-224] - Feature test detail')
        self.git('switch', '-c', 'staging', self.base)
        self.git('merge', '--no-ff', 'feature/DEV-224', '-m', 'Merge pull request #70 into staging')
        self.git('switch', '-c', 'release-dev-224', self.base)
        self.git('merge', '--squash', 'feature/DEV-224')
        self.commit('[DEV-224] - Cleaner release notes (#71)')
        self.git('switch', 'main')
        self.git('merge', '--no-ff', 'release-dev-224', '-m', 'Merge pull request #72 into main')
        self.head = self.git('rev-parse', 'HEAD')
        self.tags = [dict(name='v1.0.0', sha=self.base)]
        self.releases = [dict(tag_name='v1.0.0', draft=False, prerelease=False)]

    def git(self, *args):
        return subprocess.check_output(['git', *args], text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, subject):
        self.git('add', '.')
        self.git('commit', '-m', subject)

    def render(self):
        return notes.build_release_notes('choice/repo', self.head, 'release-dev-224', 'v1.0.2',
                                         self.tags, self.releases)

    def test_squash_is_listed_once_without_staging_release_or_main_promotion_duplicates(self):
        body = self.render()
        self.assertEqual([line for line in body.splitlines() if line.startswith('- ')],
                         ['- [DEV-224] - Cleaner release notes'])
        for unwanted in ('Feature implementation detail', 'Feature test detail', 'Merge pull request',
                         '#70', '#71', '#72', 'Example Author', 'author@example.invalid'):
            self.assertNotIn(unwanted, body)
        self.assertIn('VIP deployment is not asserted', body)
        self.assertIn(self.head, body)
        self.assertIn('https://github.com/choice/repo/compare/v1.0.0...v1.0.2', body)

    def test_exact_duplicates_are_removed_but_distinct_changes_for_same_ticket_remain(self):
        for index, title in enumerate(('[DEV-224] - Fix formatting (#73)',
                                        '[DEV-224] - Fix formatting (#74) (#75)',
                                        '[DEV-224] - Improve searchability (#76)')):
            Path(f'change-{index}.txt').write_text(title)
            self.commit(title)
        self.head = self.git('rev-parse', 'HEAD')
        entries = notes.change_titles(self.base, self.head)
        self.assertEqual(entries, ['[DEV-224] - Cleaner release notes', '[DEV-224] - Fix formatting',
                                   '[DEV-224] - Improve searchability'])

    def test_meaningful_issue_references_inside_description_are_preserved(self):
        Path('fix.txt').write_text('fix\n')
        self.commit('[DEV-224] - Handle issue #42 consistently (#77)')
        self.head = self.git('rev-parse', 'HEAD')
        self.assertIn('[DEV-224] - Handle issue #42 consistently', notes.change_titles(self.base, self.head))

    def test_baseline_is_nearest_published_ancestor_not_largest_version_number(self):
        previous = self.head
        Path('next.txt').write_text('next\n')
        self.commit('[DEV-225] - Next release')
        self.head = self.git('rev-parse', 'HEAD')
        self.tags = [dict(name='v9.0.0', sha=self.base), dict(name='v2.0.0', sha=previous)]
        self.releases = [dict(tag_name='v9.0.0'), dict(tag_name='v2.0.0')]
        body = self.render()
        self.assertNotIn('Cleaner release notes', body)
        self.assertIn('- [DEV-225] - Next release', body)
        self.assertIn('compare/v2.0.0...v1.0.2', body)

    def test_later_ci_completion_ignores_newer_descendant_release_tag(self):
        Path('later.txt').write_text('later\n')
        self.commit('[DEV-225] - Later merge')
        newer = self.git('rev-parse', 'HEAD')
        self.tags.append(dict(name='v1.0.1', sha=newer))
        self.releases.append(dict(tag_name='v1.0.1'))
        body = self.render()  # Rendering older self.head, not newer Git HEAD.
        self.assertIn('Cleaner release notes', body)
        self.assertNotIn('Later merge', body)
        self.assertIn('compare/v1.0.0...v1.0.2', body)

    def test_unpublished_reserved_tag_does_not_hide_unpublished_changes(self):
        self.tags.append(dict(name='v1.0.1', sha=self.head))
        Path('next.txt').write_text('next\n')
        self.commit('[DEV-225] - Next change')
        self.head = self.git('rev-parse', 'HEAD')
        self.assertIn('Cleaner release notes', self.render())
        self.assertIn('Next change', self.render())

    def test_no_ancestor_baseline_fails_instead_of_producing_misleading_empty_notes(self):
        self.tags = [dict(name='v1.0.0', sha=self.head)]
        with self.assertRaisesRegex(RuntimeError, 'No ancestor release tag'):
            self.render()


if __name__ == '__main__':
    unittest.main()
