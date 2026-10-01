import json
import tempfile
import unittest
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
        self.assertEqual((t["retries"], t["followups"], t["escalations"], t["escalations_completed"]), (1, 2, 1, 1))
        self.assertEqual((t["verify_events"], t["first_pass_verify_n"], t["first_pass_verify_rate"]), (4, 2, 0.5))
        self.assertEqual(t["proxy_no_retry_rate"], round(2 / 3, 4))
        self.assertEqual(t["retries_per_task"], round(1 / 3, 4))

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
        self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal("3.1"))
        self.assertTrue(t["cost"]["complete"])
        self.assertEqual(Decimal(t["cost_per_completed_usd"]), Decimal("1.55"))
        self.assertIn("not a bill", t["cost"]["note"])

    def test_cache_creation_is_separate_and_unpriced_marks_incomplete(self):
        self.task("a")
        self.close("a", "merged", dict(input_tokens=10, output_tokens=20, cache_creation_input_tokens=7,
                                       cache_read_input_tokens=30))
        t = self.total()
        buckets = t["worker_tokens"]["by_model"]["claude-sonnet-5-5"]
        self.assertEqual((buckets["input"], buckets["cache_creation"], buckets["cache_read"], buckets["output"]),
                         (10, 7, 30, 20))
        self.assertFalse(t["cost"]["complete"])  # cache write rate is not known, so it is not guessed
        self.assertEqual(t["cost"]["unpriced_tokens"], {"cache_creation": 7})
        self.assertEqual(Decimal(t["cost"]["usd_estimate"]), Decimal("0.000226"))  # (10*2 + 30*0.2 + 20*10) / 1M

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
            {"type": "assistant", "message": {"id": "m3", "usage": {"output_tokens": 77}}},  # no timestamp
            {"type": "user"},
        ])
        self.task("a")
        (row,) = self.report()["orchs"]
        t = row["orch_tokens"]
        self.assertEqual(t["tokens"], dict(input=1_000_001, cache_creation=4, cache_read=100_000, output=15))
        self.assertEqual(t["total"], 1_100_020)  # sum of disjoint buckets, nothing counted twice
        self.assertEqual(t["events"], 2)
        self.assertIn("without a timestamp", t["partial"][0])
        # opus: 1,000,001*4 + 100,000*0.2 + 15*20 per 1M; cache_creation unpriced
        self.assertEqual(Decimal(t["cost_usd_estimate"]), Decimal("4.020304"))  # 4.000004 + 0.02 + 0.0003
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

    def test_readonly_open_does_not_migrate_or_create(self):
        path = self.root / "ro.db"
        import sqlite3
        raw = sqlite3.connect(path)
        raw.executescript("CREATE TABLE tasks(id TEXT PRIMARY KEY, orch_thread TEXT, status TEXT, created_at REAL, "
                          "model TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, "
                          "body TEXT); CREATE TABLE orchs(id TEXT PRIMARY KEY, kind TEXT, model TEXT, session_id TEXT);"
                          "INSERT INTO tasks VALUES('a','o1','done',2000000,NULL)")
        raw.commit()
        raw.close()
        ro = stats.open_readonly(path)
        self.addCleanup(ro.close)
        rep = stats.stats(ro, None, self.claude, self.codex)  # old schema: no task_type etc.
        self.assertEqual(rep["orchs"][0]["total"]["tasks"], 1)
        con = sqlite3.connect(path)
        self.assertNotIn("task_type", {r[1] for r in con.execute("PRAGMA table_info(tasks)")})


if __name__ == "__main__":
    unittest.main()
