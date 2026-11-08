"""Read-only GitHub pull request review inventory."""
import json
import re
import subprocess


def _get(endpoint):
    result = subprocess.run(['gh', 'api', '--paginate', '--slurp', endpoint],
                            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
    pages = json.loads(result.stdout)
    return [item for page in pages for item in page] if pages and isinstance(pages[0], list) else pages[0]


def watch(repos, task_branches=None, accounts=()):
    task_branches = task_branches or {}
    output = []
    for repo in repos:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
            raise ValueError(f'invalid repository: {repo}')
        for pr in _get(f'repos/{repo}/pulls?state=open&per_page=100'):
            branch = pr['head']['ref']
            head_repo = (pr['head'].get('repo') or {}).get('full_name', '').lower()
            task_id = task_branches.get((repo.lower(), branch)) if head_repo == repo.lower() else None
            if not (task_id or branch.startswith('orchd/') or any(label['name'] == 'ai-agent' for label in pr['labels'])):
                continue
            number = pr['number']
            root = f'repos/{repo}'
            sha = pr['head']['sha']
            commit = _get(f'{root}/commits/{sha}')
            head_time = commit['commit']['committer']['date']
            owner = pr['user']['login']
            ours = {owner.lower()} | {account.lower() for account in accounts}
            timeline = _get(f'{root}/issues/{number}/timeline?per_page=100')
            times = [event['committer']['date'] for event in timeline
                     if event.get('event') == 'committed' and event.get('sha') == sha
                     and event.get('committer') and event['committer'].get('date')]
            times += [event['created_at'] for event in timeline
                      if event.get('event') in ('head_ref_force_pushed', 'head_ref_restored')
                      and event.get('created_at')]
            cutoff = max(times, default=None)
            reviews = _get(f'{root}/pulls/{number}/reviews?per_page=100')
            inline = _get(f'{root}/pulls/{number}/comments?per_page=100')
            issue = _get(f'{root}/issues/{number}/comments?per_page=100')
            latest = {}
            for review in sorted(reviews, key=lambda r: r.get('submitted_at') or ''):
                name = review['user']['login']
                # Like GitHub's reviewDecision, a later comment does not replace an approval or change request.
                if review['state'] == 'COMMENTED' and latest.get(name, {}).get('state') in ('APPROVED', 'CHANGES_REQUESTED'):
                    continue
                if review.get('submitted_at') and review['state'] in ('APPROVED', 'COMMENTED', 'CHANGES_REQUESTED', 'DISMISSED'):
                    latest[name] = {'state': review['state'], 'time': review['submitted_at'], 'url': review['html_url']}
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
                if c['author'].lower() in ours:
                    continue
                replies = [x['time'] for x in comments if x['author'].lower() in ours and x['kind'] == c['kind']
                           and (c['kind'] == 'issue' or thread(x) == thread(c))]
                if c['time'] > max([cutoff or ''] + replies):
                    new.append(c)
            issue_replies = [c['time'] for c in comments if c['kind'] == 'issue' and c['author'].lower() in ours]
            for review in reviews:
                # A reviewer's later approval supersedes that reviewer's earlier review bodies.
                approvals = [r['submitted_at'] for r in reviews if r.get('submitted_at') and r['state'] == 'APPROVED'
                             and r['user']['login'] == review['user']['login']]
                if (review.get('submitted_at') and review.get('body') and review['user']['login'].lower() not in ours
                        and review['submitted_at'] > max([cutoff or ''] + issue_replies)
                        and review['submitted_at'] >= max(approvals, default='')):
                    new.append(dict(kind='review', author=review['user']['login'], time=review['submitted_at'],
                                    body=review['body'], url=review['html_url']))
            requested = [name for name, review in latest.items() if review['state'] == 'CHANGES_REQUESTED']
            action = ('Address requested changes and unanswered comments' if requested and new else
                      'Address requested changes' if requested else 'Review and reply to unanswered comments' if new else
                      'Check review and CI status')
            match = re.fullmatch(r'orchd/([0-9a-f]{8})', branch)
            output.append(dict(repo=repo, number=number, title=pr['title'], url=pr['html_url'], owner=owner,
                               task_id=task_id or (match.group(1) if match else None), head_sha=sha, head_commit_time=head_time, push_cutoff=cutoff, push_cutoff_uncertain=cutoff is None,
                               timestamp_note="The timeline cutoff is a lower bound, not necessarily the push time. Issue replies are inferred from later author comments; verify manually.",
                               reviews=latest, unanswered=new, suggested_action=action))
    return output
