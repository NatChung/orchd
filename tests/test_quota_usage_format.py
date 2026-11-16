import datetime
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import quota

NOW = datetime.datetime(2026, 11, 12, 4, tzinfo=datetime.timezone.utc).timestamp()  # Nov 12 12:00 Asia/Taipei
SESSION = 'Current session: 5% used · resets Nov 12, 4:30pm (Asia/Taipei)'
WEEK = 'Current week (all models): 17% used · resets Nov 15, 1am (Asia/Taipei)'


def result_for(display):
    return subprocess.CompletedProcess([], 0, json.dumps(dict(
        result=display, num_turns=0, local_command='usage', duration_api_ms=0)), '')


class ClaudeUsageDateFormatTest(unittest.TestCase):
    def parse(self, text, now=NOW):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            quota.claude_usage(cache, text, now=now)
            return cache.status(now=now)['sonnet']['windows']

    def test_comma_format_parses_both_windows(self):
        session, week = self.parse(SESSION + '\n' + WEEK)
        self.assertEqual((session['used_percent'], week['used_percent']), (5, 17))
        taipei = datetime.timezone(datetime.timedelta(hours=8))
        self.assertEqual(session['reset_at'], datetime.datetime(2026, 11, 12, 16, 30, tzinfo=taipei).timestamp())
        self.assertEqual(week['reset_at'], datetime.datetime(2026, 11, 15, 1, tzinfo=taipei).timestamp())

    def test_legacy_at_format_still_parses(self):
        session, week = self.parse(SESSION.replace(', ', ' at ') + '\n' + WEEK.replace(', ', ' at '))
        self.assertEqual((session['used_percent'], week['used_percent']), (5, 17))

    def test_comma_and_at_formats_give_same_reset(self):
        a = self.parse(SESSION)[0]['reset_at']
        b = self.parse(SESSION.replace(', ', ' at '))[0]['reset_at']
        self.assertEqual(a, b)

    def test_missing_fields_stay_unknown_without_fabrication(self):
        session, week = self.parse(WEEK)
        self.assertEqual((session['state'], session['used_percent']), ('unknown', None))
        self.assertNotIn('reset_at', session)
        self.assertEqual(week['used_percent'], 17)
        for text in ('', 'Current session: 5% used', 'Current session: 5% used · resets Nov 12,'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.parse(text)

    def test_invalid_comma_data_is_rejected(self):
        bad = [SESSION.replace('5%', '101%'), SESSION.replace('5%', '-1%'), SESSION.replace('Nov 12', 'Nov 32'),
               SESSION.replace('4:30pm', '13pm'), SESSION.replace('4:30pm', '5:99pm'),
               SESSION.replace('Nov 12', 'Nov 1'), SESSION.replace('Nov 12', 'Xyz 12'),
               SESSION.replace('Taipei', 'PrivateZone'), SESSION.replace(', 4:30pm', ',4:30pm'),
               SESSION.replace('Nov 12,', 'Nov 12 ,')]
        for line in bad:
            with self.subTest(line=line):
                session, week = self.parse(line + '\n' + WEEK)
                self.assertEqual((session['state'], session['used_percent']), ('unknown', None))
                self.assertNotIn('reset_at', session)
                self.assertEqual(week['used_percent'], 17)
                with self.assertRaises(ValueError):
                    self.parse(line)


class ClaudeUsageRecoveryTest(unittest.TestCase):
    def test_recovery_after_unsupported_display_refreshes_receipt_age_and_auto_balances(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            old = NOW - 4.7 * 86400
            quota.claude_usage(cache, 'Current session: 5% used · resets Nov 8 at 4am (Asia/Taipei)\n'
                               'Current week (all models): 17% used · resets Nov 10 at 1am (Asia/Taipei)', now=old)
            cache.codex({'rateLimits': {'primary': {'usedPercent': 90, 'windowDurationMins': 300,
                'resetsAt': NOW + 10000}}}, now=NOW)
            self.assertEqual(cache.status(now=NOW)['sonnet']['state'], 'unknown')  # stale
            with patch.object(quota, '_run', return_value=result_for('weird')), \
                    patch.object(quota.time, 'time', return_value=NOW):
                self.assertFalse(quota.probe(cache, 'sonnet'))
            stale = cache.status(now=NOW)['sonnet']
            self.assertEqual({w['observed_at'] for w in stale['windows']}, {old})  # age not refreshed by failure
            self.assertNotEqual(stale['state'], 'known')
            display = 'Current session: 5% used · resets Nov 12, 4:30pm (Asia/Taipei)\n' + WEEK
            with patch.object(quota, '_run', return_value=result_for(display)), \
                    patch.object(quota.time, 'time', return_value=NOW + 300):
                self.assertTrue(quota.probe(cache, 'sonnet'))
            snap = cache.status(now=NOW + 300)
            self.assertEqual(snap['sonnet']['state'], 'known')
            self.assertEqual({w['observed_at'] for w in snap['sonnet']['windows']}, {NOW + 300})
            # Sonnet (95% left) vs sol (10% left): auto must now pick sonnet, not terra.
            self.assertEqual(quota.shadow(snap, now=NOW + 300, applied=True)['selected_alias'], 'sonnet')
            cache.codex({'rateLimits': {'primary': {'usedPercent': 5, 'windowDurationMins': 300,
                'resetsAt': NOW + 10000}}}, now=NOW + 300)
            snap = cache.status(now=NOW + 300)
            self.assertEqual(quota.shadow(snap, now=NOW + 300, applied=True)['selected_alias'], 'terra')

    def test_missing_executable_gives_identifiable_deidentified_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            with patch.object(quota, '_run', side_effect=FileNotFoundError(2, 'secret C:\\Users\\bob\\claude')):
                self.assertFalse(quota.probe(cache, 'sonnet', binary='claude'))
            status = cache.status(now=NOW)['sonnet']
            text = json.dumps(status)
            self.assertIn('executable not found', text)
            self.assertNotIn('bob', text)
            self.assertNotIn('secret', text)



class ClaudeUsageDecodingTest(unittest.TestCase):
    """Windows cp950 locale decodes the UTF-8 middle dot as U+7E5A unless encoding is explicit (#226 follow-up)."""

    def test_probe_requests_utf8_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = quota.Cache(Path(tmp) / 'quota.db')
            with patch.object(quota, '_run', return_value=result_for(SESSION + '\n' + WEEK)) as run, \
                    patch.object(quota.time, 'time', return_value=NOW):
                self.assertTrue(quota.probe(cache, 'sonnet'))
            self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')
            self.assertEqual(run.call_args.kwargs['errors'], 'replace')
            self.assertEqual(cache.status(now=NOW)['sonnet']['state'], 'known')

    def test_run_decodes_utf8_bytes_with_middle_dot_independent_of_locale(self):
        script = "import sys; sys.stdout.buffer.write('5% used \u00b7 resets'.encode('utf-8'))"
        done = quota._run([sys.executable, '-c', script], 30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding='utf-8', errors='replace')
        self.assertEqual(done.stdout, '5% used · resets')
        self.assertNotIn('繚', done.stdout)
