import os
import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchd import cli, config, core, worker_flow


class WorkerFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'
        self.env = patch.dict(os.environ, {'ORCHD_CONFIG_FILE': str(self.config)})
        self.env.start()
        self.addCleanup(self.env.stop)
        worker_flow._fetches.clear()
        self.git('init', '-q', '--bare', str(self.root / 'adapter.git'))
        self.git('clone', '-q', str(self.root / 'adapter.git'), str(self.root / 'orchd'))
        self.commit_file(self.root / 'orchd', 'docs/worker-flow-adapter.md')
        self.git('-C', str(self.root / 'orchd'), 'push', '-q', 'origin', 'HEAD')
        self.git('-C', str(self.root / 'orchd'), 'remote', 'set-head', 'origin', '-a')
        self.git('init', '-q', '--bare', str(self.root / 'remote.git'))
        self.git('clone', '-q', str(self.root / 'remote.git'), str(self.root / 'hub'))
        self.commit_file(self.root / 'hub', 'docs/development/delivery.md')
        self.git('-C', str(self.root / 'hub'), 'push', '-q', 'origin', 'HEAD')
        self.git('-C', str(self.root / 'hub'), 'remote', 'set-head', 'origin', '-a')

    def git(self, *args):
        return subprocess.run(['git', *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit_file(self, repo, name):
        file = repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('flow contents')
        self.git('-C', str(repo), 'add', name)
        self.git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'fixture')

    def test_configured_flow_for_both_backends(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        sha = self.git('-C', str(self.root / 'hub'), 'rev-parse', 'HEAD')
        adapter_sha = self.git('-C', str(self.root / 'orchd'), 'rev-parse', 'HEAD')
        for kind in ('claude', 'codex'):
            with self.subTest(kind=kind):
                brief = core.worker_brief('orchd', kind, repo='product', projects=self.root)
                self.assertIn(f'origin SHA {sha}', brief)
                self.assertIn(f'orchd SHA {adapter_sha}', brief)
                self.assertIn(f'{sha}:docs/development/delivery.md', brief)
                self.assertIn('git -C', brief)
        self.assertIn(sha, core.app_brief(repo='product', projects=self.root))

    def test_unconfigured_only_has_adapter(self):
        brief = core.worker_brief('orchd', repo='product', projects=self.root)
        self.assertIn('Worker flow adapter:', brief)
        self.assertNotIn('Delivery flow:', brief)

    def test_missing_hub_and_failed_fetch_are_visible(self):
        self.config.write_text('[delivery_flows]\nproduct = "absent"\n')
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        worker_flow._fetches.clear()
        self.git('-C', str(self.root / 'hub'), 'remote', 'set-url', 'origin', str(self.root / 'absent'))
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))

    def test_missing_entry_is_visible(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        self.git('-C', str(self.root / 'hub'), 'rm', '-q', 'docs/development/delivery.md')
        self.git('-C', str(self.root / 'hub'), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 'commit', '-qm', 'remove')
        self.git('-C', str(self.root / 'hub'), 'push', '-q', 'origin', 'HEAD')
        self.assertIn('Delivery flow: missing', core.worker_brief('orchd', repo='product', projects=self.root))

    def test_config_validation_and_default(self):
        self.assertEqual(config.delivery_flows(), {})
        for text in ('delivery_flows = "bad"', '[delivery_flows]\n"../product" = "hub"',
                     '[delivery_flows]\nproduct = "../hub"', '[delivery_flows]\nproduct = 42', '[delivery_flows]\n"" = "hub"',
                     '[delivery_flows]\nproduct = "path\\\\hub"'):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(ValueError):
                    config.delivery_flows()

    def test_fetch_deduplicated_and_retry_resolves_again(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        original = worker_flow._git
        with patch.object(worker_flow, '_git', wraps=original) as git:
            for _ in range(4):
                brief = worker_flow.brief('product', self.root)
            fetches = [c for c in git.call_args_list if c.args[2:] == ('fetch', '--quiet', 'origin')]
            self.assertEqual(len(fetches), 2)
            self.assertIn('a retry may resolve different SHAs', brief)
            self.assertEqual(sum(c.args[2] == 'rev-parse' for c in git.call_args_list), 8)

    def test_timeout_budget_and_missing(self):
        self.config.write_text('[delivery_flows]\nproduct = "hub"\n')
        clock = [0.0]
        def slow(*args, **kwargs):
            timeout = kwargs['timeout']
            clock[0] += timeout
            self.assertEqual(kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
            raise subprocess.TimeoutExpired(args[0], timeout, stderr='timed out')
        with patch.object(worker_flow.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(worker_flow.subprocess, 'run', side_effect=slow):
            brief = worker_flow.brief('product', self.root)
        self.assertIn('Worker flow adapter: missing', brief)
        self.assertIn('Delivery flow: missing', brief)
        self.assertLessEqual(clock[0], worker_flow.BRIEF_BUDGET)
        self.assertLess(worker_flow.BRIEF_BUDGET, core.RETRY_LOCK_WAIT)

    def test_error_redacts_credentials(self):
        error = subprocess.CalledProcessError(1, ['git'], stderr='fatal https://user:secret@example.com/path')
        with patch.object(worker_flow, '_git', side_effect=error):
            brief = worker_flow.brief(None, self.root)
        self.assertIn('fatal https://***@example.com/path', brief)
        self.assertNotIn('secret', brief)


class SkillsReadTest(unittest.TestCase):
    def test_adapter_names_skill_paths_per_task_type_for_both_runtimes(self):
        text = (Path(__file__).resolve().parent.parent / worker_flow.ADAPTER).read_text(encoding="utf-8")
        for needle in ('~/.claude/skills/pstack-X/SKILL.md', '~/.agents/skills/pstack-X/SKILL.md',
                       'Read/cat', 'Skill tool and slash commands are disabled', 'Skills read:',
                       '| investigation |', '| other |', '| code |', '| review |',
                       'pstack-architect', 'pstack-tdd', 'pstack-blast-radius', 'pstack-why', 'pstack-how',
                       '~/.claude/skills/pstack-tdd/SKILL.md', '~/.agents/skills/pstack-how/SKILL.md'):
            self.assertIn(needle, text)
        for limit in ('do not start subagents', 'cross-vendor', 'merge'):
            self.assertIn(limit, text)

    def test_brief_requires_skills_read_line_for_both_kinds(self):
        for kind in ('claude', 'codex'):
            self.assertIn('`Skills read:` line', core.worker_brief('orchd', kind, projects=Path('/nonexistent')))

    def test_task_message_states_task_type(self):
        task = {'id': 't', 'repo': 'r', 'worktree': '/w', 'branch': 'b', 'title': 'T', 'instructions': 'i',
                'done_when': 'd', 'task_type': 'code'}
        self.assertIn('Task type: code', core.task_message(task))
        del task['task_type']
        self.assertIn('Task type: unspecified', core.task_message(task))

    def test_task_type_mapping_for_both_runtimes(self):
        text = (Path(__file__).resolve().parent.parent / worker_flow.ADAPTER).read_text(encoding="utf-8")
        rows = {line.split('|')[1].strip(): line for line in text.splitlines() if line.startswith('|')}
        expected = {
            'code': ('tdd', 'blast-radius', 'how', 'architect'),
            'investigation': ('how', 'why', 'architect'),
            'other': ('architect',), 'review': ('why', 'blast-radius'),
            'docs': (), 'outward': (), 'ops': (),
        }
        for kind, root in (('claude', '~/.claude/skills'), ('codex', '~/.agents/skills')):
            with patch('orchd.worker_flow.brief', return_value=text):
                brief = core.worker_brief('orchd', kind, projects=Path('/nonexistent'))
            self.assertIn(text, brief)
            self.assertIn(root + '/pstack-X/SKILL.md', text)
            for task_type, skills in expected.items():
                with self.subTest(kind=kind, task_type=task_type):
                    row = rows[task_type]
                    for skill in skills:
                        self.assertIn('pstack-' + skill, row)
                    if not skills:
                        self.assertIn('none', row)
        self.assertNotIn('| design |', text)

    def test_report_warning_requires_a_complete_line(self):
        cases = (('Mention Skills read: /tmp/a in prose', True),
                 (r'Skills read: C:\Work Spaces\skills\SKILL.md', False),
                 ('Skills read: /tmp/a', False),
                 ('Skills read: /a/SKILL.md, /b/SKILL.md', False),
                 ('Skills read: /a /b', False),
                 ('Skills read: /a, /b /c — used for this fix', False),
                 ('Skills read: /a/SKILL.md\r\n', False),
                 ('12. Skills read: /a/SKILL.md', False),
                 ('  - Skills read: /tmp/a', False),
                 ('* Skills read: none because no skills apply', False),
                 ('Skills read: none', True),
                 ('Skills read:', True))
        for evidence, warns in cases:
            with self.subTest(evidence=evidence):
                out, err = io.StringIO(), io.StringIO()
                with patch('orchd.cli.store.connect'), patch('orchd.cli.Runtime'), \
                        patch('orchd.cli.core.report', return_value=True) as report, \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    cli.main(['report', 'task', '--status', 'done', '--summary', 'done', '--evidence', evidence])
                self.assertEqual('warning:' in err.getvalue(), warns)
                self.assertEqual(report.call_args.args[-1], evidence)
