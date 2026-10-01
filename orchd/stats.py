"""`orchd stats`: per-Orch comparison from the orchd DB plus read-only transcripts. Never writes anything.

Basis: tasks created at or after --since (UTC epoch) are the cohort; question/rework/usage counts are read
from the cohort's rows. Orch tokens are different: they come from the Orch's own transcript/rollout and
count only events stamped inside the window, so they cannot be split by task_type.
Anything the data cannot say is None (printed "unknown"), never 0 and never a rate over nothing.
Cost is an API-list-price ESTIMATE, not a bill; a subscription is billed differently.
"""
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Prices are a data table, updated here when the official page changes (no global config).
# source: the 2026-10-02 task brief (Nat's Orch) states these were checked against the vendors' official
# API pricing pages that day; the page URLs were not re-fetched by this code. Standard API, per 1M tokens.
# cache_write is None on purpose: the official cache-write rate was not provided, so it is never guessed.
PRICES = dict(
    version="2026-10-02", unit="USD per 1M tokens", tier="standard API list price",
    source="orchd task f38f7fae brief (Nat/Orch, official pricing checked 2026-10-02); URL not re-fetched",
    models={
        "claude-sonnet-5-5": dict(input="2", cache_read="0.2", output="10", cache_write=None),
        "claude-opus-5-5": dict(input="4", cache_read="0.2", output="20", cache_write=None),
        "gpt-6.1-sol": dict(input="2", cache_read="0.1", output="10", cache_write=None),
    })
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


def open_readonly(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _zero():
    return dict.fromkeys(FIELDS, 0)


def _lines(path):
    for line in Path(path).read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def claude_transcript_usage(root, session_id, since=None):
    """Claude Orch tokens in the window. A message id spans several lines (last usage wins, first stamp
    places it); input/cache buckets are kept apart, never folded into one total twice."""
    files = sorted(Path(root).glob(f"*/{session_id}.jsonl"))
    if not files:
        return None
    seen, undated = {}, 0
    for path in files:
        for entry in _lines(path):
            msg = entry.get("message")
            if entry.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            key = msg.get("id") or entry.get("uuid")
            if key is None:
                continue
            ts = parse_ts(entry.get("timestamp"))
            if key in seen and seen[key][0] is not None:
                ts = seen[key][0] if ts is None else min(ts, seen[key][0])
            seen[key] = (ts, msg["usage"])
    out, n, partial = _zero(), 0, []
    for ts, u in seen.values():
        if since is not None and ts is None:
            undated += 1
            continue
        if since is not None and ts < since:
            continue
        n += 1
        for field, name in zip(FIELDS, ("input_tokens", "cache_creation_input_tokens",
                                        "cache_read_input_tokens", "output_tokens")):
            out[field] += u.get(name) or 0
    if undated:
        partial.append(f"{undated} message(s) without a timestamp excluded")
    return dict(out, messages=n, partial=partial)


def _counter(info):
    usage = info.get("total_token_usage") or {}
    get = lambda k: usage.get(k) or 0  # noqa: E731
    return dict(input=get("input_tokens"), cached=get("cached_input_tokens"),
                write=get("cache_write_input_tokens"), output=get("output_tokens"),
                total=usage.get("total_tokens") or get("input_tokens") + get("output_tokens"))


def codex_rollout_usage(root, thread, since=None):
    """Codex Orch tokens in the window from cumulative total_token_usage counters.

    Increment = counter - previous counter (so a repeated event adds 0). The baseline for the first
    in-window increment is the last counter before the window; with none and a session that began before
    the window the first in-window event has no known delta, so it is skipped and the result is partial.
    A counter that goes down is a reset: the new value counts from zero. Files of one thread add up."""
    files = sorted(Path(root).glob(f"**/rollout-*-{thread}.jsonl"))
    if not files:
        return None
    add, events, resets, partial = dict(input=0, cached=0, write=0, output=0), 0, 0, []
    for path in files:
        entries = list(_lines(path))
        start = next((t for t in (parse_ts(e.get("timestamp")) for e in entries) if t is not None), None)
        prev = None
        for e in entries:
            payload = e.get("payload") or {}
            info = payload.get("info") if payload.get("type") == "token_count" else None
            if not isinstance(info, dict) or not info.get("total_token_usage"):
                continue
            ts, cur = parse_ts(e.get("timestamp")), _counter(info)
            in_window = since is None or (ts is not None and ts >= since)
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
                inc = {k: max(0, cur[k] - prev[k]) for k in cur}
            prev = cur
            if in_window:
                events += 1
                for k in add:
                    add[k] += inc[k]
    return dict(input=max(0, add["input"] - add["cached"]), cache_creation=add["write"], cache_read=add["cached"],
                output=add["output"], messages=events, resets=resets, partial=partial)


def estimate_cost(model, usage, prices=PRICES):
    """(cost Decimal | None, complete bool, unpriced buckets). Unknown model -> None, never 0."""
    price = prices["models"].get(model)
    if price is None or usage is None:
        return None, False, {}
    cost, unpriced = Decimal(0), {}
    for field, key in (("input", "input"), ("cache_read", "cache_read"), ("output", "output"),
                       ("cache_creation", "cache_write")):
        tokens = usage.get(field) or 0
        if price.get(key) is None:
            if tokens:
                unpriced[field] = tokens
        else:
            cost += Decimal(price[key]) * tokens / MILLION
    return cost, not unpriced, unpriced


def _json(body):
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _rate(num, den):
    return None if not den else round(num / den, 4)


def _worker_usage(tasks, msgs_by_task, closed_ids):
    """Latest usage message per task, minus tasks whose usage predates a later retry (stale = missing)."""
    per, covered, stale, buckets = {}, 0, [], {}
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
        covered += 1
        model = body.get("model") or t["model"]
        slot = buckets.setdefault(model, dict(_zero(), tasks=0))
        slot["tasks"] += 1
        slot["input"] += body.get("input_tokens") or 0
        slot["cache_creation"] += body.get("cache_creation_input_tokens") or 0
        slot["cache_read"] += body.get("cache_read_input_tokens") or 0
        slot["output"] += body.get("output_tokens") or 0
    per["covered"], per["stale"], per["by_model"] = covered, stale, buckets
    return per


def _cost_block(by_model, prices):
    total, complete, unknown, unpriced, priced = Decimal(0), True, [], {}, 0
    for model, usage in by_model.items():
        cost, ok, miss = estimate_cost(model, usage, prices)
        if cost is None:
            unknown.append(model)
            continue
        total += cost
        priced += 1
        complete &= ok
        for k, v in miss.items():
            unpriced[k] = unpriced.get(k, 0) + v
    return dict(usd_estimate=str(total) if priced else None,
                complete=complete and not unknown, unknown_models=unknown, unpriced_tokens=unpriced,
                note="API list-price estimate (not a bill; subscription use is API-equivalent only)")


def _group(tasks, all_tasks, msgs_by_task, kinds_present, prices):
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
    cost = _cost_block(usage["by_model"], prices)
    have = lambda kind: kind in kinds_present  # noqa: E731
    retried = {m["task_id"] for m in msgs if m["kind"] == "retry"}
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
        escalations=len(retried) if have("retry") else None,
        escalations_completed=sum(1 for t in done if t["id"] in retried) if have("retry") else None,
        verify_events=kinds["verify"] if have("verify") else None,
        first_pass_verify_rate=_rate(sum(1 for c in first if c == 0), len(first)),
        first_pass_verify_n=len(first) if first else None,
        proxy_no_retry_rate=_rate(sum(1 for t in done if t["id"] not in retried), len(done)) if have("retry") else None,
        proxy_no_rework_rate=_rate(sum(1 for t in done_originals if t["id"] not in reworked_origin), len(done_originals)),
        proxy_note="no-retry / no-rework among completed tasks: proxies for model choice, not model accuracy",
        worker_tokens=dict(by_model=usage["by_model"], tasks_with_usage=usage["covered"], closed_tasks=len(closed),
                           open_tasks=len(tasks) - len(closed), usage_stale_after_retry=usage["stale"],
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
    cost, complete, unpriced = estimate_cost(model, usage, prices)
    tokens = {k: usage[k] for k in FIELDS}
    return dict(kind=kind, source="transcript" if kind == "claude" else "rollout", model=model, tokens=tokens,
                total=sum(tokens.values()), events=usage["messages"], counter_resets=usage.get("resets"),
                partial=usage["partial"], cost_usd_estimate=None if cost is None else str(cost),
                cost_complete=complete if cost is not None else None, unpriced_tokens=unpriced,
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
    kinds_present = {m["kind"] for ms in msgs_by_task.values() for m in ms}
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
            total=_group(mine, tasks, msgs_by_task, kinds_present, prices),
            by_task_type={k: _group(v, tasks, msgs_by_task, kinds_present, prices) for k, v in sorted(by_type.items())},
            orch_tokens=tokens))
    return dict(since=since, basis="tasks created >= since; orch_tokens = events stamped >= since (not split by type)",
                prices=dict(version=prices["version"], unit=prices["unit"], tier=prices["tier"], source=prices["source"],
                            cache_write_priced=False), orchs=rows)


def _show(value):
    return "unknown" if value is None else str(value)


def format_table(report):
    cols = ("tasks", "completed", "questions", "rework_tasks", "rework_found_by_nat", "retries", "followups",
            "first_pass_verify_rate", "proxy_no_retry_rate", "proxy_no_rework_rate", "cost_per_completed_usd")
    lines = [f"prices {report['prices']['version']} ({report['prices']['tier']}; estimates only, cache write unpriced)",
             f"basis: {report['basis']}", "", "orch / task_type | " + " | ".join(cols) + " | worker tokens cov."]
    for row in report["orchs"]:
        for name, group in [("*", row["total"]), *row["by_task_type"].items()]:
            lines.append(f"{row['orch']} / {name} | " + " | ".join(_show(group[c]) for c in cols)
                         + f" | {_show(group['worker_tokens']['coverage'])}")
        t = row["orch_tokens"]
        lines.append(f"  orch tokens ({t['source']}): " + (
            ", ".join(f"{k}={v}" for k, v in t["tokens"].items()) + f"; cost~{_show(t['cost_usd_estimate'])} USD"
            + (f" PARTIAL: {'; '.join(t['partial'])}" if t["partial"] else "") if t["tokens"] else "unknown"))
    return "\n".join(lines)
