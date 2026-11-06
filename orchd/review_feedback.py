"""Fetch the complete review conversation for a pull request."""
import datetime
import json
import os
import re
import subprocess

def _rest(endpoint, repo):
    result = subprocess.run(['gh', 'api', '--paginate', '--slurp', endpoint],
                            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
                            env={**os.environ, 'GH_REPO': repo})
    pages = json.loads(result.stdout)
    return [item for page in pages for item in page] if pages and isinstance(pages[0], list) else pages[0]

_QUERY = '''query($owner:String!, $repo:String!, $number:Int!, $endCursor:String) {
  repository(owner:$owner, name:$repo) { pullRequest(number:$number) {
    reviewThreads(first:100, after:$endCursor) { nodes { id isResolved
      comments(first:100) { nodes { databaseId } pageInfo { hasNextPage endCursor } }
    } pageInfo { hasNextPage endCursor } }
  } }
}'''


def _threads(repo, number):
    owner, name = repo.split('/')
    result = subprocess.run(['gh', 'api', 'graphql', '--paginate', '--slurp', '-f', 'query=' + _QUERY,
                             '-f', 'owner=' + owner, '-f', 'repo=' + name, '-F', 'number=' + str(number)],
                            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
                            env={**os.environ, 'GH_REPO': repo})
    threads = []
    for page in json.loads(result.stdout):
        if page.get('errors'):
            raise ValueError(str(page['errors']))
        rows = page['data']['repository']['pullRequest']['reviewThreads']['nodes']
        for row in rows:
            info = row['comments']['pageInfo']
            while info['hasNextPage']:
                query = '''query($id:ID!, $cursor:String!) { node(id:$id) { ... on PullRequestReviewThread {
                  comments(first:100, after:$cursor) { nodes { databaseId } pageInfo { hasNextPage endCursor } }
                } } }'''
                more = subprocess.run(['gh', 'api', 'graphql', '-f', 'query=' + query,
                                       '-f', 'id=' + row['id'], '-f', 'cursor=' + info['endCursor']],
                                      capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
                                      env={**os.environ, 'GH_REPO': repo})
                page = json.loads(more.stdout)
                if page.get('errors'):
                    raise ValueError(str(page['errors']))
                comments = page['data']['node']['comments']
                row['comments']['nodes'].extend(comments['nodes'])
                info = comments['pageInfo']
        threads.extend(rows)
    return threads


def fetch(repo, number, since=None, hide_bots=False):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) or int(number) < 1:
        raise ValueError('invalid repository or pull request number')
    cutoff = None
    if since:
        cutoff = datetime.datetime.fromisoformat(since.replace('Z', '+00:00'))
        if cutoff.tzinfo is None:
            raise ValueError('--since must include a timezone')
    root = f'repos/{repo}'
    pr = _rest(f'{root}/pulls/{number}', repo)
    account = _rest('user', repo)['login'].lower()
    reviews = _rest(f'{root}/pulls/{number}/reviews?per_page=100', repo)
    inline = _rest(f'{root}/pulls/{number}/comments?per_page=100', repo)
    issue = _rest(f'{root}/issues/{number}/comments?per_page=100', repo)
    threads = _threads(repo, number)
    resolution = {c['databaseId']: t['isResolved'] for t in threads for c in t['comments']['nodes']}
    items = []
    for kind, rows in [('review', reviews), ('inline', inline), ('issue', issue)]:
        for row in rows:
            user = row.get('user') or {}
            items.append(dict(kind=kind, id=row['id'], author=user.get('login'), bot=user.get('type') == 'Bot',
                              time=row.get('submitted_at') if kind == 'review' else row.get('created_at'),
                              body=row.get('body'), url=row.get('html_url'), state=row.get('state'),
                              commit_id=row.get('commit_id'), path=row.get('path'), line=row.get('line'),
                              in_reply_to=row.get('in_reply_to_id'), resolved=resolution.get(row['id']) if kind == 'inline' else None))
    by_id = {x['id']: x for x in items if x['kind'] == 'inline'}
    def thread(item):
        seen = set()
        while item.get('in_reply_to') in by_id and item['id'] not in seen:
            seen.add(item['id'])
            item = by_id[item['in_reply_to']]
        return item['id']
    owner = pr['user']['login'].lower()
    for item in items:
        responder = (item['author'] or '').lower() in (owner, account)
        replies = [x for x in items if x['time'] and item['time'] and x['time'] > item['time']
                   and (x['author'] or '').lower() in (owner, account)
                   and ((item['kind'] == 'inline' and x['kind'] == 'inline' and thread(x) == thread(item))
                        or (item['kind'] != 'inline' and x['kind'] == 'issue'))]
        item['unanswered'] = bool(not responder and item['body'] and not replies)
    items.sort(key=lambda x: x['time'] or '')
    return [x for x in items if (not hide_bots or not x['bot']) and
            (cutoff is None or (x['time'] and datetime.datetime.fromisoformat(x['time'].replace('Z', '+00:00')) >= cutoff))]


def render(items):
    return '\n\n'.join(f"{x['time'] or 'unsubmitted'} {x['kind']} by {x['author'] or 'unknown'}"
                       f"{' [unanswered]' if x['unanswered'] else ''}"
                       f"{(' [' + x['state'] + ']') if x['state'] else ''}"
                       f"{(' commit=' + x['commit_id']) if x['commit_id'] else ''}"
                       f"{(' ' + x['path'] + ':' + str(x['line'])) if x['path'] else ''}"
                       f"{(' reply-to=' + str(x['in_reply_to'])) if x['in_reply_to'] else ''}"
                       f"{(' [resolved]' if x['resolved'] else ' [unresolved]') if x['resolved'] is not None else ''}"
                       f"\n{x['body'] or ''}\n{x['url'] or ''}" for x in items)
