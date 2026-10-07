"""A static, private, read-only snapshot. Never calls inbox or entry.status/delivery."""
from collections import defaultdict
from datetime import date, datetime, timezone
from html import escape

from . import core, goals


def snapshot(con, rt, today=None):
    today = today or date.today()
    tasks = core.list_open(con, rt)
    task_by_id = {t["task_id"]: t for t in tasks}
    projects = defaultdict(lambda: dict(goals=[], tasks=[], questions=[]))
    for task in tasks:
        projects[task["repo"]]["tasks"].append(task)
    # Only questions actually addressed to Operator, not workers' questions awaiting Orch triage.
    for row in con.execute("SELECT * FROM entry_messages WHERE direction='out' AND kind='question' "
                           "AND question_state IN ('current','queued') ORDER BY id"):
        task = task_by_id.get(row["task_id"])
        repo = task["repo"] if task else "unassigned"
        projects[repo]["questions"].append(dict(
            id=row["id"], task_id=row["task_id"], body=row["body"], state=row["question_state"],
            delivery=row["delivery"], created_at=row["created_at"],
            goal_id=task["goal_id"] if task else None,
            goal_critical=task["goal_critical"] if task else None))
    for goal in goals.list_goals(con):
        mids = con.execute("SELECT MAX(m.created_at) FROM messages m JOIN tasks t ON t.id=m.task_id "
                           "WHERE t.goal_id=? AND m.kind IN ('progress','report')", (goal["id"],)).fetchone()[0]
        progress_date = datetime.fromtimestamp(mids).date().isoformat() if mids is not None else None
        questions = [q for q in projects[goal["repo"]]["questions"] if q["goal_id"] == goal["id"]]
        waiting = min((q["created_at"] for q in questions), default=None)
        nat_since = datetime.fromtimestamp(waiting).date().isoformat() if waiting is not None else None
        goal["priority"] = goals.score(goal, today, progress_date=progress_date, nat_since=nat_since)
        projects[goal["repo"]]["goals"].append(goal)
    out = []
    for repo, project in projects.items():
        active = [g for g in project["goals"] if g["status"] in ("active", "waiting")]
        scores = [g["priority"]["score"] for g in active if g["priority"]["score"] is not None]
        project.update(repo=repo, score=max(scores, default=None),
                       paused=bool(project["goals"]) and all(g["status"] == "paused" for g in project["goals"]))
        out.append(project)
    out.sort(key=lambda p: (p["paused"], p["score"] is None, -(p["score"] or 0), p["repo"]))
    return dict(generated_at=datetime.now(timezone.utc).isoformat(), projects=out)


def _text(value):
    return escape(str(value if value is not None else "未定"))


def _details(label, body, opened=False, css=""):
    return f'<details class="{css}"{" open" if opened else ""}><summary>{_text(label)}</summary>{body}</details>'


def _row(title, subtitle="", status="", critical=False):
    tone = "c-crit" if critical else "c-run"
    return (f'<div class="row"><span class="chip {tone}">{_text(status)}</span><div class="main">'
            f'<div class="title">{_text(title)}</div><div class="sub">{_text(subtitle)}</div></div></div>')


def _task(task):
    status = "已回報待核對" if task["status"] == "done" else task["status"]
    worker = "運行中" if task["worker_alive"] is True else "未觀測到程序" if task["worker_alive"] is False else "未知／回合間"
    owner = {"alive": "運行中", "dead": "未運行", "unknown": "未知"}.get(task["owner_health"]["state"], "未知")
    relation = "目標關鍵" if task["goal_critical"] else "非關鍵" if task["goal_critical"] == 0 else "關鍵性未標記"
    subtitle = (f"{task['task_id']} · {task['model']} · worker {worker} · Orch {owner} · "
                f"{task['goal_id'] or 'Later'} · {relation} · 待送 {task['pending']} · {task.get('note') or ''}")
    return _row(task["title"], subtitle, status, task["status"] in ("blocked", "failed"))


def _question(question):
    return _row(question["body"], f"問題 {question['id']} · task={question['task_id'] or '未掛任務'} · "
                f"{question['state']} · delivery={question['delivery']}", "等 Operator", True)


def _goal(goal):
    pg = f'<p class="small-goals">PG · {_text(goal["pg"] or "待確認")}</p>'
    sprint = goal["sprint_goal"] or ("持續型：預算＋backlog" if goal["type"] == "continuous" else "Sprint Goal 待確認")
    body = (pg + f'<div class="sprint"><div class="k">SPRINT GOAL · {_text(goal["status"])}</div>'
            f'<h2>{_text(sprint)}</h2><div class="sub">{_text(goal["sprint_start"])} → {_text(goal["sprint_end"])}</div>'
            f'<p>完成條件：{_text(goal["done_when"] or "待確認")}</p>'
            f'<p>證據：{_text(goal["evidence"] or "尚無")}<br>判定權：{_text(goal["authority"] or "待確認")}</p>'
            f'<div class="plan">怎麼達成（Orch 紀錄）：{_text(goal["plan"] or "待擬")}</div>'
            f'<p class="sub">意圖：{_text(goal["intent"])}<br>球在：{_text(goal["ball"])} · '
            f'追蹤日：{_text(goal["follow_up_date"])}<br>來源：{_text(goal["source"])} · '
            f'最後確認：{_text(goal["last_confirmed_date"])} · 公司標籤：{_text(", ".join(goal["companies"]))}</p></div>')
    if goal["blocker"]:
        body += _details("Blocked", _row(goal["blocker"], goal["ball"], "blocked", True), True)
    priority = goal["priority"]
    label = "未定（V 待 Operator 設定）" if priority["score"] is None else f"{priority['score']:.2f}"
    body += _details("優先分數 " + label, '<p class="sub">' + _text(
        f"(V={priority['V']} + T={priority['T']} + S={priority['S']} + B={priority['B']}) / J={priority['J']}；權重 1，10/20 校準") + '</p>')
    return _details(f"{goal['id']} · Sprint Goal", body, goal["status"] not in ("paused", "done"), "goal")


def render(data):
    tabs, panels = [], []
    for i, project in enumerate(data["projects"]):
        repo, selected = project["repo"], i == 0
        score = "未定" if project["score"] is None else f"{project['score']:.2f}"
        tabs.append(f'<button type="button" role="tab" id="tab-{i}" aria-controls="panel-{i}" '
                    f'aria-selected="{str(selected).lower()}" tabindex="{0 if selected else -1}">'
                    f'<b>{_text(repo)}</b><span class="sub">{_text(score)}</span></button>')
        body = "".join(_goal(g) for g in project["goals"] if g["status"] in ("active", "waiting"))
        if not project["goals"]:
            body += '<p class="empty">尚無目標紀錄；未掛目標的任務列於 Later。</p>'
        critical_q = [q for q in project["questions"] if q["goal_critical"]]
        body += _details(f"要你決定 · 目標關鍵 ({len(critical_q)})", '<div class="list">' +
                         ("".join(_question(q) for q in critical_q) or '<p class="empty">目前沒有待決問題。</p>') + '</div>', True)
        critical_tasks = [t for t in project["tasks"] if t["goal_critical"] and t["status"] != "done"]
        blocked = [t for t in project["tasks"] if t["status"] in ("blocked", "failed")]
        body += _details(f"Blocked ({len(blocked)})", '<div class="list">' + "".join(_task(t) for t in blocked) + '</div>', True)
        body += _details("目標關鍵工作", '<div class="list">' + "".join(_task(t) for t in critical_tasks if t not in blocked) + '</div>', True)
        secondary_q = [q for q in project["questions"] if not q["goal_critical"]]
        records = [t for t in project["tasks"] if not t["goal_critical"] and t["goal_id"] and t["status"] != "done" and t not in blocked]
        later = [t for t in project["tasks"] if not t["goal_id"] and t["status"] != "done" and t not in blocked]
        body += _details("次要佇列 · 非目標關鍵", '<div class="list">' + "".join(_question(q) for q in secondary_q) + '</div>')
        body += _details("可追蹤紀錄", '<div class="list">' + "".join(_task(t) for t in records) + '</div>')
        body += _details(f"Later · 未掛目標 ({len(later)})", '<div class="list">' + "".join(_task(t) for t in later) + '</div>')
        done = [t for t in project["tasks"] if t["status"] == "done"]
        body += _details("Done · 已回報待核對", '<div class="list">' + "".join(_task(t) for t in done) + '</div>' +
                         "".join(_goal(g) for g in project["goals"] if g["status"] == "done"))
        body += "".join(_goal(g) for g in project["goals"] if g["status"] == "paused")
        if project["paused"]:
            body = _details("暫停專案", body)
        panels.append(f'<section role="tabpanel" id="panel-{i}" aria-labelledby="tab-{i}"'
                      f'{"" if selected else " hidden"}>{body}</section>')
    empty = '<p class="empty">尚無目標或未結任務。用 orchd goal add 建立中央目標。</p>' if not panels else ""
    return ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Orch 任務看板</title><style>' + CSS + '</style></head><body><main class="wrap">'
            '<header><h1>Orch 任務看板</h1><span class="meta">快照 ' + _text(data["generated_at"]) + '</span></header>'
            '<p class="notice">中央 orchd 靜態快照。此頁不自動更新；worker done 是已回報待核對，並非驗收。</p>'
            '<nav class="tabs" role="tablist" aria-label="專案，依優先分數排序">' + "".join(tabs) + '</nav>' +
            empty + "".join(panels) + '<footer>來源：goals、list_open、已送往 Operator 的待決問題。未消費 inbox；無對外資料請求。</footer>'
            '</main><script>' + JS + '</script></body></html>')


# v1 palette, typography, compact single-column rows; no remotely hosted fonts or assets.
CSS = """
:root{--bg:#f5f6f4;--panel:#ffffff;--fg:#1c2421;--muted:#5d6a65;--line:#dde3e0;
--accent:#2f6f62;--accent-soft:#e3efeb;--crit:#b4342c;--crit-soft:#f8e1df;--run:#2f5fa8;--run-soft:#e2eaf7}
@media(prefers-color-scheme:dark){:root{--bg:#121715;--panel:#1a211e;--fg:#e4ebe8;--muted:#97a6a0;
--line:#2b3531;--accent:#6cc0ab;--accent-soft:#1f3530;--crit:#f07c73;--crit-soft:#3c1f1d;--run:#7fa8ef;--run-soft:#1d2a40;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 system-ui,-apple-system,"PingFang TC",sans-serif}
.wrap{max-width:980px;margin:auto;padding:20px 16px 48px;display:grid;gap:22px}
header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px 16px;align-items:baseline}
h1{font-size:20px;margin:0}h2{font-size:18px;margin:6px 0}p{margin:8px 0;max-width:75ch;white-space:pre-wrap;overflow-wrap:anywhere}
.meta,.sub,.small-goals,footer{color:var(--muted);font-size:12.5px}.notice,.plan{background:var(--accent-soft);border-radius:8px;padding:8px 12px}
.tabs{display:flex;gap:6px;overflow-x:auto;padding:3px}button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--fg);border-radius:8px;padding:8px 12px;cursor:pointer;display:grid;gap:2px;flex-shrink:0}
button[aria-selected=true]{border-color:var(--accent);background:var(--accent-soft)}button:hover{border-color:var(--accent)}
button:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
section{display:grid;gap:14px}section[hidden]{display:none}details{min-width:0}summary{cursor:pointer;font-weight:600;padding:6px 0}
.sprint,.list{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}.sprint{padding:14px 16px}
.k{font-size:11px;color:var(--muted);letter-spacing:.06em}.row{display:grid;grid-template-columns:auto minmax(0,1fr);gap:4px 12px;padding:10px 14px;border-top:1px solid var(--line);align-items:start}.row:first-child{border-top:0}
.title{font-weight:500;white-space:pre-wrap;overflow-wrap:anywhere}.main{min-width:0}.chip{font-size:11.5px;padding:1px 8px;border-radius:999px;white-space:nowrap}.c-crit{background:var(--crit-soft);color:var(--crit)}.c-run{background:var(--run-soft);color:var(--run)}
.empty{padding:8px 12px;color:var(--muted)}@media(max-width:520px){.row{grid-template-columns:1fr}.chip{justify-self:start}}
"""

JS = """
const tabs = [...document.querySelectorAll('[role=tab]')];
function select(tab, focus=false){
  tabs.forEach(t=>{const active=t===tab;t.setAttribute('aria-selected',String(active));
    t.tabIndex=active?0:-1;document.getElementById(t.getAttribute('aria-controls')).hidden=!active;});
  if(focus) tab.focus();
}
tabs.forEach((tab,i)=>{tab.addEventListener('click',()=>select(tab));tab.addEventListener('keydown',e=>{
  let index;if(e.key==='ArrowRight')index=(i+1)%tabs.length;
  if(e.key==='ArrowLeft')index=(i-1+tabs.length)%tabs.length;
  if(e.key==='Home')index=0;if(e.key==='End')index=tabs.length-1;
  if(index!==undefined){e.preventDefault();select(tabs[index],true);}
});});
"""
