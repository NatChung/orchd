import json
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from orchd import stats, store

T0 = 1_000_000.0  # window start used by most tests


def iso(epoch, offset=""):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + (offset or "Z")


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.con = store.connect(self.root / "t.db")
        self.addCleanup(self.con.close)
        self.claude, self.codex = self.root / "claude", self.root / "codex"
        (self.claude / "-p").mkdir(parents=True)
        self.codex.mkdir()

    def task(self, tid, orch="o1", at=T0 + 10, model="claude-sonnet-5-5", **fields):
        store.create_task(self.con, id=tid, repo="r", repo_path="/r", title="T", instructions="i", done_when="d",
                          orch_thread=orch, codex_bin="c", model=model)
        sets = dict(created_at=at, **fields)
        self.con.execute(f"UPDATE tasks SET {','.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), tid))

    def msg(self, tid, kind, body="x"):
        return store.add_message(self.con, tid, kind, body if isinstance(body, str) else json.dumps(body))

    def close(self, tid, outcome, usage=None, model="claude-sonnet-5-5"):
        if usage:
            self.msg(tid, "usage", dict(model=model, **usage))
        self.con.execute("UPDATE tasks SET status='closed', outcome=? WHERE id=?", (outcome, tid))

    def report(self, since=T0):
        return stats.stats(self.con, since, self.claude, self.codex)

    def total(self, orch="o1", **kw):
        (row,) = [r for r in self.report(**kw)["orchs"] if r["orch_id"] == orch]
        return row["total"]

    # -- counts ----------------------------------------------------------------------------------

    def test_counts_by_orch_type_status_model_and_cohort_boundary(self):
        store.register_orch(self.con, "o1", "claude", model="claude-opus-5-5", session_id="s1")
        self.task("a", task_type="code")
        self.task("b", task_type="code", model="gpt-6.1-sol")
        self.task("c", task_type="docs", orch="o2")
        self.task("old", at=T0 - 1, task_type="code")  # before the window: excluded
        self.task("edge", at=T0, task_type="docs")     # exactly at since: included
        self.close("a", "merged")
        self.close("b", "abandoned")
        rep = self.report()
        by_orch = {r["orch_id"]: r for r in rep["orchs"]}
        self.assertEqual(set(by_orch), {"o1", "o2"})
        o1 = by_orch["o1"]
        self.assertEqual(o1["total"]["tasks"], 3)
        self.assertEqual(o1["total"]["status"], {"closed": 2, "starting": 1})
        self.assertEqual(o1["total"]["model"], {"claude-sonnet-5-5": 2, "gpt-6.1-sol": 1})
        self.assertEqual({k: v["tasks"] for k, v in o1["by_task_type"].items()}, {"code": 2, "docs": 1})
        # completed = closed with merged/done; abandoned and open do not count
        self.assertEqual(o1["total"]["completed"], 1)
        self.assertEqual(by_orch["o2"]["by_task_type"]["docs"]["tasks"], 1)

    def test_closed_without_outcome_is_not_completed_and_is_visible(self):
        self.task("a")
        self.close("a", None)
        t = self.total()
        self.assertEqual((t["completed"], t["closed_without_outcome"]), (0, 1))
        self.assertIsNone(t["cost_per_completed_usd"])  # no completed task: unknown, not 0

    # -- questions / rework ----------------------------------------------------------------------

    def test_questions_answers_and_rework_found_by_split(self):
        self.task("a")
        self.msg("a", "question"), self.msg("a", "question"), self.msg("a", "answer")
        self.task("r1", rework_of="a", found_by="nat")
        self.task("r2", rework_of="a", found_by="review")
        self.task("r3", rework_of="a")  # found_by not recorded
        t = self.total()
        self.assertEqual((t["questions"], t["answers"]), (2, 1))
        self.assertEqual(t["rework_tasks"], 3)
        self.assertEqual(t["rework_found_by"], {"nat": 1, "review": 1, "unknown": 1})
        self.assertEqual(t["rework_found_by_nat"], 1)
        self.assertEqual(t["rework_rate"], 3.0)  # 3 reworks over 1 original

    def test_no_tasks_gives_no_rate(self):
        self.task("r1", rework_of="zzz")
        self.assertIsNone(self.total()["rework_rate"])  # only rework rows: no original to divide by

    # -- retry / followup / verify: only from events that exist ------------------------------------

    def test_missing_event_kinds_are_unknown_not_zero(self):
        self.task("a")
        self.close("a", "merged")
        t = self.total()
        for key in ("retries", "followups", "escalations", "verify_events", "first_pass_verify_rate",
                    "proxy_no_retry_rate"):
            self.assertIsNone(t[key], key)
        self.assertEqual(t["proxy_no_rework_rate"], 1.0)  # rework is in tasks columns, so it is knowable

    def test_retry_followup_verify_events(self):
        for tid in ("a", "b", "c"):
            self.task(tid)
            self.close(tid, "merged")
        self.msg("a", "retry", {"to": "opus"}), self.msg("a", "followup"), self.msg("a", "followup")
        self.msg("b", "verify", {"exit_code": 1}), self.msg("b", "verify", {"exit_code": 0})
        self.msg("c", "verify", {"exit_code": 0})
        self.msg("a", "verify", {"no_exit_code": True})  # unparseable verify: counted as an event, not a rate input
        t = self.total()
        # {"to": "opus"} names no origin model: unresolved, so not counted as an escalation
        self.assertEqual((t["retries"], t["followups"], t["escalations"], t["escalations_unresolved"]), (1, 2, 0, 1))
        self.assertFalse(t["escalations_complete"])
        self.assertEqual((t["verify_events"], t["first_pass_verify_n"], t["first_pass_verify_rate"]), (4, 2, 0.5))
        self.assertEqual(t["proxy_no_retry_rate"], round(2 / 3, 4))
        self.assertEqual(t["retries_per_task"], round(1 / 3, 4))

    def lock(self, tid, sha):
        return store.add_message(self.con, tid, "verify_lock", "locked", json.dumps(
            dict(sha=sha, paths={"file": "blob"}, command="test")))

    def verification(self, tid, lock_id, sha, code=0, **overrides):
        record = dict(verifier="v-" + tid, verifier_session="s-verifier", author=tid,
                      lock_id=lock_id, sha=sha, command="test", exit=code, tail="",
                      hash_ok=True, dirty=False, head_after=sha, cleanup=True,
                      restored=True, restore_error=None, passed=code == 0)
        record.update(overrides)
        return store.add_message(self.con, tid, "verification", "pass or fail", json.dumps(record))

    def test_current_verification_first_fail_then_pass_and_first_pass(self):
        for tid in ("a", "b"):
            self.task(tid, task_type=tid)
        lock = self.lock("a", "sha-a")
        self.verification("a", lock, "sha-a", 1)
        self.msg("a", "retry", dict(from_model="sonnet", to_model="opus"))
        lock = self.lock("a", "sha-a2")
        self.verification("a", lock, "sha-a2")
        lock = self.lock("b", "sha-b")
        self.verification("b", lock, "sha-b")
        t = self.total()
        self.assertEqual((t["verify_events"], t["first_pass_verify_n"], t["first_pass_verify_rate"]),
                         (3, 2, 0.5))
        self.assertEqual(t["escalations_post_upgrade_verified"], 1)
        by_type = self.report()["orchs"][0]["by_task_type"]
        self.assertEqual(by_type["a"]["first_pass_verify_rate"], 0)
        self.assertEqual(by_type["b"]["first_pass_verify_rate"], 1)

    def test_locks_relocks_stale_and_author_do_not_count_as_results(self):
        self.task("a")
        lock = self.lock("a", "sha")
        self.lock("a", "sha2")
        for state in ("locked", "relock", "stale"):
            self.verification("a", lock, "sha", state=state)
        self.verification("a", lock, "sha", verifier="a")
        self.verification("a", lock, "sha", role="author")
        self.verification("a", lock, "sha", author="other")
        self.msg("a", "report", dict(exit=0, passed=True))
        t = self.total()
        for key in ("verify_events", "first_pass_verify_n", "first_pass_verify_rate"):
            self.assertIsNone(t[key])
        self.verification("a", lock, "sha", code=0, dirty=True, passed=False)
        self.assertEqual(self.total()["first_pass_verify_rate"], 0)

    def test_unknown_first_result_is_not_replaced_by_later_pass(self):
        for kind in ("verification", "verify"):
            with self.subTest(kind=kind):
                tid = kind
                self.task(tid)
                lock = self.lock(tid, "sha")
                if kind == "verification":
                    self.verification(tid, lock, "sha", code=None, passed=None)
                    self.verification(tid, lock, "sha")
                else:
                    self.msg(tid, kind, dict(exit_code=True))
                    self.msg(tid, kind, dict(exit_code=0))
                self.assertIsNone(self.total()["first_pass_verify_n"])
                self.assertIsNone(self.total()["first_pass_verify_rate"])

    def test_post_upgrade_requires_changed_sha_matching_lock_and_retry_interval(self):
        for case in ("pass", "fail", "missing_before", "missing_after", "same_sha", "wrong_sha",
                     "wrong_lock", "wrong_command", "later_wrong_sha", "before_retry", "after_next_retry", "relock", "legacy", "unknown"):
            with self.subTest(case=case):
                tid = case
                self.task(tid, orch=case)
                old = self.lock(tid, "old") if case != "missing_before" else None
                if case == "before_retry":
                    self.verification(tid, old, "old")
                self.msg(tid, "retry", dict(from_model="sonnet", to_model="opus"))
                sha = "old" if case == "same_sha" else "new"
                lock = self.lock(tid, sha) if case != "missing_after" else 12345
                if case == "after_next_retry":
                    self.msg(tid, "retry", dict(from_model="opus", to_model="sonnet"))
                if case == "legacy":
                    self.msg(tid, "verify", dict(exit_code=0))
                elif case != "before_retry":
                    self.verification(tid, lock + 1 if case == "wrong_lock" else lock,
                                      "other" if case == "wrong_sha" else sha,
                                      1 if case == "fail" else 0,
                                      **(dict(passed=None, exit=None) if case == "unknown" else
                                         dict(command="different") if case == "wrong_command" else {}))
                if case == "later_wrong_sha":
                    self.verification(tid, lock, "other", 1)
                if case == "relock":
                    self.lock(tid, "newer")
                value = self.total(case)["escalations_post_upgrade_verified"]
                if case in ("pass", "fail"):
                    self.assertEqual(value, int(case == "pass"))
                else:
                    self.assertIsNone(value)

    def test_multiple_upgrades_are_attributed_to_their_own_retry_intervals(self):
        self.task("a")
        self.lock("a", "old")
        for sha, code in (("first", 1), ("second", 0)):
            self.msg("a", "retry", dict(from_model="sonnet", to_model="opus"))
            lock = self.lock("a", sha)
            self.verification("a", lock, sha, code)
        t = self.total()
        self.assertEqual(t["escalations"], 2)
        self.assertEqual(t["escalations_post_upgrade_verified"], 1)
        self.assertEqual(t["first_pass_verify_rate"], 0)

    def test_post_upgrade_aggregate_remains_unknown_if_one_upgrade_has_no_evidence(self):
        self.task("a")
        self.task("b")
        self.lock("a", "old")
        for tid in ("a", "b"):
            self.msg(tid, "retry", dict(from_model="sol", to_model="opus"))
            lock = self.lock(tid, "new")
            self.verification(tid, lock, "new")
        self.assertIsNone(self.total()["escalations_post_upgrade_verified"])

    def test_proxy_no_rework_counts_originals_that_were_reworked(self):
        for tid in ("a", "b"):
            self.task(tid)
            self.close(tid, "merged")
        self.task("r", rework_of="a", found_by="orch")
        self.assertEqual(self.total()["proxy_no_rework_rate"], 0.5)
        self.assertIn("not model accuracy", self.total()["proxy_note"])

    # -- worker tokens and cost -----------------------------------------------------------------

    def test_worker_tokens_cost_exact_decimal_and_coverage(self):
        self.task("a")
        self.task("b")
        self.task("c")  # still open: not counted as missing
        self.close("a", "merged", dict(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=500_000,
                                       cache_creation_input_tokens=0))
        self.close("b", "merged")  # closed but usage never written: missing
        t = self.total()
        self.assertEqual(t["worker_tokens"]["coverage"], "1/2")
        self.assertEqual(t["worker_tokens"]["open_tasks"], 1)
        # 1M*2 + 0.1M*10 + 0.5M*0.2 = 2 + 1 + 0.1 USD per 1M prices
        # one of two closed tasks has no usage: 3.1 is a lower bound, never a complete cost
        self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal("3.1"))
        self.assertFalse(t["cost"]["complete"])
        self.assertEqual(t["cost"]["missing_usage_tasks"], 1)
        self.assertFalse(t["cost_per_completed_complete"])
        self.assertIn("PARTIAL", stats.format_table(self.report()))
        self.assertIn("not a bill", t["cost"]["note"])

    def test_cache_creation_is_separate_and_unpriced_marks_incomplete(self):
        self.task("a")
        self.close("a", "merged", dict(input_tokens=10, output_tokens=20, cache_creation_input_tokens=7,
                                       cache_read_input_tokens=30))
        t = self.total()
        buckets = t["worker_tokens"]["by_model"]["claude-sonnet-5-5"]
        self.assertEqual((buckets["input"], buckets["cache_creation"], buckets["cache_read"], buckets["output"]),
                         (10, 7, 30, 20))
        # cache write is priced at the published 2.5, but its TTL/tier is unknown: counted, flagged partial
        self.assertFalse(t["cost"]["complete"])
        self.assertEqual((t["cost"]["unpriced_tokens"], t["cost"]["unverified_tokens"]), ({}, {"cache_creation": 7}))
        self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal("0.0002435"))  # (10*2 + 7*2.5 + 30*0.2 + 20*10) / 1M

    def test_unknown_model_has_null_cost_and_stale_usage_after_retry_is_missing(self):
        self.task("a", model="mystery-1")
        self.close("a", "merged", dict(input_tokens=5, output_tokens=5), model="mystery-1")
        self.task("b")
        self.close("b", "merged", dict(input_tokens=5, output_tokens=5))
        self.msg("b", "retry")  # usage was written before this retry: it cannot be the final number
        t = self.total()
        self.assertEqual(t["worker_tokens"]["usage_stale_after_retry"], ["b"])
        self.assertEqual(t["worker_tokens"]["coverage"], "1/2")
        self.assertIsNone(t["cost"]["usd_estimate"])
        self.assertEqual(t["cost"]["unknown_models"], ["mystery-1"])

    # -- Claude Orch transcript ------------------------------------------------------------------

    def write_claude(self, session, lines):
        (self.claude / "-p" / f"{session}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

    def line(self, mid, ts, out, inp=1, cc=0, cr=0):
        return {"type": "assistant", "timestamp": ts, "uuid": mid + ts, "message": {"id": mid, "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr}}}

    def test_claude_orch_tokens_window_dedup_offsets_and_cost(self):
        store.register_orch(self.con, "o1", "claude", model="claude-opus-5-5", session_id="s1")
        self.write_claude("s1", [
            self.line("m0", iso(T0 - 100), 999),                          # before the window
            self.line("m1", iso(T0 + 5), 3, inp=1_000_000, cr=100_000),    # streamed twice, last usage wins
            self.line("m1", iso(T0 + 6), 10, inp=1_000_000, cr=100_000, cc=4),
            self.line("m2", iso(T0 + 7 + 8 * 3600).replace("Z", "+08:00"), 5),  # +08:00 offset: UTC T0+7
            {"type": "assistant", "message": {"id": "m3", "usage": {"input_tokens": 1, "output_tokens": 77}}},  # no timestamp
            {"type": "user"},
        ])
        self.task("a")
        (row,) = self.report()["orchs"]
        t = row["orch_tokens"]
        self.assertEqual(t["tokens"], dict(input=1_000_001, cache_creation=4, cache_read=100_000, output=15))
        self.assertEqual(t["total"], 1_100_020)  # sum of disjoint buckets, nothing counted twice
        self.assertEqual(t["events"], 2)
        self.assertIn("without a timestamp", t["partial"][0])
        # opus: 1,000,001*4 + 100,000*0.2 + 4*5 + 15*20 per 1M
        self.assertEqual(Decimal(t["cost_usd_estimate"]), Decimal("4.020324"))  # 4.000004 + 0.02 + 0.00002 + 0.0003
        self.assertFalse(t["cost_complete"])

    def test_claude_session_without_transcript_is_missing_not_zero(self):
        store.register_orch(self.con, "o1", "claude", model="claude-opus-5-5", session_id="nope")
        self.task("a")
        t = self.report()["orchs"][0]["orch_tokens"]
        self.assertEqual((t["source"], t["tokens"]), ("missing", None))

    # -- Codex Orch rollout ----------------------------------------------------------------------

    def write_codex(self, thread, events, start=None):
        lines = [{"timestamp": iso(start if start is not None else events[0][0]), "type": "session_meta", "payload": {}}]
        for ts, i, c, o in events:
            lines.append({"timestamp": iso(ts), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": i, "cached_input_tokens": c, "cache_write_input_tokens": 0,
                                      "output_tokens": o, "total_tokens": i + o}}}})
        (self.codex / f"rollout-2026-x-{thread}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

    def codex_tokens(self, thread="th1", since=T0):
        self.task("a", orch=thread)
        (row,) = [r for r in self.report()["orchs"] if r["orch_id"] == thread] if since == T0 else [
            r for r in stats.stats(self.con, since, self.claude, self.codex)["orchs"] if r["orch_id"] == thread]
        return row["orch_tokens"]

    def test_codex_baseline_before_window_counts_first_increment(self):
        self.write_codex("th1", [(T0 - 50, 100, 40, 10), (T0 + 10, 160, 60, 25), (T0 + 20, 200, 80, 30)])
        t = self.codex_tokens()
        # increments after the baseline (100/40/10): input +100, cached +40, output +20
        self.assertEqual(t["tokens"], dict(input=60, cache_creation=0, cache_read=40, output=20))
        self.assertEqual(t["partial"], [])
        self.assertIsNone(t["cost_usd_estimate"])  # Astra's model is not in the DB: unknown, no estimate

    def test_codex_session_started_in_window_counts_from_zero(self):
        self.write_codex("th1", [(T0 + 10, 100, 40, 10), (T0 + 20, 100, 40, 10)])  # repeated event adds nothing
        t = self.codex_tokens()
        self.assertEqual(t["tokens"], dict(input=60, cache_creation=0, cache_read=40, output=10))

    def test_codex_no_baseline_is_partial_and_skips_unknown_first_delta(self):
        self.write_codex("th1", [(T0 + 10, 500, 0, 50), (T0 + 20, 560, 0, 60)], start=T0 - 1000)
        t = self.codex_tokens()
        self.assertEqual(t["tokens"], dict(input=60, cache_creation=0, cache_read=0, output=10))
        self.assertTrue(t["partial"] and "no counter before the window" in t["partial"][0])

    def test_codex_counter_reset_and_multiple_rollout_files(self):
        self.write_codex("th1", [(T0 + 1, 100, 0, 10), (T0 + 2, 300, 0, 30), (T0 + 3, 20, 0, 5), (T0 + 4, 50, 0, 9)])
        (self.codex / "rollout-2026-y-th1.jsonl").write_text(json.dumps(
            {"timestamp": iso(T0 + 1), "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 7, "cached_input_tokens": 0, "output_tokens": 1}}}}))
        t = self.codex_tokens()
        # file 1: 100 + 200 + (reset -> 20) + 30 = 350 input, 10+20+5+4 = 39 output; file 2: 7 / 1
        self.assertEqual((t["tokens"]["input"], t["tokens"]["output"]), (357, 40))
        self.assertEqual(t["counter_resets"], 1)

    # -- output ----------------------------------------------------------------------------------

    def test_timestamps_and_since_parsing(self):
        self.assertEqual(stats.parse_ts("2026-10-01T08:00:00+08:00"), stats.parse_ts("2026-10-01T00:00:00Z"))
        self.assertEqual(stats.parse_ts("2026-10-01T00:00:00"), stats.parse_ts("2026-10-01T00:00:00Z"))
        self.assertIsNone(stats.parse_ts("garbage"))
        self.assertEqual(stats.parse_since("2026-10-01T08:00:00+08:00"), stats.parse_ts("2026-10-01T00:00:00Z"))

    def test_table_prints_unknown_and_label(self):
        self.task("a", task_type="code")
        text = stats.format_table(self.report())
        self.assertIn("unknown", text)
        self.assertIn("estimates only", text)
        self.assertIn("Astra o1", text)

    # -- usage validity: missing / malformed / counterless evidence is not coverage ---------------

    def test_malformed_or_counterless_worker_usage_does_not_raise_coverage_or_complete_cost(self):
        for tid in ("a", "b", "c"):
            self.task(tid)
        self.close("a", "merged", dict(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=0,
                                       cache_creation_input_tokens=0))
        self.close("b", "merged")
        self.msg("b", "usage", {"error": "no usage available"})          # no counters
        self.close("c", "merged")
        self.msg("c", "usage", "{not json")                              # unparseable body
        t = self.total()
        self.assertEqual(t["worker_tokens"]["coverage"], "1/3")
        self.assertEqual(sorted(t["worker_tokens"]["usage_malformed"]), ["b", "c"])
        self.assertFalse(t["cost"]["complete"])
        self.assertFalse(t["cost_per_completed_complete"])

    def test_negative_or_non_integer_counters_are_malformed(self):
        for i, bad in enumerate((-1, "7", 1.5, True)):
            self.task(f"t{i}")
            self.close(f"t{i}", "merged", dict(input_tokens=bad, output_tokens=1))
        self.assertEqual(self.total()["worker_tokens"]["coverage"], "0/4")
        self.assertIsNone(self.total()["cost"]["usd_estimate"] and None)  # no covered model: nothing priced
        self.assertIsNone(self.total()["cost"]["usd_estimate"])

    def test_missing_cache_bucket_is_not_a_zero_and_keeps_cost_partial(self):
        self.task("a")
        self.close("a", "merged", dict(input_tokens=10, output_tokens=20))  # no cache_* keys at all
        t = self.total()
        self.assertEqual(t["worker_tokens"]["coverage"], "1/1")
        self.assertFalse(t["cost"]["complete"])
        self.assertEqual(t["worker_tokens"]["by_model"]["claude-sonnet-5-5"]["bucket_unknown_tasks"], 1)

    def test_fully_covered_cache_free_usage_is_complete(self):
        self.task("a")
        self.close("a", "merged", dict(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0,
                                       cache_read_input_tokens=0))
        self.assertTrue(self.total()["cost"]["complete"])

    def test_claude_transcript_without_usable_usage_is_unknown_not_zero(self):
        store.register_orch(self.con, "o1", "claude", model="claude-opus-5-5", session_id="s1")
        (self.claude / "-p" / "s1.jsonl").write_text('{malformed\n' + json.dumps({"type": "user"}) + "\n" + json.dumps(
            {"type": "assistant", "timestamp": iso(T0 + 1), "message": {"id": "x", "usage": {"error": "none"}}}))
        self.task("a")
        t = self.report()["orchs"][0]["orch_tokens"]
        self.assertEqual((t["source"], t["tokens"]), ("unusable", None))
        self.assertIsNone(t.get("cost_usd_estimate"))  # priced model, but no evidence: no complete zero cost
        self.assertIn("unknown", stats.format_table(self.report()))

    def test_codex_rollout_without_counters_is_unknown_not_zero(self):
        (self.codex / "rollout-2026-x-th1.jsonl").write_text(json.dumps({"timestamp": iso(T0), "type": "session_meta"}))
        self.task("a", orch="th1")
        (row,) = [r for r in self.report()["orchs"] if r["orch_id"] == "th1"]
        self.assertEqual((row["orch_tokens"]["source"], row["orch_tokens"]["tokens"]), ("unusable", None))

    # -- Claude: the final usage of a message wins by timestamp, not by file order ---------------

    def claude_two_files(self, a_name, b_name):
        (self.claude / "-q").mkdir(exist_ok=True)
        (self.claude / "-p" / f"{a_name}.jsonl").write_text("\n".join(json.dumps(x) for x in (
            self.line("m", iso(T0 + 1), 2), self.line("m", iso(T0 + 2), 7))))
        (self.claude / "-q" / f"{b_name}.jsonl").write_text(json.dumps(self.line("m", iso(T0 + 1), 2)))

    def test_claude_repeated_message_keeps_final_usage_whichever_file_sorts_last(self):
        for older_dir_first in (True, False):
            with self.subTest(older_dir_first=older_dir_first):
                for f in list(self.claude.glob("*/*.jsonl")):
                    f.unlink()
                if older_dir_first:  # the stale copy lives in the lexicographically LAST directory
                    self.claude_two_files("s1", "s1")
                else:                # swap: the stale copy now sorts first
                    (self.claude / "-q").mkdir(exist_ok=True)
                    (self.claude / "-q" / "s1.jsonl").write_text("\n".join(json.dumps(x) for x in (
                        self.line("m", iso(T0 + 1), 2), self.line("m", iso(T0 + 2), 7))))
                    (self.claude / "-p" / "s1.jsonl").write_text(json.dumps(self.line("m", iso(T0 + 1), 2)))
                u = stats.claude_transcript_usage(self.claude, "s1", T0)
                self.assertEqual((u["output"], u["messages"]), (7, 1))

    def test_claude_same_timestamp_prefers_larger_output_and_dated_beats_undated(self):
        self.write_claude("s1", [self.line("m", iso(T0 + 1), 9), self.line("m", iso(T0 + 1), 4),
                                 {"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 1, "output_tokens": 50}}}])
        u = stats.claude_transcript_usage(self.claude, "s1", T0)
        self.assertEqual((u["output"], u["messages"]), (9, 1))

    # -- Codex: ordering by timestamp, delayed low counter is not a reset -------------------------

    def codex_events(self, thread, events):
        lines = [{"timestamp": iso(T0 - 1000), "type": "session_meta", "payload": {}}]
        for ts, i, o, extra in events:
            usage = {"input_tokens": i, "cached_input_tokens": 0, "output_tokens": o, "total_tokens": i + o, **extra}
            lines.append({"timestamp": iso(ts), "type": "event_msg",
                          "payload": {"type": "token_count", "info": {"total_token_usage": usage}}})
        (self.codex / f"rollout-2026-x-{thread}.jsonl").write_text("\n".join(json.dumps(x) for x in lines))

    def test_codex_late_written_lower_counter_is_not_a_reset(self):
        # physical order: t+20(200), t+10(150) written late, t+30(220); baseline t-10 (100)
        self.codex_events("th1", [(T0 - 10, 100, 10, {}), (T0 + 20, 200, 20, {}), (T0 + 10, 150, 15, {}),
                                  (T0 + 30, 220, 22, {})])
        u = stats.codex_rollout_usage(self.codex, "th1", T0)
        self.assertEqual((u["input"], u["output"], u["resets"], u["partial"]), (120, 12, 0, []))

    def test_codex_true_reset_in_time_order_still_counts_from_zero(self):
        self.codex_events("th1", [(T0 - 10, 100, 10, {}), (T0 + 1, 300, 30, {}), (T0 + 2, 20, 5, {})])
        u = stats.codex_rollout_usage(self.codex, "th1", T0)
        self.assertEqual((u["input"], u["output"], u["resets"]), (220, 25, 1))

    def test_codex_malformed_and_undated_counters_are_ignored_and_flagged(self):
        self.codex_events("th1", [(T0 - 10, 100, 10, {}), (T0 + 1, 150, 20, {})])
        path = next(self.codex.glob("rollout-*-th1.jsonl"))
        with path.open("a") as f:
            f.write("\n" + json.dumps({"timestamp": iso(T0 + 2), "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"input_tokens": "x"}}}}))
            f.write("\n" + json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": 999, "output_tokens": 999}}}}))
        u = stats.codex_rollout_usage(self.codex, "th1", T0)
        self.assertEqual((u["input"], u["output"]), (50, 10))
        self.assertEqual(len(u["partial"]), 2)

    def test_codex_cache_write_is_kept_raw_not_assumed_disjoint(self):
        self.codex_events("th1", [(T0 - 10, 0, 0, {}), (T0 + 1, 100, 10, {"cached_input_tokens": 30, "cache_write_input_tokens": 20})])
        u = stats.codex_rollout_usage(self.codex, "th1", T0)
        self.assertEqual(u["cache_write_raw"], 20)
        self.assertEqual(u["cache_creation"], 0)                     # not folded into any bucket
        self.assertEqual(sum(u[k] for k in stats.FIELDS), 110)       # == the source's own total_tokens
        self.assertTrue(any("not established" in n for n in u["partial"]))
        self.task("a", orch="th1")
        (row,) = [r for r in self.report()["orchs"] if r["orch_id"] == "th1"]
        self.assertEqual(row["orch_tokens"]["cache_write_unattributed"], 20)

    def test_codex_total_that_contradicts_input_plus_output_is_flagged(self):
        self.codex_events("th1", [(T0 - 10, 0, 0, {}), (T0 + 1, 100, 10, {"total_tokens": 130})])
        u = stats.codex_rollout_usage(self.codex, "th1", T0)
        self.assertTrue(any("total_tokens" in n for n in u["partial"]))

    # -- retries / escalation ---------------------------------------------------------------------

    def retry_total(self, *bodies):
        self.task("a")
        self.close("a", "merged")
        for b in bodies:
            self.msg("a", "retry", b)
        return self.total()

    def test_escalation_is_a_model_tier_upgrade_not_a_retry(self):
        cases = [({"from": "claude-sonnet-5-5", "to": "claude-sonnet-5-5"}, 0),   # same model
                 ({"from": "claude-opus-5-5", "to": "claude-sonnet-5-5"}, 0),     # downgrade
                 ({"from": "claude-sonnet-5-5", "to": "gpt-6.1-sol"}, 0),         # same routing tier (M)
                 ({"from_model": "claude-sonnet-5-5", "to_model": "claude-opus-5-5"}, 1)]  # tolerated alt names
        for body, want in cases:
            with self.subTest(body=body):
                for tbl in ("messages", "tasks"):
                    self.con.execute(f"DELETE FROM {tbl}")
                t = self.retry_total(body)
                self.assertEqual((t["retries"], t["escalations"], t["escalations_unresolved"]), (1, want, 0))

    def test_escalation_unknown_fields_tolerated_and_unverified_result_is_unknown(self):
        t = self.retry_total({"from": "sonnet", "to": "opus", "future_field": {"x": [1]}}, {"reason": "oops"}, {})
        self.assertEqual((t["escalations"], t["escalations_unresolved"], t["escalations_complete"]), (1, 2, False))
        self.assertIsNone(t["escalations_post_upgrade_verified"])    # a done outcome is not a verified pass
        self.assertEqual(t["escalated_tasks_outcome_completed"], 1)
        self.assertIn("not a verified pass", t["escalation_note"])
        self.assertIn("1+ (2 unresolved)", stats.format_table(self.report()))
        self.assertIn("unknown", stats.format_table(self.report()))

    def test_another_orchs_retry_does_not_turn_this_orchs_unknown_into_zero(self):
        self.task("mine")
        self.task("theirs", orch="o2")
        self.msg("theirs", "retry", {"from": "sonnet", "to": "opus"})
        mine = self.total("o1")
        self.assertIsNone(mine["retries"])
        self.assertIsNone(mine["escalations"])
        self.assertEqual(self.total("o2")["escalations"], 1)

    # -- multi-session worker usage (retry writes one tagged row per replaced session; close one untagged) -------

    def sessions_task(self, final_session="new", **retry_extra):
        self.task("a", model="claude-opus-5-5", session_id=final_session)
        self.msg("a", "usage", dict(model="claude-sonnet-5-5", session_id="old", job_id="j0", source="retry",
                                    **self.counters(1_000_000)))
        self.msg("a", "retry", dict(from_model="claude-sonnet-5-5", to_model="claude-opus-5-5", reason="r",
                                    old_model="claude-sonnet-5-5", old_session_id="old", old_job_id="j0",
                                    new_session_id="new", new_job_id="j1", **retry_extra))

    def counters(self, n, **extra):
        return dict(input_tokens=n, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0, **extra)

    def test_retry_sessions_all_cost_once_and_complete(self):
        for final in ("new", None):  # final session from tasks.session_id, or from the retry's new_session_id
            with self.subTest(final=final):
                for tbl in ("messages", "tasks"):
                    self.con.execute(f"DELETE FROM {tbl}")
                self.sessions_task(final)
                self.close("a", "merged", self.counters(500_000), model="claude-opus-5-5")  # untagged final
                t = self.total()
                self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal(4))  # Sonnet 1M x $2 + Opus 500k x $4
                self.assertTrue(t["cost"]["complete"])
                self.assertEqual((t["worker_tokens"]["coverage"], t["cost"]["partial_usage_tasks"]), ("1/1", 0))
                by = t["worker_tokens"]["by_model"]
                self.assertEqual((by["claude-sonnet-5-5"]["input"], by["claude-opus-5-5"]["input"]),
                                 (1_000_000, 500_000))

    def test_duplicate_tagged_session_counts_once(self):
        self.sessions_task()
        self.msg("a", "usage", dict(model="claude-sonnet-5-5", session_id="old", source="retry",
                                    **self.counters(1_000_000)))
        self.close("a", "merged", self.counters(500_000), model="claude-opus-5-5")
        t = self.total()
        self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal(4))
        self.assertEqual(t["worker_tokens"]["by_model"]["claude-sonnet-5-5"]["sessions"], 1)

    def test_untagged_final_that_is_a_tagged_session_is_not_double_counted(self):
        # retry recorded s0, then failed to spawn: the task still points at s0 and close reads s0 again
        self.task("a", session_id="s0")
        self.msg("a", "usage", dict(model="claude-sonnet-5-5", session_id="s0", source="retry",
                                    **self.counters(1_000_000)))
        self.msg("a", "retry_failed", dict(stage="spawn", error="x", old_session_id="s0"))
        self.close("a", "merged", self.counters(1_000_000))
        t = self.total()
        self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal(2))
        self.assertTrue(t["cost"]["complete"])

    def test_untagged_final_of_unknown_session_next_to_tagged_rows_is_partial(self):
        self.task("a")  # no tasks.session_id, and the newest attempt failed: the final session is unknown
        self.msg("a", "usage", dict(model="claude-sonnet-5-5", session_id="s0", source="retry",
                                    **self.counters(1_000_000)))
        self.msg("a", "retry_failed", dict(stage="spawn", error="x", old_session_id="s0"))
        self.close("a", "merged", self.counters(1_000_000))
        t = self.total()
        self.assertFalse(t["cost"]["complete"])
        self.assertEqual((t["cost"]["partial_usage_tasks"], t["cost"]["missing_usage_tasks"]), (1, 0))
        self.assertEqual(t["worker_tokens"]["usage_partial_sessions"], ["a"])
        self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal(2))  # lower bound: the overlap is unknown
        self.assertEqual(t["worker_tokens"]["coverage"], "0/1")
        self.assertIn("PARTIAL", stats.format_table(self.report()))

    def test_retry_without_usage_of_replaced_session_is_partial_not_complete(self):
        cases = {"legacy retry names no old session": dict(old_session_id=None),
                 "replaced session has no usage row": dict(old_session_id="gone")}
        for name, extra in cases.items():
            with self.subTest(name):
                for tbl in ("messages", "tasks"):
                    self.con.execute(f"DELETE FROM {tbl}")
                self.task("a", model="claude-opus-5-5", session_id="new")
                self.msg("a", "retry", dict(from_model="claude-sonnet-5-5", to_model="claude-opus-5-5", **extra))
                self.close("a", "merged", self.counters(500_000), model="claude-opus-5-5")
                t = self.total()
                self.assertFalse(t["cost"]["complete"])
                self.assertEqual(t["worker_tokens"]["usage_partial_sessions"], ["a"])
                self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal(2))  # only the final session is known

    def test_malformed_tagged_session_row_keeps_task_partial(self):
        self.sessions_task()
        self.con.execute("DELETE FROM messages WHERE kind='usage'")
        self.msg("a", "usage", dict(model="claude-sonnet-5-5", session_id="old", input_tokens="x", output_tokens=0))
        self.close("a", "merged", self.counters(500_000), model="claude-opus-5-5")
        t = self.total()
        self.assertFalse(t["cost"]["complete"])
        self.assertEqual(t["worker_tokens"]["usage_partial_sessions"], ["a"])
        self.assertNotIn("claude-sonnet-5-5", t["worker_tokens"]["by_model"])

    def test_stats_reads_without_changing_the_db(self):
        self.sessions_task()
        self.close("a", "merged", self.counters(500_000), model="claude-opus-5-5")
        self.con.commit()
        before = self.con.total_changes
        rows = [tuple(r) for r in self.con.execute("SELECT * FROM messages ORDER BY id")]
        self.report()
        self.assertEqual(self.con.total_changes, before)
        self.assertEqual([tuple(r) for r in self.con.execute("SELECT * FROM messages ORDER BY id")], rows)

    # -- escalation by project routing tier ----------------------------------------------------------

    def test_escalation_follows_project_routing_tiers(self):
        cases = [({"from_model": "gpt-6.1-sol", "to_model": "claude-opus-5-5"}, (1, 0, True)),   # M -> H
                 ({"old_model": "gpt-6.1-sol", "to_model": "claude-opus-5-5"}, (1, 0, True)),    # PR30 field
                 ({"from": "sol", "to": "opus"}, (1, 0, True)),
                 ({"from": "claude-opus-5-5", "to": "gpt-6.1-sol"}, (0, 0, True)),               # H -> M
                 ({"from": "claude-sonnet-5-5", "to": "gpt-6.1-sol"}, (0, 0, True)),             # same tier
                 ({"from": "future-model-A", "to": "future-model-B"}, (0, 1, False)),            # no tier
                 ({"from": "claude-haiku-4-5", "to": "claude-opus-5-5"}, (0, 1, False)),         # not routed
                 ({"from": "claude-sonnet-5-5", "to": 7}, (0, 1, False))]                        # bad schema
        for body, want in cases:
            with self.subTest(body=body):
                for tbl in ("messages", "tasks"):
                    self.con.execute(f"DELETE FROM {tbl}")
                t = self.retry_total(body)
                self.assertEqual((t["escalations"], t["escalations_unresolved"], t["escalations_complete"]), want)
        self.assertIn("not a quality ranking", t["escalation_note"])

    # -- Codex cache counters ---------------------------------------------------------------------

    def test_codex_missing_or_malformed_cache_counter_is_unknown_not_zero(self):
        drop = object()
        cases = {"missing": drop, "malformed": "malformed", "null": None, "negative": -1, "above input": 150}
        for name, cached in cases.items():
            with self.subTest(name):
                extra = {} if cached is drop else {"cached_input_tokens": cached}
                self.codex_events("th1", [(T0 - 10, 0, 0, {}), (T0 + 1, 100, 10, extra)])
                if cached is drop:
                    path = next(self.codex.glob("rollout-*-th1.jsonl"))
                    lines = [json.loads(x) for x in path.read_text().splitlines()]
                    del lines[-1]["payload"]["info"]["total_token_usage"]["cached_input_tokens"]
                    path.write_text("\n".join(json.dumps(x) for x in lines))
                u = stats.codex_rollout_usage(self.codex, "th1", T0)
                self.assertEqual((u["input"], u["cache_read"], u["input_cache_split_unknown"], u["output"]),
                                 (0, 0, 100, 10))
                self.assertTrue(any("cached_input_tokens" in n for n in u["partial"]))

    def test_codex_cache_split_unknown_is_unpriced_and_in_total(self):
        self.codex_events("th1", [(T0 - 10, 0, 0, {}), (T0 + 1, 100, 10, {"cached_input_tokens": 40}),
                                  (T0 + 2, 160, 20, {"cached_input_tokens": "?"}),
                                  (T0 + 3, 200, 30, {"cached_input_tokens": 50})])
        u = stats.codex_rollout_usage(self.codex, "th1", T0)
        # only 0->100 has both ends known; 100->160 and 160->200 touch the unknown sample
        self.assertEqual((u["input"], u["cache_read"], u["input_cache_split_unknown"]), (60, 40, 100))
        t = stats.orch_tokens({"kind": "codex", "model": None, "session_id": None}, "th1", T0,
                              self.claude, self.codex, stats.PRICES)
        self.assertEqual(t["total"], 60 + 40 + 30 + 100)
        self.assertEqual(t["input_cache_split_unknown"], 100)
        self.assertIsNone(t["cost_usd_estimate"])  # Astra's model is unknown; nothing priced either way

    def test_codex_invalid_cache_write_counter_is_flagged(self):
        self.codex_events("th1", [(T0 - 10, 0, 0, {}), (T0 + 1, 100, 10, {"cache_write_input_tokens": "x"})])
        u = stats.codex_rollout_usage(self.codex, "th1", T0)
        self.assertEqual((u["input"], u["cache_write_raw"]), (100, 0))
        self.assertTrue(any("cache_write_input_tokens" in n for n in u["partial"]))

    # -- table / prices ---------------------------------------------------------------------------

    def test_table_shows_partial_cost_and_required_columns(self):
        store.register_orch(self.con, "o1", "claude", model="claude-opus-5-5", session_id="s1")
        self.write_claude("s1", [self.line("m1", iso(T0 + 1), 5, cc=100)])
        self.task("a")
        self.close("a", "merged", dict(input_tokens=1, output_tokens=1, cache_creation_input_tokens=3,
                                       cache_read_input_tokens=0))
        text = stats.format_table(self.report())
        for col in ("rework_rate", "retries_per_task", "followups_per_task", "escalations",
                    "escalations_post_upgrade_verified", "cost/completed"):
            self.assertIn(col, text)
        worker_row, orch_row = [ln for ln in text.splitlines() if "Claude Orch" in ln or "orch tokens" in ln][:2]
        self.assertIn("PARTIAL", worker_row)   # worker cost has cache writes of unverified tier
        self.assertIn("PARTIAL", orch_row)     # orch cost too

    def test_price_table_records_official_sources_and_cache_write_rates(self):
        prices = stats.PRICES
        self.assertEqual(prices["version"], "2026-10-02")
        want = {"claude-sonnet-5-5": ("2", "2.5"), "claude-opus-5-5": ("4", "5"), "gpt-6.1-sol": ("2", "2.5")}
        for model, (inp, write) in want.items():
            p = prices["models"][model]
            self.assertEqual((p["input"], p["cache_write"]), (inp, write))
            self.assertTrue(p["source"].startswith("https://"))
        self.assertTrue(prices["cache_write_priced"])
        self.assertIn("tier", prices["cache_write_note"])
        rep = self.report()
        self.assertTrue(rep["prices"]["cache_write_priced"])

    # -- SQLite snapshot: never touches the source, sees the WAL, fails closed -------------------

    def wal_db(self, name="wal.db"):
        path = self.root / name
        w = sqlite3.connect(path)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY, orch_thread TEXT, status TEXT, created_at REAL, "
                        "model TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, "
                        "body TEXT); CREATE TABLE orchs(id TEXT PRIMARY KEY, kind TEXT, model TEXT, session_id TEXT);")
        w.commit()
        self.addCleanup(w.close)
        return path, w

    def tree(self, path):
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in path.parent.glob(path.name + "*")}

    def test_snapshot_sees_uncheckpointed_wal_task_and_leaves_source_untouched(self):
        path, w = self.wal_db()
        w.execute("INSERT INTO tasks VALUES('old','o1','done',2000000,NULL)")
        w.commit()
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.execute("INSERT INTO tasks VALUES('new-in-wal','o1','done',2000001,NULL)")  # only in the WAL
        w.commit()
        before = self.tree(path)
        self.assertIn("wal.db-wal", before)
        with stats.open_snapshot(path) as snap:
            rep = stats.stats(snap.con, None, self.claude, self.codex)
        self.assertEqual(rep["orchs"][0]["total"]["tasks"], 2)  # the WAL-only task is not omitted
        self.assertEqual(self.tree(path), before)               # no new sidecar, no size/mtime change
        self.assertEqual(sorted(p.name for p in self.root.glob("wal.db*")), sorted(before))

    def test_snapshot_after_checkpoint_and_without_any_sidecar(self):
        path, w = self.wal_db()
        w.execute("INSERT INTO tasks VALUES('a','o1','done',2000000,NULL)")
        w.commit()
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.close()
        for sidecar in self.root.glob("wal.db-*"):
            sidecar.unlink()                                     # a cleanly closed DB leaves only the main file
        before = self.tree(path)
        self.assertEqual(list(before), ["wal.db"])
        with stats.open_snapshot(path) as snap:
            self.assertEqual(stats.stats(snap.con, None, self.claude, self.codex)["orchs"][0]["total"]["tasks"], 1)
        self.assertEqual(self.tree(path), before)

    def test_snapshot_is_private_temp_copy_removed_on_close_and_old_schema_untouched(self):
        path, w = self.wal_db()
        w.execute("INSERT INTO tasks VALUES('a','o1','done',2000000,NULL)")
        w.commit()
        snap = stats.open_snapshot(path)
        d = Path(snap._dir)
        self.assertNotEqual(d, self.root)
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in d.iterdir()))
        stats.stats(snap.con, None, self.claude, self.codex)    # old schema: no task_type etc., no migration
        snap.close()
        self.assertFalse(d.exists())
        self.assertNotIn("task_type", {r[1] for r in w.execute("PRAGMA table_info(tasks)")})

    def test_snapshot_retries_when_source_changes_mid_copy_then_succeeds(self):
        path, w = self.wal_db()
        w.execute("INSERT INTO tasks VALUES('a','o1','done',2000000,NULL)")
        w.commit()
        real, calls = stats._copy, []

        def racing(src, dst):
            real(src, dst)
            if not calls:
                calls.append(1)
                w.execute("INSERT INTO tasks VALUES('late','o1','done',2000002,NULL)")
                w.commit()                                       # WAL grows while we were copying
        with mock.patch.object(stats, "_copy", racing):
            with stats.open_snapshot(path) as snap:
                self.assertEqual(snap.con.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)

    def test_snapshot_fails_closed_when_source_never_settles(self):
        path, w = self.wal_db()
        real, n = stats._copy, [0]

        def racing(src, dst):
            real(src, dst)
            n[0] += 1
            w.execute("INSERT INTO tasks VALUES(?, 'o1','done',2000000,NULL)", (f"t{n[0]}",))
            w.commit()
        with mock.patch.object(stats, "_copy", racing), mock.patch.object(stats, "SNAPSHOT_ATTEMPTS", 3):
            with self.assertRaises(stats.SnapshotError):
                stats.open_snapshot(path)

    def test_snapshot_retries_when_wal_vanishes_mid_copy(self):
        path, w = self.wal_db()
        w.execute("INSERT INTO tasks VALUES('a','o1','done',2000000,NULL)")
        w.commit()
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        w.execute("INSERT INTO tasks VALUES('b','o1','done',2000001,NULL)")
        w.commit()
        real, calls = stats._copy, []

        def vanishing(src, dst):
            if src.endswith("-wal") and not calls:
                calls.append(1)
                raise FileNotFoundError(src)  # the last connection closed and SQLite deleted the WAL
            real(src, dst)
        with mock.patch.object(stats, "_copy", vanishing):
            with stats.open_snapshot(path) as snap:
                self.assertEqual(snap.con.execute("SELECT count(*) FROM tasks").fetchone()[0], 2)
        self.assertEqual(calls, [1])

    def test_snapshot_of_missing_db_is_an_error_not_a_created_file(self):
        with self.assertRaises(stats.SnapshotError):
            stats.open_snapshot(self.root / "nope.db")
        self.assertFalse((self.root / "nope.db").exists())


if __name__ == "__main__":
    unittest.main()
