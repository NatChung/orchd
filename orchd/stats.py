"""`orchd stats`: per-Orch comparison from the orchd DB plus read-only transcripts.

Nothing under the source DB's directory is ever written: the DB is read by copying its files byte for byte
into a private temp dir (see `open_snapshot`), never by opening it with SQLite in place.

Basis: tasks created at or after --since (UTC epoch) are the cohort; question/rework/usage counts are read
from the cohort's rows. Orch tokens are different: they come from the Orch's own transcript/rollout and
count only events stamped inside the window, so they cannot be split by task_type.
Anything the data cannot say is None (printed "unknown"), never 0 and never a rate over nothing. A source
file that exists but holds no usable usage is unknown, not zero. Cost is an API-list-price ESTIMATE.
"""
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Prices are a data table, updated here when the official page changes (no global config). Standard API,
# per 1M tokens, observed 2026-10-02. Cache-write rates are published; they are applied, but a cache-write
# cost stays PARTIAL because the usage records do not say the TTL / context tier the rate depends on.
_ANTHROPIC = "https://www.anthropic.com/claude-sonnet-5-5 (announcement 2026-09-28, pricing table)"
_OPENAI = "https://developers.openai.com/api/docs/pricing (Standard, short-context table)"
PRICES = dict(
    version="2026-10-02", unit="USD per 1M tokens", tier="standard API list price, short context",
    source="official pages below, fetched 2026-10-02", cache_write_priced=True,
    cache_write_note="published rate applied; TTL / context tier of each write is not in the usage data, so partial",
    models={
        "claude-sonnet-5-5": dict(input="2", cache_read="0.2", output="10", cache_write="2.5", source=_ANTHROPIC),
        "claude-opus-5-5": dict(input="4", cache_read="0.2", output="20", cache_write="5", source=_ANTHROPIC),
        "gpt-6.1-sol": dict(input="2", cache_read="0.1", output="10", cache_write="2.5", source=_OPENAI),
    })
# Only the Claude family has an ordering we can state; any other model pair is "unordered", never an upgrade.
MODEL_TIERS = ("haiku", "sonnet", "opus")
FIELDS = ("input", "cache_creation", "cache_read", "output")  # disjoint buckets; total = their sum
COMPLETED = ("merged", "done")  # outcome values that count as a completed task
MILLION = Decimal(1_000_000)


def parse_ts(value):
    """ISO-8601 with Z or an offset -> UTC epoch; a naive stamp is UTC. None when unparseable."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def parse_since(text, now=None):
    """HH:MM = today, local time (like watch/summary); otherwise ISO-8601 (Z/offset honoured, naive = local)."""
    now = now or datetime.now()
    try:
        hh, mm = text.split(":")
        if len(text) <= 5:
            return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0).timestamp()
    except ValueError:
        pass
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.timestamp()  # a naive datetime is read as local time by .timestamp()


class SnapshotError(RuntimeError):
    """The DB could not be copied consistently; nothing was read."""


_SUFFIXES = ("", "-wal", "-journal")  # the files that together hold the committed state (-shm is derived)
SNAPSHOT_ATTEMPTS = 5


def _fingerprint(path):
    out = {}
    for suffix in _SUFFIXES:
        try:
            st = os.stat(str(path) + suffix)
        except FileNotFoundError:
            continue
        out[suffix] = (st.st_size, st.st_mtime_ns, st.st_ino)
    return out


def _copy(src, dst):
    """Byte-for-byte copy with the source opened read-only; the copy is owner-only. A seam for tests."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with open(src, "rb") as r, os.fdopen(fd, "wb") as w:
        shutil.copyfileobj(r, w)


class Snapshot:
    """A private, consistent copy of the DB plus its WAL; `.con` reads it. Close it to delete the copy."""

    def __init__(self, path):
        path = Path(path)
        self._dir = tempfile.mkdtemp(prefix="orchd-stats-")  # mode 0700
        try:
            for _ in range(SNAPSHOT_ATTEMPTS):
                before = _fingerprint(path)
                if "" not in before:
                    raise SnapshotError(f"no DB at {path}")
                for suffix in _SUFFIXES:
                    target = os.path.join(self._dir, "snap.db" + suffix)
                    if os.path.exists(target):
                        os.unlink(target)
                    if suffix in before:
                        _copy(str(path) + suffix, target)
                if _fingerprint(path) == before:  # nothing moved (write, checkpoint) while copying
                    break
            else:
                raise SnapshotError(f"{path} kept changing during {SNAPSHOT_ATTEMPTS} copy attempts; "
                                    "no consistent snapshot, nothing reported")
            self.con = sqlite3.connect(os.path.join(self._dir, "snap.db"), timeout=30)
            self.con.row_factory = sqlite3.Row
        except BaseException:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise

    def close(self):
        con = getattr(self, "con", None)
        if con is not None:
            con.close()
            self.con = None
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_snapshot(path):
    return Snapshot(path)


BUCKETS = (("input", "input_tokens"), ("cache_creation", "cache_creation_input_tokens"),
           ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens"))


def _zero():
    return dict.fromkeys(FIELDS, 0)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def read_usage(usage):
    """(buckets, missing buckets) from a Claude-shaped usage dict, or None when it is not usable evidence.
    input_tokens and output_tokens must be non-negative ints; a present-but-invalid counter makes the whole
    record unusable. A missing cache bucket is not a zero: it is listed in `missing` (cost goes partial)."""
    if not isinstance(usage, dict):
        return None
    out, missing = _zero(), []
    for field, name in BUCKETS:
        if usage.get(name) is None:
            if field in ("input", "output"):
                return None
            missing.append(field)
            continue
        value = _count(usage[name])
        if value is None:
            return None
        out[field] = value
    return out, missing


def _lines(path):
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _no_usage(partial):
    return dict(_zero(), messages=0, partial=partial, no_usage=True)


def claude_transcript_usage(root, session_id, since=None):
    """Claude Orch tokens in the window. One message id spans several lines (and, after a resume, several
    files): the final usage is the one with the latest timestamp (ties: larger output, then later position),
    never the one from the lexicographically last file. The first stamp places the message in the window.
    A transcript with no usable usage record at all is `no_usage` (unknown), not zero."""
    files = sorted(Path(root).glob(f"*/{session_id}.jsonl"))
    if not files:
        return None
    best, bad, order = {}, 0, 0
    for path in files:
        for entry in _lines(path):
            msg = entry.get("message")
            if entry.get("type") != "assistant" or not isinstance(msg, dict) or "usage" not in msg:
                continue
            parsed = read_usage(msg["usage"])
            key = msg.get("id") or entry.get("uuid")
            if parsed is None or key is None:
                bad += 1
                continue
            order += 1
            ts = parse_ts(entry.get("timestamp"))
            rank = (float("-inf") if ts is None else ts, parsed[0]["output"], order)
            cur = best.get(key)
            if cur is None:
                best[key] = dict(first=ts, rank=rank, parsed=parsed)
                continue
            if ts is not None and (cur["first"] is None or ts < cur["first"]):
                cur["first"] = ts
            if rank > cur["rank"]:
                cur["rank"], cur["parsed"] = rank, parsed
    notes = [f"{bad} usage record(s) malformed or without counters ignored"] if bad else []
    if not best:
        return _no_usage(notes + ["transcript has no usable assistant usage record"])
    out, n, undated, lacking = _zero(), 0, 0, Counter()
    for rec in best.values():
        if since is not None and rec["first"] is None:
            undated += 1
            continue
        if since is not None and rec["first"] < since:
            continue
        n += 1
        buckets, missing = rec["parsed"]
        lacking.update(missing)
        for field in FIELDS:
            out[field] += buckets[field]
    if undated:
        notes.append(f"{undated} message(s) without a timestamp excluded")
    for field, count in sorted(lacking.items()):
        notes.append(f"{count} message(s) without {field} counter (not assumed zero)")
    return dict(out, messages=n, partial=notes)


def _counter(info):
    """Cumulative counter dict from a token_count info, or None when input/output are not valid ints."""
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    inp, outp = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if inp is None or outp is None:
        return None
    get = lambda k: _count(usage.get(k)) or 0  # noqa: E731
    total = _count(usage.get("total_tokens"))
    return dict(input=inp, cached=get("cached_input_tokens"), write=get("cache_write_input_tokens"), output=outp,
                total=inp + outp if total is None else total, reported_total=total is not None)


def codex_rollout_usage(root, thread, since=None):
    """Codex Orch tokens in the window from cumulative total_token_usage counters.

    Events are ordered by their timestamp (file order only breaks ties), so a late-written lower counter is
    just an older sample, not a reset. Increment = counter - previous counter (a repeat adds 0); a counter
    that still goes down in time order is a reset and counts from zero. The baseline for the first in-window
    increment is the last counter before the window; with none and a session that began before the window
    that delta is unknown, so it is skipped and the result is partial. Files of one thread add up.
    input_tokens includes cached_input_tokens (reported input = input - cached). The cache-write counter's
    relation to input_tokens is NOT established by the source, so it is kept raw in `cache_write_raw`, not
    added to any bucket, and flagged partial; a total_tokens that disagrees with input+output is flagged too."""
    files = sorted(Path(root).glob(f"**/rollout-*-{thread}.jsonl"))
    if not files:
        return None
    add, events, resets, partial = dict(input=0, cached=0, write=0, output=0), 0, 0, []
    usable = mismatched = 0
    for path in files:
        entries = list(_lines(path))
        start = min((t for t in (parse_ts(e.get("timestamp")) for e in entries) if t is not None), default=None)
        samples, bad, undated = [], 0, 0
        for idx, e in enumerate(entries):
            payload = e.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            info = payload.get("info") if payload.get("type") == "token_count" else None
            if not isinstance(info, dict) or not info.get("total_token_usage"):
                continue
            cur, ts = _counter(info), parse_ts(e.get("timestamp"))
            if cur is None:
                bad += 1
            elif ts is None:
                undated += 1
            else:
                samples.append((ts, idx, cur))
        if bad:
            partial.append(f"{path.name}: {bad} counter record(s) malformed, ignored")
        if undated:
            partial.append(f"{path.name}: {undated} counter record(s) without a timestamp, cannot be ordered, ignored")
        usable += len(samples)
        prev = None
        for ts, _, cur in sorted(samples, key=lambda x: x[:2]):
            in_window = since is None or ts >= since
            if cur["reported_total"] and cur["total"] != cur["input"] + cur["output"] and in_window:
                mismatched += 1
            if prev is None:
                if since is None or (start is not None and start >= since):
                    prev = dict.fromkeys(cur, 0)  # the session began inside the window: counters start at 0
                elif not in_window:
                    prev = cur
                    continue
                else:
                    prev = cur
                    partial.append(f"{path.name}: no counter before the window, first in-window delta unknown")
                    continue
            if cur["total"] < prev["total"]:
                resets += 1
                inc = cur
            else:
                inc = {k: max(0, cur[k] - prev[k]) for k in add}
            prev = cur
            if in_window:
                events += 1
                for k in add:
                    add[k] += inc[k]
    if not usable:
        return _no_usage(partial + ["rollout has no usable token_count record"])
    if add["write"]:
        partial.append(f"cache_write_input_tokens={add['write']} reported but not added: whether it is inside "
                       "input_tokens is not established by the source")
    if mismatched:
        partial.append(f"{mismatched} event(s) where total_tokens != input+output (cache-write accounting unclear)")
    return dict(input=max(0, add["input"] - add["cached"]), cache_creation=0, cache_read=add["cached"],
                output=add["output"], cache_write_raw=add["write"], messages=events, resets=resets, partial=partial)


def estimate_cost(model, usage, prices=PRICES, bucket_unknown=False):
    """(cost Decimal | None, complete bool, unpriced buckets, unverified buckets). Unknown model -> None.
    unpriced = tokens with no price; unverified = cache-write tokens priced at the published rate whose
    TTL/context tier the data does not say. Either, or bucket_unknown (a bucket missing from the source),
    makes the cost incomplete."""
    price = prices["models"].get(model)
    if price is None or usage is None:
        return None, False, {}, {}
    cost, unpriced, unverified = Decimal(0), {}, {}
    for field, key in (("input", "input"), ("cache_read", "cache_read"), ("output", "output"),
                       ("cache_creation", "cache_write")):
        tokens = usage.get(field) or 0
        if price.get(key) is None:
            if tokens:
                unpriced[field] = tokens
            continue
        cost += Decimal(price[key]) * tokens / MILLION
        if field == "cache_creation" and tokens:
            unverified[field] = tokens
    return cost, not unpriced and not unverified and not bucket_unknown, unpriced, unverified


def _json(body):
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _rate(num, den):
    return None if not den else round(num / den, 4)


def _worker_usage(tasks, msgs_by_task, closed_ids):
    """Latest usage message per closed task. Missing, malformed (no valid counters) and stale (a retry came
    after it) usage is not coverage. A valid record lacking a cache bucket counts but marks the cost partial."""
    covered, stale, invalid, buckets = 0, [], [], {}
    for t in tasks:
        if t["id"] not in closed_ids:
            continue
        msgs = msgs_by_task.get(t["id"], [])
        usage = [m for m in msgs if m["kind"] == "usage"]
        if not usage:
            continue
        if any(m["kind"] == "retry" and m["id"] > usage[-1]["id"] for m in msgs):
            stale.append(t["id"])
            continue
        body = _json(usage[-1]["body"])
        parsed = read_usage(body)
        if parsed is None:
            invalid.append(t["id"])
            continue
        counters, missing = parsed
        covered += 1
        slot = buckets.setdefault(body.get("model") or t["model"], dict(_zero(), tasks=0, bucket_unknown_tasks=0))
        slot["tasks"] += 1
        slot["bucket_unknown_tasks"] += bool(missing)
        for field in FIELDS:
            slot[field] += counters[field]
    return dict(covered=covered, stale=stale, invalid=invalid, by_model=buckets)


def _cost_block(by_model, prices, closed, covered):
    total, complete, unknown, unpriced, unverified, priced = Decimal(0), True, [], {}, {}, 0
    for model, usage in by_model.items():
        cost, ok, miss, unver = estimate_cost(model, usage, prices, bool(usage["bucket_unknown_tasks"]))
        if cost is None:
            unknown.append(model)
            continue
        total += cost
        priced += 1
        complete &= ok
        for k, v in miss.items():
            unpriced[k] = unpriced.get(k, 0) + v
        for k, v in unver.items():
            unverified[k] = unverified.get(k, 0) + v
    missing_tasks = closed - covered
    return dict(usd_estimate=str(total) if priced else None,
                complete=complete and not unknown and not missing_tasks and bool(closed),
                missing_usage_tasks=missing_tasks, unknown_models=unknown, unpriced_tokens=unpriced,
                unverified_tokens=unverified,
                note="API list-price estimate (not a bill); a lower bound when PARTIAL")


def _tier(model):
    if isinstance(model, str):
        for rank, name in enumerate(MODEL_TIERS):
            if name in model.lower():
                return rank
    return None


def classify_retry(body):
    """'upgrade' | 'not_upgrade' | 'unresolved' for one retry event body. Needs both models named; reads
    from/to or from_model/to_model and ignores any other field (the event schema is not settled). An upgrade
    is a higher tier of the same ordered family; same model, a downgrade, or models we cannot order are not."""
    old = body.get("from") or body.get("from_model")
    new = body.get("to") or body.get("to_model")
    if not isinstance(old, str) or not isinstance(new, str):
        return "unresolved"
    a, b = _tier(old), _tier(new)
    return "upgrade" if a is not None and b is not None and b > a else "not_upgrade"


def _group(tasks, all_tasks, msgs_by_task, prices):
    ids = {t["id"] for t in tasks}
    msgs = [m for i in ids for m in msgs_by_task.get(i, [])]
    kinds = Counter(m["kind"] for m in msgs)
    closed = {t["id"] for t in tasks if t["status"] == "closed"}
    done = [t for t in tasks if t["status"] == "closed" and t["outcome"] in COMPLETED]
    originals = [t for t in tasks if not t["rework_of"]]
    reworks = [t for t in tasks if t["rework_of"]]
    reworked_origin = {t["rework_of"] for t in all_tasks if t["rework_of"]}
    done_originals = [t for t in done if not t["rework_of"]]
    usage = _worker_usage(tasks, msgs_by_task, closed)
    cost = _cost_block(usage["by_model"], prices, len(closed), usage["covered"])
    # Event coverage per source is not established: no events of a kind in THIS group is unknown, never 0.
    have = lambda kind: kinds[kind] > 0  # noqa: E731
    retried = {m["task_id"] for m in msgs if m["kind"] == "retry"}
    verdicts = {m["task_id"]: [] for m in msgs if m["kind"] == "retry"}
    for m in msgs:
        if m["kind"] == "retry":
            verdicts[m["task_id"]].append(classify_retry(_json(m["body"])))
    upgraded = {t for t, v in verdicts.items() if "upgrade" in v}
    unresolved = sum(v.count("unresolved") for v in verdicts.values())
    verifies = {}
    for m in msgs:
        if m["kind"] == "verify":
            verifies.setdefault(m["task_id"], []).append(_json(m["body"]).get("exit_code"))
    first = [v[0] for v in verifies.values() if isinstance(v[0], int) and not isinstance(v[0], bool)]
    return dict(
        tasks=len(tasks),
        status=dict(Counter(t["status"] for t in tasks)),
        model=dict(Counter(t["model"] or "unknown" for t in tasks)),
        completed=len(done), closed_without_outcome=sum(1 for t in tasks if t["id"] in closed and t["outcome"] is None),
        completed_definition=f"closed with outcome in {list(COMPLETED)}",
        questions=kinds["question"], answers=kinds["answer"],
        rework_tasks=len(reworks), rework_rate=_rate(len(reworks), len(originals)),
        rework_found_by=dict(Counter(t["found_by"] or "unknown" for t in reworks)),
        rework_found_by_nat=sum(1 for t in reworks if t["found_by"] == "nat"),
        retries=kinds["retry"] if have("retry") else None,
        followups=kinds["followup"] if have("followup") else None,
        retries_per_task=_rate(kinds["retry"], len(tasks)) if have("retry") else None,
        followups_per_task=_rate(kinds["followup"], len(tasks)) if have("followup") else None,
        escalations=sum(len([x for x in v if x == "upgrade"]) for v in verdicts.values()) if have("retry") else None,
        escalations_unresolved=unresolved if have("retry") else None,
        escalations_complete=(unresolved == 0) if have("retry") else None,
        escalated_tasks_outcome_completed=(sum(1 for t in done if t["id"] in upgraded) if have("retry") else None),
        escalations_post_upgrade_verified=None,
        escalation_note="escalation = retry to a higher model tier (from/to); same model, downgrade or "
                        "unorderable models are not; retries without from/to are unresolved. Whether an upgrade "
                        "then passed verification is unknown: no post-upgrade verify role is recorded, and a "
                        "done outcome is not a verified pass",
        verify_events=kinds["verify"] if have("verify") else None,
        first_pass_verify_rate=_rate(sum(1 for c in first if c == 0), len(first)),
        first_pass_verify_n=len(first) if first else None,
        verify_note="author vs independent verify role is not recorded; verify bodies without exit_code are excluded",
        proxy_no_retry_rate=_rate(sum(1 for t in done if t["id"] not in retried), len(done)) if have("retry") else None,
        proxy_no_rework_rate=_rate(sum(1 for t in done_originals if t["id"] not in reworked_origin), len(done_originals)),
        proxy_note="no-retry / no-rework among completed tasks: proxies for model choice, not model accuracy",
        worker_tokens=dict(by_model=usage["by_model"], tasks_with_usage=usage["covered"], closed_tasks=len(closed),
                           open_tasks=len(tasks) - len(closed), usage_stale_after_retry=usage["stale"],
                           usage_malformed=usage["invalid"],
                           coverage=f"{usage['covered']}/{len(closed)}" if closed else None),
        cost=cost,
        cost_per_completed_usd=(str(Decimal(cost["usd_estimate"]) / len(done))
                                if cost["usd_estimate"] is not None and done else None),
        cost_per_completed_complete=cost["complete"] if done else None)


def orch_tokens(orch, thread, since, claude_root, codex_root, prices):
    kind = (orch["kind"] if orch is not None else None) or "codex"
    if kind == "claude":
        usage = claude_transcript_usage(claude_root, orch["session_id"], since) if orch["session_id"] else None
        model = orch["model"]
    else:
        usage, model = codex_rollout_usage(codex_root, thread, since), None  # the DB does not record Astra's model
    if usage is None:
        return dict(kind=kind, source="missing", model=model, tokens=None, partial=["no transcript/rollout found"])
    if usage.get("no_usage"):
        return dict(kind=kind, source="unusable", model=model, tokens=None, partial=usage["partial"])
    cost, complete, unpriced, unverified = estimate_cost(model, usage, prices)
    tokens = {k: usage[k] for k in FIELDS}
    return dict(kind=kind, source="transcript" if kind == "claude" else "rollout", model=model, tokens=tokens,
                total=sum(tokens.values()), events=usage["messages"], counter_resets=usage.get("resets"),
                cache_write_unattributed=usage.get("cache_write_raw"),
                partial=usage["partial"], cost_usd_estimate=None if cost is None else str(cost),
                cost_complete=(complete and not usage["partial"]) if cost is not None else None,
                unpriced_tokens=unpriced, unverified_tokens=unverified,
                cost_note="API list-price estimate, not a bill" if cost is not None else "unknown model: no estimate")


def stats(con, since=None, claude_root=None, codex_root=None, prices=PRICES):
    claude_root = claude_root or os.environ.get("ORCHD_CLAUDE_PROJECTS", Path.home() / ".claude" / "projects")
    codex_root = codex_root or os.environ.get("ORCHD_CODEX_SESSIONS", Path.home() / ".codex" / "sessions")
    tasks = [dict(r) for r in con.execute("SELECT * FROM tasks ORDER BY created_at")]
    for t in tasks:
        for col in ("model", "task_type", "rework_of", "found_by", "outcome"):
            t.setdefault(col, None)
    msgs_by_task = {}
    for m in con.execute("SELECT * FROM messages ORDER BY id"):
        msgs_by_task.setdefault(m["task_id"], []).append(dict(m))
    orchs = {r["id"]: r for r in con.execute("SELECT * FROM orchs")}
    cohort = [t for t in tasks if since is None or t["created_at"] >= since]
    rows = []
    for orch_id in dict.fromkeys([t["orch_thread"] for t in cohort] + list(orchs)):
        mine = [t for t in cohort if t["orch_thread"] == orch_id]
        tokens = orch_tokens(orchs.get(orch_id), orch_id, since, claude_root, codex_root, prices)
        if not mine and not (tokens["tokens"] and tokens["total"]):
            continue
        orch = orchs.get(orch_id)
        label = (("Claude Orch " if orch and orch["kind"] == "claude" else "Astra ") + orch_id[:8])
        by_type = {}
        for t in mine:
            by_type.setdefault(t["task_type"] or "unknown", []).append(t)
        rows.append(dict(
            orch=label, orch_id=orch_id,
            total=_group(mine, tasks, msgs_by_task, prices),
            by_task_type={k: _group(v, tasks, msgs_by_task, prices) for k, v in sorted(by_type.items())},
            orch_tokens=tokens))
    return dict(since=since, basis="tasks created >= since; orch_tokens = events stamped >= since (not split by type)",
                prices=dict(version=prices["version"], unit=prices["unit"], tier=prices["tier"], source=prices["source"],
                            cache_write_priced=prices["cache_write_priced"], cache_write_note=prices["cache_write_note"],
                            models={m: dict(p) for m, p in prices["models"].items()}), orchs=rows)


def _show(value):
    return "unknown" if value is None else str(value)


def _cost_cell(group):
    cost = group["cost"]
    if cost["usd_estimate"] is None:
        return "unknown"
    value = group["cost_per_completed_usd"]
    text = "unknown" if value is None else f"{value}~"
    return text if cost["complete"] else f"{text} PARTIAL"


def format_table(report):
    cols = ("tasks", "completed", "questions", "rework_tasks", "rework_rate", "rework_found_by_nat", "retries",
            "retries_per_task", "followups", "followups_per_task", "escalations", "escalations_post_upgrade_verified",
            "first_pass_verify_rate", "proxy_no_retry_rate", "proxy_no_rework_rate")
    lines = [f"prices {report['prices']['version']} ({report['prices']['tier']}; estimates only; "
             f"cache write priced but tier unverified => PARTIAL)",
             f"basis: {report['basis']}", "",
             "orch / task_type | " + " | ".join(cols) + " | cost/completed | worker tokens cov."]
    for row in report["orchs"]:
        for name, group in [("*", row["total"]), *row["by_task_type"].items()]:
            cells = [_show(group[c]) for c in cols]
            if group["escalations"] is not None and group["escalations_unresolved"]:
                cells[cols.index("escalations")] += f" (>= ; {group['escalations_unresolved']} unresolved)"
            lines.append(f"{row['orch']} / {name} | " + " | ".join(cells)
                         + f" | {_cost_cell(group)} | {_show(group['worker_tokens']['coverage'])}")
        t = row["orch_tokens"]
        if not t["tokens"]:
            lines.append(f"  orch tokens ({t['source']}): unknown" + (f" ({'; '.join(t['partial'])})" if t["partial"] else ""))
            continue
        flags = list(t["partial"])
        if t["cost_complete"] is False:
            flags.append("cost incomplete (cache-write tier / unpriced / partial tokens)")
        lines.append(f"  orch tokens ({t['source']}): " + ", ".join(f"{k}={v}" for k, v in t["tokens"].items())
                     + f"; cost~{_show(t['cost_usd_estimate'])} USD" + (f" PARTIAL: {'; '.join(flags)}" if flags else ""))
    return "\n".join(lines)
