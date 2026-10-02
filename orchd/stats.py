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
        # Retained for historical worker records and current Claude Orch costs.
        "claude-opus-5-5": dict(input="4", cache_read="0.2", output="20", cache_write="5", source=_ANTHROPIC),
        "gpt-6.1-sol": dict(input="2", cache_read="0.1", output="10", cache_write="2.5", source=_OPENAI),
    })
# Historical worker routing tiers (docs/decisions.md #13 and the 2026-09-30 Sol entry): M = sonnet / sol,
# H = opus (removed from worker choices). Historical event classification, not a quality ranking across families.
# Keyed by alias and full model id; any other model has no tier here, so its retries are unresolved, not counted zero.
ROUTING_TIERS = {"sonnet": 1, "sol": 1, "opus": 2,
                 "claude-sonnet-5-5": 1, "gpt-6.1-sol": 1, "claude-opus-5-5": 2}
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
                try:
                    for suffix in _SUFFIXES:
                        target = os.path.join(self._dir, "snap.db" + suffix)
                        if os.path.exists(target):
                            os.unlink(target)
                        if suffix in before:
                            _copy(str(path) + suffix, target)
                except FileNotFoundError:  # the writer closed and removed -wal/-journal mid-copy: it changed
                    continue
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
    """Cumulative counter dict from a token_count info, or None when input/output are not valid ints.
    cached is None (unknown, not 0) when cached_input_tokens is missing, invalid or larger than input_tokens;
    write_bad marks a present-but-invalid cache_write_input_tokens (an absent one reports nothing)."""
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    inp, outp = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if inp is None or outp is None:
        return None
    cached = _count(usage.get("cached_input_tokens"))
    write = _count(usage.get("cache_write_input_tokens"))
    total = _count(usage.get("total_tokens"))
    return dict(input=inp, cached=cached if cached is not None and cached <= inp else None, write=write or 0,
                write_bad=usage.get("cache_write_input_tokens") is not None and write is None, output=outp,
                total=inp + outp if total is None else total, reported_total=total is not None)


def codex_rollout_usage(root, thread, since=None):
    """Codex Orch tokens in the window from cumulative total_token_usage counters.

    Events are ordered by their timestamp (file order only breaks ties), so a late-written lower counter is
    just an older sample, not a reset. Increment = counter - previous counter (a repeat adds 0); a counter
    that still goes down in time order is a reset and counts from zero. The baseline for the first in-window
    increment is the last counter before the window; with none and a session that began before the window
    that delta is unknown, so it is skipped and the result is partial. Files of one thread add up.
    input_tokens includes cached_input_tokens (reported input = input - cached). When either end of an increment
    has no valid cached counter, that increment's input cannot be split: it goes to `input_cache_split_unknown`
    (neither uncached input nor cache read, unpriced) and the result is partial. The cache-write counter's
    relation to input_tokens is NOT established by the source, so it is kept raw in `cache_write_raw`, not
    added to any bucket, and flagged partial; a total_tokens that disagrees with input+output is flagged too."""
    files = sorted(Path(root).glob(f"**/rollout-*-{thread}.jsonl"))
    if not files:
        return None
    add, events, resets, partial = dict(input=0, cached=0, write=0, output=0), 0, 0, []
    usable = mismatched = split_unknown = cache_unknown_events = write_bad = 0
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
            reset = cur["total"] < prev["total"]
            resets += reset
            base = dict.fromkeys(add, 0) if reset else prev
            inc = {k: max(0, cur[k] - base[k]) for k in ("input", "write", "output")}
            known = cur["cached"] is not None and base["cached"] is not None
            prev = cur
            if in_window:
                events += 1
                write_bad += cur["write_bad"]
                for k in ("write", "output"):
                    add[k] += inc[k]
                if known:
                    add["cached"] += max(0, cur["cached"] - base["cached"])
                    add["input"] += inc["input"]
                else:
                    split_unknown += inc["input"]
                    cache_unknown_events += 1
    if not usable:
        return _no_usage(partial + ["rollout has no usable token_count record"])
    if add["write"]:
        partial.append(f"cache_write_input_tokens={add['write']} reported but not added: whether it is inside "
                       "input_tokens is not established by the source")
    if mismatched:
        partial.append(f"{mismatched} event(s) where total_tokens != input+output (cache-write accounting unclear)")
    if cache_unknown_events:
        partial.append(f"{cache_unknown_events} event(s) without a valid cached_input_tokens: {split_unknown} input "
                       "token(s) not split into uncached/cache read (not assumed zero)")
    if write_bad:
        partial.append(f"{write_bad} event(s) with an invalid cache_write_input_tokens, not counted")
    return dict(input=max(0, add["input"] - add["cached"]), cache_creation=0, cache_read=add["cached"],
                output=add["output"], input_cache_split_unknown=split_unknown, cache_write_raw=add["write"],
                messages=events, resets=resets, partial=partial)


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


def _final_session(task, msgs):
    """The task's last worker session id: tasks.session_id, else the newest successful retry's new_session_id
    when no failed retry came after it. None when the data cannot say."""
    if isinstance(task.get("session_id"), str) and task["session_id"]:
        return task["session_id"]
    attempts = [m for m in msgs if m["kind"] in ("retry", "retry_failed")]
    if attempts and attempts[-1]["kind"] == "retry":
        sid = _json(attempts[-1]["body"]).get("new_session_id")
        return sid if isinstance(sid, str) and sid else None
    return None


def _task_usage(task, msgs):
    """One closed task's worker usage, summed over its sessions.

    Every worker session counts once. A usage row tagged with session_id (written for a replaced session at
    retry) is deduplicated by that id, the last valid row winning. The untagged row is the close-time reading of
    the final session; an untagged row older than the newest retry is stale. The untagged row is added when it
    is provably a session no tagged row covers, dropped when the final session id is already tagged, and left
    out with the task marked partial when the data cannot tell (no final session id). Every session a retry
    replaced must have its own row (a retry without old_session_id cannot be checked), else partial.
    Returns (sessions {sid or None: (model, buckets, missing)}, status) where status is one of
    'complete' | 'partial' | 'stale' | 'invalid'."""
    rows = [m for m in msgs if m["kind"] == "usage"]
    retries = [m for m in msgs if m["kind"] == "retry"]
    last_retry = retries[-1]["id"] if retries else None
    tagged, untagged, gaps, bad = {}, None, False, 0
    for m in rows:
        body = _json(m["body"])
        sid = body.get("session_id")
        parsed = read_usage(body)
        if isinstance(sid, str) and sid:
            if parsed is None:
                bad += 1
                tagged.setdefault(sid, None)
            else:
                tagged[sid] = (body.get("model") or task["model"], *parsed)
        elif last_retry is not None and m["id"] < last_retry:
            continue  # a reading taken before a later retry: it cannot be the final number
        elif parsed is None:
            bad += 1
            untagged = None
        else:
            untagged = (body.get("model") or task["model"], *parsed)
    sessions = {sid: v for sid, v in tagged.items() if v is not None}
    gaps = bad > 0 or len(sessions) < len(tagged)
    for r in retries:
        old = _json(r["body"]).get("old_session_id")
        if not (isinstance(old, str) and old in sessions):
            gaps = True  # the replaced session has no valid usage of its own (or the event does not name it)
    final = _final_session(task, msgs)
    if untagged is not None:
        if final is not None and final not in tagged:
            sessions[final] = untagged
        elif final is None and not tagged:
            sessions[None] = untagged  # one session, never retried or tagged: nothing to overlap with
        elif final is None:
            gaps = True  # may or may not be one of the tagged sessions: leave it out, say partial
    elif final is None and not retries and len(sessions) == 1:
        pass  # never retried: the one tagged session is the only session
    elif final is None or final not in sessions:
        gaps = True  # the final session has no usage row
    if not sessions:
        return sessions, "invalid" if bad else "stale"
    return sessions, "partial" if gaps else "complete"


def _worker_usage(tasks, msgs_by_task, closed_ids):
    """Usage per closed task across all of its worker sessions (see _task_usage). Only a task whose every
    session is accounted for is coverage; a partial task still adds its known sessions (a lower bound) and
    keeps the cost incomplete. A valid record lacking a cache bucket counts but marks the cost partial."""
    covered, stale, invalid, partial, buckets = 0, [], [], [], {}
    for t in tasks:
        if t["id"] not in closed_ids:
            continue
        msgs = msgs_by_task.get(t["id"], [])
        if not any(m["kind"] == "usage" for m in msgs):
            continue
        sessions, status = _task_usage(t, msgs)
        if status in ("stale", "invalid"):
            (stale if status == "stale" else invalid).append(t["id"])
            continue
        if status == "complete":
            covered += 1
        else:
            partial.append(t["id"])
        for model, counters, missing in sessions.values():
            slot = buckets.setdefault(model, dict(_zero(), tasks=0, sessions=0, bucket_unknown_tasks=0))
            slot["sessions"] += 1
            for field in FIELDS:
                slot[field] += counters[field]
        for model in {v[0] for v in sessions.values()}:
            buckets[model]["tasks"] += 1
            buckets[model]["bucket_unknown_tasks"] += any(v[2] for v in sessions.values() if v[0] == model)
    return dict(covered=covered, stale=stale, invalid=invalid, partial=partial, by_model=buckets)


def _cost_block(by_model, prices, closed, covered, partial_tasks=0):
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
    missing_tasks = closed - covered - partial_tasks
    return dict(usd_estimate=str(total) if priced else None,
                complete=complete and not unknown and not missing_tasks and not partial_tasks and bool(closed),
                missing_usage_tasks=missing_tasks, partial_usage_tasks=partial_tasks, unknown_models=unknown,
                unpriced_tokens=unpriced,
                unverified_tokens=unverified,
                note="API list-price estimate (not a bill); a lower bound when PARTIAL")


def _tier(model):
    return ROUTING_TIERS.get(model.strip().lower()) if isinstance(model, str) else None


def classify_retry(body):
    """'upgrade' | 'not_upgrade' | 'unresolved' for one retry event body. Reads from/to, from_model/to_model or
    old_model and ignores any other field (the event schema is not settled). An upgrade is a move to a higher
    historical routing tier (Sol -> Opus counts for old events); same tier or a downgrade is not. A missing model,
    or a model without a routing tier, is unresolved: it is never counted as a known non-upgrade."""
    a = _tier(body.get("from") or body.get("from_model") or body.get("old_model"))
    b = _tier(body.get("to") or body.get("to_model"))
    if a is None or b is None:
        return "unresolved"
    return "upgrade" if b > a else "not_upgrade"


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
    cost = _cost_block(usage["by_model"], prices, len(closed), usage["covered"], len(usage["partial"]))
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
        escalation_note="escalation = retry to a higher historical routing tier (M = sonnet/sol, H = opus; an old routing "
                        "policy, not a quality ranking); same tier or downgrade is not; a retry without both models "
                        "or with a model outside those tiers is unresolved. Whether an upgrade "
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
                           usage_malformed=usage["invalid"], usage_partial_sessions=usage["partial"],
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
    split_unknown = usage.get("input_cache_split_unknown") or 0
    if split_unknown and cost is not None:
        unpriced = dict(unpriced, input_cache_split_unknown=split_unknown)
    return dict(kind=kind, source="transcript" if kind == "claude" else "rollout", model=model, tokens=tokens,
                total=sum(tokens.values()) + split_unknown, events=usage["messages"],
                counter_resets=usage.get("resets"), input_cache_split_unknown=usage.get("input_cache_split_unknown"),
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
        for col in ("model", "task_type", "rework_of", "found_by", "outcome", "session_id"):
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
                cells[cols.index("escalations")] += f"+ ({group['escalations_unresolved']} unresolved)"
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
