#!/usr/bin/env python3
"""Isolated dispatch smoke: real Git worktrees/processes, fixture provider adapters.

No model inference, installed service access, network, or production state writes.
Run with the repository's dependencies available: python3 scripts/routing-smoke.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchd import core, mcp_server, quota, store
from orchd.runtime import Runtime


class ProcessRuntime(Runtime):
    def __init__(self, projects):
        self.projects = projects
        self.codex = sys.executable
        self.children = []
        self.helpers = []

    def base_ref(self, repo_path):
        return 'HEAD'  # isolated fixture has no remote

    def claude_trusted(self, repo_path):
        return True  # only the fixture subprocess, never Claude

    def worker(self, worktree, model):
        # Child announces readiness, then waits. A second
        # dispatch returns while the first remains alive, proving overlap.
        child = subprocess.Popen([sys.executable, '-u', '-c',
            'import json,os,time; print(json.dumps({"pid":os.getpid(),"cwd":os.getcwd()})); time.sleep(300)',
            'codex-fixture', worktree],
            cwd=worktree, env=dict(os.environ, ORCHD_WORKTREE=worktree),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, start_new_session=True)
        self.children.append(child)
        ready = json.loads(child.stdout.readline())
        assert ready['pid'] == child.pid and Path(ready['cwd']).samefile(worktree)
        return str(child.pid), f'fixture-{child.pid}'

    def start_worker(self, worktree, sock, brief, model):
        return self.worker(worktree, model)

    def start_codex_worker(self, worktree, log, prompt, model):
        return self.worker(worktree, model)

    def send_uds(self, *args):
        pass  # provider message transport fixture

    def wake_orch(self, *args):
        pass  # no production Orch notification

    def codex_usage(self, *args):
        return None

    claude_usage = codex_usage

    def start_auto_retry(self, *args):
        pid = super().start_auto_retry(*args)
        self.helpers.append(pid)
        return pid

    def live_jobs(self):
        return {str(c.pid): {'pid': c.pid, 'status': 'busy'} for c in self.children if c.poll() is None}

    def socket_path(self, task_id):
        return str(store.home() / f'{task_id}.sock')

    def codex_log(self, task_id):
        return str(store.home() / f'{task_id}.jsonl')

    def cleanup(self):
        for child in self.children:
            child.stdin.close()
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=5)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                child.stdout.close()


def main():
    with tempfile.TemporaryDirectory(prefix='orchd-routing-') as tmp:
        root = Path(tmp)
        os.environ.update(ORCHD_HOME=str(root/'state'), ORCHD_PROJECTS=str(root/'projects'),
                          ORCHD_CONFIG_DIR=str(root/'config'),
                          ORCHD_EXECUTABLE=str(Path(__file__).resolve()))
        repo = root/'projects'/'synthetic'
        repo.mkdir(parents=True)
        for args in (['init', '-q', str(repo)], ['-C', str(repo), '-c', 'user.name=Synthetic',
                     '-c', 'user.email=synthetic@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'fixture']):
            subprocess.run(['git', *args], check=True, capture_output=True)
        con = store.connect()
        rt = ProcessRuntime(root/'projects')
        args = dict(repo='synthetic', title='Synthetic', instructions='Synthetic idle fixture',
                    done_when='Process ready', model='auto', backend='exec',
                    model_reason='independent smoke', task_type='investigation')
        try:
            now = time.time()
            quota.Cache().codex({'rateLimits': {'primary': {'usedPercent': 100,
                'windowDurationMins': 300, 'resetsAt': now+18000}}}, now=now)
            quota.Cache().claude({'rate_limits': {name: {'used_percentage': 20, 'resets_at': now+18000}
                                                 for name in ('five_hour', 'seven_day')}}, now=now)
            full = mcp_server.call('dispatch', args, 'smoke', con, rt)
            assert full['model'] == core.WORKER_MODELS['sonnet']
            assert full['quota_shadow']['applied'] is True
            print(json.dumps(dict(case='a: sol full', model=full['model'], applied=full['quota_shadow']['applied'], reason=full['model_reason'])))
            quota.Cache().path.unlink()
            unknown = mcp_server.call('dispatch', args, 'smoke', con, rt)
            assert unknown['model'] == core.WORKER_MODELS['terra']
            assert 'fallback' in unknown['model_reason']
            print(json.dumps(dict(case='b: quota unknown', model=unknown['model'], reason=unknown['model_reason'])))
            batch = mcp_server.call('dispatch_batch', {'tasks': [args, dict(args, title='Second')]}, 'smoke', con, rt)
            tasks = [store.get_task(con, t['task_id']) for t in batch['tasks']]
            pids = [int(t['job_id']) for t in tasks]
            assert len(set(pids)) == 2 and all(rt.pid_alive(p) for p in pids)
            assert len({t['worktree'] for t in tasks}) == 2
            grouped = mcp_server.call('list_open', {'group_id': batch['group_id']}, 'smoke', con, rt)
            assert len(grouped) == 2 and all(t['worker_alive'] for t in grouped)
            print(json.dumps(dict(case='c: batch parallel', group_id=batch['group_id'], pids=pids,
                simultaneous_alive=True, distinct_git_worktrees=True, grouped_tasks=len(grouped))))
            failing = tasks[0]
            log = root/'failure.jsonl'
            log.write_text(json.dumps({'type':'turn.failed', 'error':{'message':"You’ve hit your usage limit. Please try again later."}})+'\n')
            core.auto_flush(con, rt, failing['id'], int(failing['job_id']), turn_log=log)
            assert len(rt.helpers) == 1
            psutil.Process(rt.helpers[0]).wait(timeout=15)
            replaced = store.get_task(con, failing['id'])
            assert replaced['model'] == core.WORKER_MODELS['sonnet'] and replaced['auto_retry_count'] == 1
            assert replaced['worktree'] == failing['worktree'] and rt.pid_alive(replaced['job_id'])
            print(json.dumps(dict(case='d: automatic rate-limit retry', model=replaced['model'],
                auto_retry_count=replaced['auto_retry_count'], same_worktree=True, replacement_alive=True)))
            print('PASS: isolated state, real child processes and Git worktrees; provider adapters are fixtures, no live LLM inference')
        finally:
            rt.cleanup()
            for task in store.open_tasks(con):
                if task['job_id'] and str(task['job_id']).isdigit():
                    try:
                        proc = psutil.Process(int(task['job_id']))
                        if proc.environ().get('ORCHD_WORKTREE') == task['worktree']:
                            proc.terminate()
                            proc.wait(timeout=3)
                    except psutil.NoSuchProcess:
                        pass
            con.close()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '_auto-retry':
        # Execute the real checkout CLI/core/helper with fixture provider launch
        # adapters; all stop/lock/identity/count logic remains real.
        from orchd.cli import main as cli_main
        with patch('orchd.cli.Runtime', return_value=ProcessRuntime(Path(os.environ['ORCHD_PROJECTS']))):
            sys.exit(cli_main(sys.argv[1:]))
    else:
        main()
