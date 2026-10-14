"""Dispatch policy over the shared quota snapshot; never collects quota."""
import json
import re
import time

from . import quota

COOLDOWN = 600
MAX_BATCH = 4


def limited(error):
    text = json.dumps(error) if not isinstance(error, str) else error
    return bool(re.search(r'\b429\b|rate[ _-]?limit|usage[ _-]?limit|quota[ _-]?(?:exceeded|exhausted)',
                          text, re.I))


def select(con, *, backend=None, task_type=None, author_model=None, now=None):
    now = time.time() if now is None else now
    recent = set()
    for row in con.execute("SELECT body FROM messages WHERE kind='rate_limit' AND created_at>=?", (now - COOLDOWN,)):
        recent.add(json.loads(row['body'])['provider'])
    return quota.dispatch_shadow(backend=backend or 'exec', task_type=task_type, author_model=author_model,
                                 now=now, applied=True, recent_limited=recent)
