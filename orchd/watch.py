"""Live timeline of Orch <-> worker traffic, and a per-Orch summary. Read-only over the orchd DB."""
import json
import shutil
import time
from collections import Counter

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
ORCH_COLORS = {"claude": "\033[38;5;208m", "codex": "\033[38;5;39m"}
WORKER_COLOR = "\033[38;5;114m"
ICONS = {"dispatch": "▶", "ack": "✓", "progress": "…", "question": "?", "answer": "↩", "report": "■",
         "close": "✔", "usage": "Σ"}

QUERY = """SELECT m.id, m.task_id, m.kind, m.body, m.evidence, m.created_at,
                  t.repo, t.title, t.model, t.model_reason, t.orch_thread, o.kind AS orch_kind, o.model AS orch_model
           FROM messages m JOIN tasks t ON t.id = m.task_id LEFT JOIN orchs o ON o.id = t.orch_thread
           WHERE m.id > ? ORDER BY m.id"""


def _short_model(model):
    if not model:  # dispatched before v2, when every worker ran Opus
        return "opus"
    return model.replace("claude-", "").replace("-5-5", "").replace("gpt-6.1-", "")


def orch_label(row, color=True):
    kind = row["orch_kind"] or "codex"
    name = f"Claude Orch {row['orch_thread'][:8]}" if kind == "claude" else f"Astra {row['orch_thread'][:8]}"
    return f"{ORCH_COLORS[kind]}{BOLD}{name}{RESET}" if color else name


def worker_label(row, color=True):
    name = f"worker {row['task_id']} ({_short_model(row['model'])})"
    return f"{WORKER_COLOR}{name}{RESET}" if color else name


def _first(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def format_row(row, width=160, color=True):
    """One timeline line: who talks to whom, and what."""
    orch, worker = orch_label(row, color), worker_label(row, color)
    kind, body = row["kind"], row["body"] or ""
    stamp = time.strftime("%H:%M:%S", time.localtime(row["created_at"]))
    icon = ICONS.get(kind, "·")
    if kind == "dispatch":
        try:
            reason = json.loads(body).get("model_reason") or ""
        except ValueError:
            reason = ""
        text = f"{orch} → {worker}  [{row['repo']}] {row['title']}" + (f"  {DIM}· {reason}{RESET}" if color else f"  · {reason}")
    elif kind == "answer":
        text = f"{orch} → {worker}: {body}"
    elif kind == "close":
        try:
            outcome = json.loads(body).get("outcome") or "closed"
        except ValueError:
            outcome = "closed"
        text = f"{orch} closes {worker}: {outcome}"
    elif kind == "usage":
        try:
            u = json.loads(body)
            body = (f"{u.get('output_tokens', 0):,} out / "
                    f"{u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0):,} in tokens")
        except ValueError:
            pass
        text = f"{worker} {body}"
    else:
        label = {"ack": "got it", "progress": "progress", "question": "asks", "report": "reports"}.get(kind, kind)
        text = f"{worker} → {orch}: {label}: {body}"
    return f"{DIM if color else ''}{stamp}{RESET if color else ''} {icon} {_first(text, width)}"


def watch(con, since=None, follow=True, out=print, interval=1.0, color=True):
    last = 0
    if since is not None:
        row = con.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE created_at < ?", (since,)).fetchone()
        last = row[0]
    while True:
        width = max(80, shutil.get_terminal_size((160, 40)).columns + 40)  # ANSI codes take no columns
        for row in con.execute(QUERY, (last,)).fetchall():
            out(format_row(row, width, color))
            last = row["id"]
        if not follow:
            return
        time.sleep(interval)


def summary(con, since=0):
    """Per Orch: how many workers it ran, with which models, and how much work overlapped."""
    tasks = con.execute("""SELECT t.*, o.kind AS orch_kind FROM tasks t LEFT JOIN orchs o ON o.id = t.orch_thread
                           WHERE t.created_at >= ? ORDER BY t.created_at""", (since,)).fetchall()
    out = []
    for orch in dict.fromkeys(t["orch_thread"] for t in tasks):
        mine = [t for t in tasks if t["orch_thread"] == orch]
        ids = [t["id"] for t in mine]
        marks = ",".join("?" * len(ids))
        msgs = con.execute(f"SELECT * FROM messages WHERE task_id IN ({marks}) ORDER BY id", ids).fetchall()
        spans, first, last = [], None, None
        for t in mine:
            times = [m["created_at"] for m in msgs if m["task_id"] == t["id"]]
            done = [m["created_at"] for m in msgs if m["task_id"] == t["id"] and m["kind"] == "report"]
            start, end = t["created_at"], (done[-1] if done else (times[-1] if times else t["created_at"]))
            spans.append(end - start)
            first = start if first is None else min(first, start)
            last = end if last is None else max(last, end)
        edges = sorted([(t["created_at"], 1) for t in mine] + [(t["created_at"] + d, -1) for t, d in zip(mine, spans)],
                       key=lambda e: (e[0], e[1]))
        running = peak = 0
        for _, step in edges:
            running += step
            peak = max(peak, running)
        tokens = 0
        for m in msgs:
            if m["kind"] == "usage":
                try:
                    tokens += json.loads(m["body"]).get("output_tokens", 0)
                except ValueError:
                    pass
        kinds = Counter(m["kind"] for m in msgs)
        wall = (last - first) if first is not None else 0
        busy = sum(spans)
        out.append(dict(
            orch=("Claude Orch " if mine[0]["orch_kind"] == "claude" else "Astra ") + orch[:8],
            workers=len(mine), models=dict(Counter(_short_model(t["model"]) for t in mine)),
            questions=kinds["question"], answers=kinds["answer"], progress=kinds["progress"],
            rework=sum(1 for t in mine if t["rework_of"]),
            wall_min=round(wall / 60, 1), worker_min=round(busy / 60, 1),
            parallel=round(busy / wall, 1) if wall else None, peak_workers=peak, output_tokens=tokens))
    return out
