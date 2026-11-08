import unittest
from unittest.mock import patch
from orchd import pr_watch


class BranchNamesTest(unittest.TestCase):
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
