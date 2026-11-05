"""Read-only GitHub pull request review inventory."""
import json
import re
import subprocess


def _get(endpoint):
    result = subprocess.run(['gh', 'api', '--paginate', '--slurp', endpoint],
                            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
    pages = json.loads(result.stdout)
    return [item for page in pages for item in page] if pages and isinstance(pages[0], list) else pages[0]


def watch(repos):
    output = []
    for repo in repos:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError(f'invalid repository: {repo}')
        for pr in _get(f'repos/{repo}/pulls?state=open&per_page=100'):
            branch = pr['head']['ref']
            if not (branch.startswith('orchd/') or any(label['name'] == 'ai-agent' for label in pr['labels'])):
                continue
            number = pr['number']
            root = f'repos/{repo}'
            sha = pr['head']['sha']
            commit = _get(f'{root}/commits/{sha}')
            head_time = commit['commit']['committer']['date']
            owner = pr['user']['login']
            reviews = _get(f'{root}/pulls/{number}/reviews?per_page=100')
            inline = _get(f'{root}/pulls/{number}/comments?per_page=100')
            issue = _get(f'{root}/issues/{number}/comments?per_page=100')
            latest = {}
            for review in sorted(reviews, key=lambda r: r.get('submitted_at') or ''):
                if review.get('submitted_at') and review['state'] in ('APPROVED', 'COMMENTED', 'CHANGES_REQUESTED', 'DISMISSED'):
                    latest[review['user']['login']] = {'state': review['state'], 'time': review['submitted_at'], 'url': review['html_url']}
            latest = {name: review for name, review in latest.items() if review['state'] != 'DISMISSED'}
            comments = [dict(kind=kind, id=c['id'], author=c['user']['login'], time=c['created_at'],
                             body=c['body'], url=c['html_url'], reply_to=c.get('in_reply_to_id'))
                        for kind, rows in [('inline', inline), ('issue', issue)] for c in rows]
            # A reply to an inline thread is local to that thread; issue comments have no threads.
            def thread(c):
                by_id = {x['id']: x for x in comments if x['kind'] == 'inline'}
                while c.get('reply_to') in by_id:
                    c = by_id[c['reply_to']]
                return c['id']
            new = []
            for c in comments:
                if c['author'].lower() == owner.lower():
                    continue
                replies = [x['time'] for x in comments if x['author'].lower() == owner.lower() and x['kind'] == c['kind']
                           and (c['kind'] == 'issue' or thread(x) == thread(c))]
                if c['time'] > max([''] + replies):
                    new.append(c)
            for review in reviews:
                if (review.get('submitted_at') and review.get('body') and review['user']['login'].lower() != owner.lower()
                        and review['submitted_at'] > max([''] + [c['time'] for c in comments if c['kind'] == 'issue' and c['author'].lower() == owner.lower()])):
                    new.append(dict(kind='review', author=review['user']['login'], time=review['submitted_at'],
                                    body=review['body'], url=review['html_url']))
            requested = [name for name, review in latest.items() if review['state'] == 'CHANGES_REQUESTED']
            action = ('Address requested changes and unanswered comments' if requested and new else
                      'Address requested changes' if requested else 'Review and reply to unanswered comments' if new else
                      'Check review and CI status')
            match = re.fullmatch(r'orchd/([0-9a-f]{8})', branch)
            output.append(dict(repo=repo, number=number, title=pr['title'], url=pr['html_url'], owner=owner,
                               task_id=match.group(1) if match else None, head_sha=sha, head_commit_time=head_time, timestamp_note="Last push time is unavailable; unanswered candidates include comments before the last push. Issue replies are inferred from any later author comment; verify manually.",
                               reviews=latest, unanswered=new, suggested_action=action))
    return output
