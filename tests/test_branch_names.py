import unittest
from unittest.mock import patch
from orchd import branch_names, pr_watch


class BranchNamesTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(branch_names.choose('Clear café / cache!', 'jira:SF-1913', 'code', '1234abcd', set()), 'SF-1913-clear-caf-cache')
        self.assertEqual(branch_names.choose('中文', None, 'docs', '1234abcd', set()), 'docs-1234abcd')
        self.assertEqual(branch_names.choose('Fix cache', None, 'code', '1234abcd', set()), 'fix-fix-cache')
        self.assertLessEqual(len(branch_names.choose('word ' * 100, None, 'code', '1234abcd', set())), 60)

    def test_collision(self):
        self.assertEqual(branch_names.choose('cache', 'jira:SF-1', 'code', '1234abcd', {'SF-1-cache'}), 'SF-1-cache-1234abcd')
        with self.assertRaises(ValueError):
            branch_names.choose('cache', 'jira:SF-1', 'code', '1234abcd', {'SF-1-cache', 'SF-1-cache-1234abcd'})

    def test_watcher_matches_recorded_remote(self):
        pr = dict(number=1, title='AI', html_url='pr', user={'login': 'author'}, head={'ref': 'SF-1-cache', 'sha': 'abc', 'repo': {'full_name': 'owner/repo'}}, labels=[])
        data = [[pr], {'commit': {'committer': {'date': '2026-01-01T00:00:00Z'}}}, [], [], [], []]
        with patch.object(pr_watch, '_get', side_effect=data):
            result = pr_watch.watch(['owner/repo'], {('owner/repo', 'SF-1-cache'): '1234abcd'})
        self.assertEqual(result[0]['task_id'], '1234abcd')

    def test_watcher_does_not_match_a_fork(self):
        pr = dict(number=1, title='AI', html_url='pr', user={'login': 'author'}, head={'ref': 'SF-1-cache', 'sha': 'abc', 'repo': {'full_name': 'other/repo'}}, labels=[])
        with patch.object(pr_watch, '_get', return_value=[pr]):
            self.assertEqual(pr_watch.watch(['owner/repo'], {('owner/repo', 'SF-1-cache'): '1234abcd'}), [])
