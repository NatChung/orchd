import json
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import review_feedback


class ReviewFeedbackTests(unittest.TestCase):
    def test_complete_ordered_feedback_and_thread_replies(self):
        data = json.loads((Path(__file__).parent / 'fixtures/review_feedback/data.json').read_text())
        with patch.object(review_feedback, '_rest', side_effect=[data['pr'], {'login': 'our-bot'}, data['reviews'], data['inline'], data['issue']]), patch.object(review_feedback, '_threads', return_value=data['threads']):
            items = review_feedback.fetch('owner/repo', 42)
        self.assertEqual([x['kind'] for x in items], ['review', 'inline', 'inline', 'issue'])
        self.assertTrue(items[0]['unanswered'])
        self.assertFalse(items[1]['unanswered'])
        self.assertTrue(items[1]['resolved'])
        self.assertTrue(items[3]['unanswered'])

    def test_resolved_and_responder_replies_answer_threads(self):
        data = json.loads((Path(__file__).parent / 'fixtures/review_feedback/data.json').read_text())
        for resolved, reply_author, expected in [(True, None, False), (False, 'aiagent-kc', False),
                                                  (False, 'author', False), (False, 'other', True)]:
            with self.subTest(resolved=resolved, reply_author=reply_author):
                inline = [dict(row) for row in data['inline']]
                if reply_author is None:
                    inline.pop()
                else:
                    inline[1]['user'] = {'login': reply_author, 'type': 'User'}
                threads = [{'isResolved': resolved, 'comments': {'nodes': [{'databaseId': row['id']} for row in inline]}}]
                with patch.object(review_feedback, '_rest', side_effect=[data['pr'], {'login': 'different-account'}, data['reviews'], inline, data['issue']]), patch.object(review_feedback, '_threads', return_value=threads):
                    items = review_feedback.fetch('owner/repo', 42)
                self.assertEqual(next(x for x in items if x['id'] == 2)['unanswered'], expected)

    def test_gh_pages_and_cli_output(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from subprocess import CompletedProcess
        from orchd import cli
        data = json.loads((Path(__file__).parent / 'fixtures/review_feedback/data.json').read_text())
        graph = {'data': {'repository': {'pullRequest': {'reviewThreads': {'nodes': data['threads']}}}}}
        responses = [[data['pr']], [{'login': 'our-bot'}], [data['reviews'][:1], []],
                     [data['inline']], [data['issue']], [graph]]
        calls = []
        def run(argv, **kwargs):
            calls.append(argv)
            return CompletedProcess(argv, 0, json.dumps(responses[len(calls) - 1]), '')
        with patch('subprocess.run', side_effect=run), redirect_stdout(StringIO()) as out:
            self.assertEqual(cli.main(['review-feedback', 'owner/repo', '42', '--json']), 0)
        self.assertEqual(len(json.loads(out.getvalue())), 4)
        self.assertTrue(all('--paginate' in call and '--slurp' in call for call in calls))

    def test_filters_do_not_hide_replies_used_for_answering(self):
        data = json.loads((Path(__file__).parent / 'fixtures/review_feedback/data.json').read_text())
        data['inline'][1]['user'] = {'login': 'our-bot', 'type': 'Bot'}
        with patch.object(review_feedback, '_rest', side_effect=[data['pr'], {'login': 'our-bot'}, data['reviews'], data['inline'], data['issue']]), patch.object(review_feedback, '_threads', return_value=data['threads']):
            items = review_feedback.fetch('owner/repo', 42, since='2026-01-02T00:00:00Z', hide_bots=True)
        self.assertEqual(len(items), 2)
        self.assertFalse(items[0]['unanswered'])

    def test_recorded_gh_issue_comment_pages(self):
        from subprocess import CompletedProcess
        recorded = (Path(__file__).parent / 'fixtures/review_feedback/issue-comments-gh.json').read_text()
        with patch('subprocess.run', return_value=CompletedProcess([], 0, recorded, '')):
            comments = review_feedback._rest('repos/NatChung/orchd/issues/146/comments?per_page=100', 'NatChung/orchd')
        self.assertTrue(comments)
        self.assertTrue(all('body' in item and 'created_at' in item for item in comments))
