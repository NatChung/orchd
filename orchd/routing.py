"""Dispatch policy over the shared quota snapshot; refreshes expired Claude telemetry before auto selection."""
import json
import re
import time

from . import quota

COOLDOWN = 600
MAX_BATCH = 4


def limited(error):
    """Classify a provider error payload, never serialized events/tool output.

    Callers must extract errors from Codex failed turns or JSON-RPC rejections.
    Only known error fields are inspected; arbitrary nested text is not evidence.
    """
    codes = {'ratelimit', 'ratelimitexceeded', 'usagelimitexceeded'}

    def code(value):
        return isinstance(value, str) and re.sub(r'[_-]', '', value).lower() in codes

    if isinstance(error, dict):
        # Full item events (including command output) are not error payloads.
        if 'item' in error or 'method' in error or str(error.get('type', '')).startswith(('item.', 'turn.')):
            return False
        info = error.get('codexErrorInfo')
        if isinstance(info, str):
            return code(info)  # A definitive non-limit code overrides message text.
        if isinstance(info, dict):
            # CodexErrorInfo's externally tagged HTTP error variants.
            for variant in ('httpConnectionFailed', 'responseStreamConnectionFailed',
                            'responseStreamDisconnected', 'responseTooManyFailedAttempts'):
                detail = info.get(variant)
                if isinstance(detail, dict) and detail.get('httpStatusCode') == 429:
                    return True
            return False
        if any(code(error.get(key)) for key in ('code', 'type')):
            return True
        if any(error.get(key) in (429, '429') for key in ('httpStatusCode', 'status', 'status_code')):
            return True
        # JSON-RPC error.data can carry the provider error details.
        if isinstance(error.get('data'), dict) and limited(error['data']):
            return True
        error = error.get('message')
    if not isinstance(error, str):
        return False
    # Exec ThreadErrorEvent has only message. Anchor known provider wording so
    # gh logs, line numbers and quoted tool output cannot match incidentally.
    return bool(re.match(
        r"^(?:you['’]ve hit your usage limit\b|429 too many requests\b|"
        r"(?:http )?status(?: code)?[ :]+429\b|"
        r"unexpected status(?: code)?[ :]+429\b|"
        r"rate limit exceeded:|rate limit reached for .+ in organization\b)",
        error.strip(), re.I))


def select(con, *, backend=None, task_type=None, author_model=None, now=None):
    now = time.time() if now is None else now
    recent = set()
    for row in con.execute("SELECT body FROM messages WHERE kind='rate_limit' AND created_at>=?", (now - COOLDOWN,)):
        recent.add(json.loads(row['body'])['provider'])
    return quota.dispatch_shadow(backend=backend or 'exec', task_type=task_type, author_model=author_model,
                                 now=now, applied=True, recent_limited=recent)
