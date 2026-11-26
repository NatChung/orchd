"""Project a read-only orchd snapshot into the ORCH Dash Pages payload."""
import re


STATES = {"current", "queued", "snoozed"}
OPTION = re.compile(r"^([A-Z])[.)、：:]\s+(.+)$")
SUMMARY_OPTION = re.compile(r"^([A-Z])[.)、：:\s]\s*(.+)$")
SUMMARY_HEADER = "【給 Operator 的摘要】"
DETAILS = "---詳細---"


def preview(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…（見 Interface 原文）"


def summary_text(lines):
    end = next((index for index, line in enumerate(lines) if line == DETAILS), None)
    if not lines or lines[0] != SUMMARY_HEADER or end is None or end < 2:
        return None
    summary = [line.strip() for line in lines[1:end] if line.strip()]
    choices = next((line for line in summary if line.startswith(("選項：", "選項:"))), None)
    options = []
    if choices:
        tokens = re.split(r"[／/]\s*(?=[A-Z][.)、：:\s])", choices[3:].strip())
        matches = [SUMMARY_OPTION.fullmatch(token.strip()) for token in tokens]
        if 2 <= len(matches) <= 6 and all(matches):
            labels = {match[1] for match in matches}
            if len(labels) == len(matches):
                options = [[match[1], preview(match[2], 90)] for match in matches]
                summary.remove(choices)
    return preview("\n".join(summary), 300), options


def question_text(body):
    lines = body.strip().splitlines()
    summary = summary_text(lines)
    if summary is not None:
        return summary
    first = next((i for i, line in enumerate(lines) if OPTION.fullmatch(line)), len(lines))
    options = [OPTION.fullmatch(line) for line in lines[first:] if line.strip()]
    if first and 2 <= len(options) <= 6 and all(options):
        return preview("\n".join(lines[:first]).rstrip(), 300), [[m[1], preview(m[2], 90)] for m in options]
    return preview(body.strip(), 300), []


def project(snapshot, *, project_names=None):
    if snapshot.get("schema_version") != 2 or not isinstance(snapshot.get("entry_messages"), list):
        raise ValueError("Expected an orchd version 2 snapshot")
    project_names = project_names or {}
    groups = {}
    for question in snapshot["entry_messages"]:
        if (question["entry_id"] != "desktop" or question["kind"] != "question"
                or question["binding_current"] is not True or question["question_state"] not in STATES
                or question.get("superseded_by") is not None):
            continue
        task = question.get("task") or {}
        key = question.get("project") or task.get("repo") or "unassigned"
        group = groups.setdefault(key, {
            "id": key, "name": project_names.get(key, "未分類" if key == "unassigned" else key),
            "cards": [],
        })
        state = question["question_state"]
        ready = state == "current" and question["delivery"] == "delivered"
        title, options = question_text(question["body"])
        group["cards"].append({
            "id": question["id"], "ready": ready, "title": title, "options": options,
            "status": "可回答" if ready else {"current": "等待送達", "queued": "排隊中", "snoozed": "稍後"}[state],
        })

    for group in groups.values():
        group["cards"].sort(key=lambda card: (not card["ready"], card["id"]))
    projects = sorted(groups.values(), key=lambda group: (not group["cards"][0]["ready"], group["cards"][0]["id"]))
    return {"schema": 1, "projects": projects}
