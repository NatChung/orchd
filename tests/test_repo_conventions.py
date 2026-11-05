import tempfile
import unittest
from pathlib import Path

from orchd import core


class RepoConventionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def add(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('convention contents')

    def test_finds_conventions_and_case_variants(self):
        names = ['AGENTS.md', 'CLAUDE.md', '.claude/skills/pr/SKILL.md',
                 '.claude/commands/pr.md', '.github/Pull_Request_Template.MD',
                 'PULL_REQUEST_TEMPLATE.md']
        for name in names:
            self.add(name)
        self.add('README.md')
        self.assertCountEqual(core.repo_conventions(self.root), names)

    def test_empty(self):
        self.assertEqual(core.repo_conventions(self.root), [])
        self.assertNotIn('Repo conventions found:', core.worker_brief('orchd', worktree=self.root))

    def test_list_is_capped_and_deterministic(self):
        for i in range(core.CONVENTION_LIMIT + 5):
            self.add(f'.claude/commands/pr{i:03}.md')
        found = core.repo_conventions(self.root)
        self.assertEqual(len(found), core.CONVENTION_LIMIT)
        self.assertEqual(found, sorted(found))

    def test_briefs_include_paths_and_guidance_for_each_backend(self):
        self.add('.claude/skills/pr/SKILL.md')
        self.add('CLAUDE.md')
        for brief in (core.worker_brief('orchd', 'claude', self.root),
                      core.worker_brief('orchd', 'codex', self.root), core.app_brief(self.root)):
            self.assertIn('Repo conventions found: .claude/skills/pr/SKILL.md, CLAUDE.md.', brief)
            self.assertIn('Before opening or editing a PR', brief)
            self.assertIn('read CLAUDE.md if your runtime', brief)
            self.assertNotIn('convention contents', brief)
