import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchd import config, models, quota
from orchd.runtime import WORKER_MODELS, worker_kind


class ModelCatalog(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "config.toml"
        patcher = mock.patch.dict(os.environ, {"ORCHD_CONFIG_FILE": str(self.file)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, text):
        self.file.write_text(text)

    def test_defaults_without_a_config_file(self):
        self.assertEqual(dict(WORKER_MODELS), {
            "terra": "gpt-6.1-terra", "sonnet": "claude-sonnet-5-5", "sol": "gpt-6.1-sol",
            "fable": "claude-fable-5-1", "astra": "gpt-6-astra"})
        self.assertEqual({a: e["tier"] for a, e in models.catalog().items()},
                         dict(terra=1, sonnet=1, sol=2, fable=3, astra=3))
        self.assertEqual(config.worker_backend("terra"), "app-server")
        self.assertEqual(config.worker_backend("sol"), "app-server")
        self.assertEqual(config.worker_backend("sonnet"), "exec")

    def test_quota_defaults_to_vendor_and_is_validated(self):
        self.assertEqual({a: e["quota"] for a, e in models.catalog().items()},
                         dict(terra="codex", sonnet="claude", sol="codex", fable="claude", astra="codex"))
        self.write('[models.mini]\nid = "gpt-7-mini"\nvendor = "codex"\nquota = "claude"\ntier = 1\n')
        self.assertEqual(models.catalog()["mini"]["quota"], "claude")
        self.assertEqual(models.quota_provider("mini"), "sonnet")
        self.write('[models.mini]\nid = "gpt-7-mini"\nvendor = "codex"\nquota = "sol"\ntier = 1\n')
        with self.assertRaisesRegex(ValueError, "quota must be one of"):
            models.catalog()

    def test_quota_mapping_drives_auto_selection(self):
        self.write('[models.terra]\nquota = "claude"\n')
        snap = {"sol": dict(state="known", windows=[dict(used_percent=10, state="known")]),
                "sonnet": dict(state="known", windows=[dict(used_percent=90, state="known")])}
        # terra now draws on the Claude bucket (90% used), sonnet too; the Codex bucket has no tier-1 alias.
        self.assertEqual(quota.shadow(snap, now=1)["selected_alias"], "terra")
        self.assertEqual(models.quota_provider("terra"), "sonnet")

    def test_review_pairing_ignores_the_tier_1_auto_filter(self):
        snap = {"sol": dict(state="known", windows=[dict(used_percent=10, state="known")]),
                "sonnet": dict(state="known", windows=[dict(used_percent=10, state="known")])}
        for author, reviewer in (("gpt-6.1-sol", "sonnet"), ("claude-fable-5-1", "astra"), ("gpt-6-astra", "fable")):
            got = quota.shadow(snap, task_type="review", author_model=author, applied=True, now=1)
            self.assertEqual(got["selected_alias"], reviewer, author)
        self.write('[models.terra]\ntier = 2\n')  # no tier-1 codex alias left; fable still pairs with astra/terra
        got = quota.shadow(snap, task_type="review", author_model="claude-fable-5-1", applied=True, now=1)
        self.assertEqual(got["selected_alias"], models.pair("claude-fable-5-1"))
        self.assertIsNotNone(got["selected_alias"])

    def test_config_changes_the_model_id_without_code(self):
        self.write('[models.terra]\nid = "gpt-6.2-terra"\n[models.mini]\nid = "gpt-7-mini"\nvendor = "codex"\ntier = 1\n')
        self.assertEqual(WORKER_MODELS["terra"], "gpt-6.2-terra")
        self.assertEqual(models.catalog()["terra"]["tier"], 1)  # unset fields keep the default
        self.assertIn("mini", WORKER_MODELS)
        self.assertEqual(worker_kind(WORKER_MODELS["mini"]), "codex")
        self.write('[models.terra]\nid = "gpt-6.3-terra"\n')  # a later edit is seen without a restart of the module
        self.assertEqual(WORKER_MODELS["terra"], "gpt-6.3-terra")

    def test_bad_config_fails_closed_naming_the_file(self):
        for text in ('[models.x]\nid = "gpt-1"\nvendor = "codex"\ntier = 0\n',
                     '[models.x]\nid = "claude-1"\nvendor = "codex"\ntier = 1\n',  # id does not match the vendor
                     '[models.x]\nid = "gpt-1"\nvendor = "other"\ntier = 1\n',
                     '[models.auto]\nid = "gpt-1"\nvendor = "codex"\ntier = 1\n',
                     '[models.x]\nid = "gpt-1"\nvendor = "codex"\ntier = 1\nextra = 1\n',
                     '[models]\nx = 1\n'):
            self.write(text)
            with self.assertRaises(ValueError, msg=text) as raised:
                models.catalog()
            self.assertIn(str(self.file), str(raised.exception))

    def test_pairing_and_escalation_ladder(self):
        pair = models.pair
        self.assertEqual([pair("terra"), pair("sonnet"), pair("sol"), pair("fable"), pair("astra")],
                         ["sonnet", "terra", "sonnet", "astra", "fable"])
        self.assertEqual(pair("gpt-6.1-terra"), "sonnet")  # full ids work too
        self.assertEqual(models.escalate("terra"), "sol")
        self.assertEqual(models.escalate("sonnet"), "sol")
        self.assertEqual(models.escalate("sol"), "fable")  # other vendor first
        with self.assertRaises(ValueError):
            models.escalate("fable")

    def test_effective_config_is_shown(self):
        self.write('[interface]\nmodel = "gpt-6.2-terra"\n')
        shown = config.show()
        self.assertEqual(shown["models"]["astra"]["id"], "gpt-6-astra")
        self.assertEqual(shown["interface"], {"model": "gpt-6.2-terra", "effort": "low"})
        self.assertEqual(config.interface_settings(), ("gpt-6.2-terra", "low"))
        self.file.unlink()
        self.assertEqual(config.interface_settings(), ("gpt-6.1-terra", "low"))
        self.write('[interface]\neffort = 3\n')
        with self.assertRaises(ValueError):
            config.interface_settings()


def snapshot(codex_used, claude_used):
    def known(used):
        return dict(state="known", windows=[dict(name="primary", used_percent=used, state="known")])
    return dict(sol=known(codex_used), sonnet=known(claude_used))


class ModelQuotaMapping(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ORCHD_CONFIG_FILE": "/nonexistent/orchd-config.toml"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_auto_picks_only_tier_one_by_remaining_quota(self):
        self.assertEqual(quota.shadow(snapshot(10, 10), applied=True)["selected_alias"], "terra")
        self.assertEqual(quota.shadow(snapshot(80, 10), applied=True)["selected_alias"], "sonnet")
        self.assertEqual(quota.shadow(snapshot(10, 80), applied=True)["selected_alias"], "terra")
        self.assertEqual(quota.shadow(snapshot(10, 10), backend="app-server", applied=True)["selected_alias"], "terra")

    def test_review_takes_the_cross_vendor_pair_of_the_author(self):
        for author, expected in (("gpt-6.1-terra", "sonnet"), ("claude-sonnet-5-5", "terra"), ("gpt-6.1-sol", "sonnet"),
                                 ("claude-fable-5-1", "astra"), ("gpt-6-astra", "fable")):
            got = quota.shadow(snapshot(10, 10), task_type="review", author_model=author, applied=True)
            self.assertEqual(got["selected_alias"], expected, author)

    def test_review_pair_on_a_full_provider_waits(self):
        got = quota.shadow(snapshot(10, 99), task_type="review", author_model="gpt-6.1-terra")
        self.assertIsNone(got["selected_alias"])


if __name__ == "__main__":
    unittest.main()
