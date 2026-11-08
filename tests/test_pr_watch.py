import unittest
import json
import subprocess
from unittest.mock import patch

from orchd import pr_watch


class WatchTest(unittest.TestCase):
    def test_reviews_and_unanswered_comments(self):
        pr = dict(number=1, title='AI', html_url='pr', user={'login': 'author'},
                  head={'ref': 'orchd/1234abcd', 'sha': 'abc'}, labels=[])
        reviews = [dict(user={'login': 'reviewer'}, state=state, submitted_at=f'2026-01-0{i}T00:00:00Z', html_url='review')
                   for i, state in enumerate(['APPROVED', 'COMMENTED', 'CHANGES_REQUESTED'], 1)]
        comment = dict(id=1, user={'login': 'reviewer'}, created_at='2026-01-05T00:00:00Z', body='Fix this', html_url='comment')
        data = [[pr], {'commit': {'committer': {'date': '2026-01-04T00:00:00Z'}}}, [], reviews, [comment], []]
        with patch.object(pr_watch, '_get', side_effect=data):
            result = pr_watch.watch(['owner/repo'])[0]
        self.assertEqual(result['reviews']['reviewer']['state'], 'CHANGES_REQUESTED')
        self.assertEqual(result['task_id'], '1234abcd')
        self.assertEqual(result['unanswered'][0]['body'], 'Fix this')
        self.assertIn('requested changes', result['suggested_action'])

    def test_no_ai_prs(self):
        with patch.object(pr_watch, '_get', return_value=[{'head': {'ref': 'feature'}, 'labels': []}]):
            self.assertEqual(pr_watch.watch(['owner/repo']), [])

    def test_label_and_owner_reply(self):
        pr = dict(number=2, title='AI', html_url='pr', user={'login': 'author'},
                  head={'ref': 'feature', 'sha': 'abc'}, labels=[{'name': 'ai-agent'}])
        def comment(id, who, date):
            return dict(id=id, user={'login': who}, created_at=date, body='text', html_url='comment')
        data = [[pr], {'commit': {'committer': {'date': '2026-01-01T00:00:00Z'}}}, [], [], [],
                [comment(1, 'reviewer', '2026-01-02T00:00:00Z'), comment(2, 'author', '2026-01-03T00:00:00Z')]]
        with patch.object(pr_watch, '_get', side_effect=data):
            result = pr_watch.watch(['owner/repo'])[0]
        self.assertEqual(result['unanswered'], [])
        self.assertIsNone(result['push_cutoff'])
        self.assertTrue(result['push_cutoff_uncertain'])
        self.assertIsNone(result['task_id'])

    def test_paginated_gh_json_and_read_only_command(self):
        pages = [[{'number': 1}], [{'number': 2}]]
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(pages), stderr='')
        with patch('orchd.pr_watch.subprocess.run', return_value=result) as run:
            self.assertEqual(pr_watch._get('repos/o/r/pulls?state=open'), pages[0] + pages[1])
        self.assertEqual(run.call_args.args[0], ['gh', 'api', '--paginate', '--slurp', 'repos/o/r/pulls?state=open'])

    def test_approval_and_comment_decisions_remain_distinct(self):
        pr = dict(number=1, title='AI', html_url='pr', user={'login': 'author'},
                  head={'ref': 'orchd/1234abcd', 'sha': 'abc'}, labels=[])
        reviews = [dict(user={'login': name}, state=state, submitted_at='2026-01-02T00:00:00Z', html_url='review')
                   for name, state in [('alice', 'APPROVED'), ('bob', 'COMMENTED')]]
        data = [[pr], {'commit': {'committer': {'date': '2026-01-01T00:00:00Z'}}}, [], reviews, [], []]
        with patch.object(pr_watch, '_get', side_effect=data):
            result = pr_watch.watch(['owner/repo'])[0]
        self.assertEqual(result['reviews']['alice']['state'], 'APPROVED')
        self.assertEqual(result['reviews']['bob']['state'], 'COMMENTED')

    def test_timeline_cutoff_uses_head_commit_and_force_push(self):
        pr = dict(number=3, title='AI', html_url='pr', user={'login': 'author'},
                  head={'ref': 'orchd/1234abcd', 'sha': 'abc'}, labels=[])
        def comment(id, date):
            return dict(id=id, user={'login': 'reviewer'}, created_at=date, body='text', html_url='comment')
        timeline = [
            {'event': 'committed', 'sha': 'abc', 'committer': {'date': '2026-01-02T00:00:00Z'}},
            {'event': 'committed', 'sha': 'old', 'committer': {'date': '2026-01-09T00:00:00Z'}},
            {'event': 'head_ref_force_pushed', 'created_at': '2026-01-04T00:00:00Z'},
            {'event': 'head_ref_restored', 'created_at': '2026-01-03T00:00:00Z'},
        ]
        def get(endpoint):
            if '/timeline?' in endpoint: return timeline
            if '/pulls?state=' in endpoint: return [pr]
            if '/commits/' in endpoint: return {'commit': {'committer': {'date': '2026-01-02T00:00:00Z'}}}
            if '/reviews?' in endpoint or '/pulls/3/comments?' in endpoint: return []
            if '/issues/3/comments?' in endpoint: return [comment(1, '2026-01-03T00:00:00Z'), comment(2, '2026-01-05T00:00:00Z')]
            self.fail(endpoint)
        with patch.object(pr_watch, '_get', side_effect=get):
            result = pr_watch.watch(['owner/repo'])[0]
        self.assertEqual(result['push_cutoff'], '2026-01-04T00:00:00Z')
        self.assertFalse(result['push_cutoff_uncertain'])
        self.assertEqual([c['id'] for c in result['unanswered']], [2])

    def test_later_comment_keeps_changes_requested(self):
        pr = dict(number=1, title='AI', html_url='pr', user={'login': 'author'},
                  head={'ref': 'orchd/1234abcd', 'sha': 'abc'}, labels=[])
        reviews = [dict(user={'login': 'reviewer'}, state=state, submitted_at=f'2026-01-0{i}T00:00:00Z', html_url='review')
                   for i, state in enumerate(['CHANGES_REQUESTED', 'COMMENTED'], 2)]
        data = [[pr], {'commit': {'committer': {'date': '2026-01-01T00:00:00Z'}}}, [], reviews, [], []]
        with patch.object(pr_watch, '_get', side_effect=data):
            result = pr_watch.watch(['owner/repo'])[0]
        self.assertEqual(result['reviews']['reviewer']['state'], 'CHANGES_REQUESTED')
        self.assertIn('requested changes', result['suggested_action'])

    def test_configured_accounts_count_as_replies(self):
        pr = dict(number=2, title='AI', html_url='pr', user={'login': 'author'},
                  head={'ref': 'orchd/1234abcd', 'sha': 'abc'}, labels=[])
        def comment(id, who, date):
            return dict(id=id, user={'login': who}, created_at=date, body='text', html_url='comment')
        issue = [comment(1, 'reviewer', '2026-01-02T00:00:00Z'), comment(2, 'helper-bot', '2026-01-03T00:00:00Z')]
        data = [[pr], {'commit': {'committer': {'date': '2026-01-01T00:00:00Z'}}}, [], [], [], issue]
        with patch.object(pr_watch, '_get', side_effect=data):
            self.assertEqual([c['id'] for c in pr_watch.watch(['owner/repo'])[0]['unanswered']], [1, 2])
        with patch.object(pr_watch, '_get', side_effect=data):
            self.assertEqual(pr_watch.watch(['owner/repo'], accounts=['Helper-Bot'])[0]['unanswered'], [])

    def test_later_approval_hides_older_review_bodies(self):
        pr = dict(number=1, title='AI', html_url='pr', user={'login': 'author'},
                  head={'ref': 'orchd/1234abcd', 'sha': 'abc'}, labels=[])
        reviews = [dict(user={'login': name}, state=state, body=body, submitted_at=date, html_url='review')
                   for name, state, body, date in [('alice', 'CHANGES_REQUESTED', 'old', '2026-01-02T00:00:00Z'),
                                                   ('alice', 'APPROVED', 'lgtm', '2026-01-03T00:00:00Z'),
                                                   ('bob', 'COMMENTED', 'still open', '2026-01-02T00:00:00Z')]]
        data = [[pr], {'commit': {'committer': {'date': '2026-01-01T00:00:00Z'}}}, [], reviews, [], []]
        with patch.object(pr_watch, '_get', side_effect=data):
            result = pr_watch.watch(['owner/repo'])[0]
        self.assertEqual(sorted(c['body'] for c in result['unanswered']), ['lgtm', 'still open'])
