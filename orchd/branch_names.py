"""Stable names for opt-in remote branches."""
import re
import subprocess
from urllib.parse import urlsplit


def choose(title, issue_ref, task_type, task_id, occupied):
    kind = re.match(r'^(fix|feat|docs|chore)(?:\([^)]*\))?:', title, re.IGNORECASE)
    prefix = issue_ref[5:] if issue_ref and issue_ref.startswith('jira:') else (
        kind.group(1).lower() if kind else {'code': 'fix', 'docs': 'docs'}.get(task_type, 'chore'))
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') or task_id
    def build(suffix=''):
        available = 60 - len(prefix) - len(suffix) - 1
        if available < 1:
            raise ValueError('issue key is too long for a readable branch')
        return prefix + '-' + slug[:available].rstrip('-') + suffix
    candidate = build()
    if candidate in occupied:
        candidate = build('-' + task_id)
        if candidate in occupied:
            raise ValueError(f'remote branch {candidate} already exists; dispatch refused')
    return candidate


def remote_refs(repo_path):
    result = subprocess.run(['git', '-C', str(repo_path), 'ls-remote', '--heads', 'origin'],
                            capture_output=True, text=True, check=True, timeout=60)
    return {line.split('\t', 1)[1].removeprefix('refs/heads/') for line in result.stdout.splitlines()}


def github_origin(url):
    url = url.strip()
    if '://' in url:
        return (urlsplit(url).hostname or '').lower() == 'github.com'
    return bool(re.match(r'^(?:[^@/:]+@)?github\.com:', url, re.IGNORECASE))
