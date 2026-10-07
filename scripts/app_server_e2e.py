#!/usr/bin/env python3
"""Manual real-Codex test: .venv/bin/python scripts/app_server_e2e.py

Requires codex 0.160.1+ and tmux. Private DB/git/CODEX_HOME/tmux; copies auth
only into a 0700 temporary home and deletes it in finally. Never uses live tasks.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchd import app_worker, core, store
from orchd.app_transport import RPC
from orchd.processes import task_processes
from orchd.runtime import Runtime


def wait(check, label, timeout=90):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=check()
        if result:
            return result
        time.sleep(.2)
    raise AssertionError('timeout: '+label)


def main():
    config=Path.home()/'.codex/config.toml'
    before=hashlib.sha256(config.read_bytes()).hexdigest() if config.exists() else None
    original=dict(os.environ)
    with tempfile.TemporaryDirectory(prefix='orchd-app-e2e-') as tmp:
        root=Path(tmp);repo=root/'repo';repo.mkdir();home=root/'codex';home.mkdir(mode=0o700)
        auth=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'auth.json'
        shutil.copyfile(auth,home/'auth.json');(home/'auth.json').chmod(0o600)
        os.environ.update(ORCHD_HOME=str(root/'state'),CODEX_HOME=str(home),
                          ORCHD_EXECUTABLE=str(Path(__file__).resolve().parents[1]/'bin/orchd'))
        (home/'config.toml').write_text('model_reasoning_effort = "low"\n')
        for cmd in [['git','init','-q'],['git','-c','user.name=E2E','-c','user.email=user@example.invalid',
                     'commit','--allow-empty','-qm','test']]:
            subprocess.run(cmd,cwd=repo,check=True)
        worktree=root/'worktree'
        subprocess.run(['git','worktree','add','-qb','isolated-e2e',str(worktree)],cwd=repo,check=True)
        con=store.connect();rt=Runtime();task_id='isolated-e2e'
        sock=str(root/'tmux.sock')
        def tmux(*args,check=True):
            return subprocess.run(['tmux','-S',sock,'-f','/dev/null',*args],capture_output=True,text=True,check=check)
        task=store.create_task(con,id=task_id,repo='repo',repo_path=str(repo),title='E2E',instructions='probe',
            done_when='probe',orch_thread='isolated',codex_bin=rt.codex,model='gpt-6.1-sol',
            worktree=str(worktree),base=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip())
        closed=False
        try:
            app_worker.start(con,rt,task,
                'Run the shell command sleep 20 now. After it completes reply E2E_DONE and include any '
                'additional marker requested by the user. Do not make any other tool calls.',task['model'])
            store.update_task(con,task_id,status='running')
            def events():
                return [json.loads(r[0]) for r in con.execute("SELECT body FROM messages WHERE kind='app_event'")]
            first=wait(lambda:next((e for e in events() if e['method']=='item/started' and
                     e['params'].get('item',{}).get('type')=='commandExecution'),None),'tool start')
            turn=first['params']['turnId'];task=store.get_task(con,task_id)
            print('PASS task app-server; active thread',task['session_id'],'turn',turn,flush=True)
            command=shlex.join([sys.executable,'-m','orchd.app_worker','viewer',
                         str(store.home()/'orchd.db'),task_id,task['generation']])
            env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
            subprocess.run(['tmux','-S',sock,'-f','/dev/null','new-session','-d','-s','viewer','-x','120','-y','35',command],
                           env=env,check=True)
            tmux_identity=app_worker.identity(int(tmux('display-message','-p','#{pid}').stdout))
            viewer_row=wait(lambda:con.execute('SELECT identity FROM worker_viewers').fetchone(),'viewer registration')
            viewer_identity=viewer_row['identity']
            wait(lambda:'Working' in tmux('capture-pane','-pt','viewer').stdout,'remote TUI attached mid-turn',20)
            tmux('send-keys','-t','viewer','-l','Include E2E_TUI_STEER in your reply after sleep.')
            time.sleep(1);tmux('send-keys','-t','viewer','Enter')
            queued=core.answer(con,rt,task_id,'Reply QUEUED_AUTOFLUSH_OK only; no tools.')
            assert queued['status']=='queued',queued
            print('PASS mid-turn native remote TUI attach; answer FIFO queued',flush=True)
            user=wait(lambda:next((e for e in events() if e['method']=='item/started' and
                e['params'].get('turnId')==turn and e['params'].get('item',{}).get('type')=='userMessage'
                ),None),'same-turn userMessage')
            # Event rows retain metadata only. Inspect content transiently through RPC.
            probe = RPC(task['endpoint'])
            try:
                def steer_seen():
                    history = probe.call('thread/turns/list', {'threadId':task['session_id'],
                        'limit':100, 'sortDirection':'asc', 'itemsView':'full'})['data']
                    return any(t['id']==turn and any('E2E_TUI_STEER' in json.dumps(i)
                        for i in t.get('items', [])) for t in history)
                wait(steer_seen, 'same-turn steer marker in history')
            finally:
                probe.close()
            print('PASS injected line seen as same-turn userMessage',user['params']['turnId'],flush=True)
            tmux('kill-session','-t','viewer')
            wait(lambda:not con.execute('SELECT 1 FROM worker_viewers').fetchone(),'viewer lease released')
            assert app_worker.health(store.get_task(con,task_id)) in ('active','idle','alive')
            print('PASS viewer closed; worker continues',flush=True)
            wait(lambda:store.pending_answer_count(con,task_id)==0,'queued answer flushed')
            wait(lambda:not store.get_task(con,task_id)['active_turn'],'second turn complete')
            count=con.execute("SELECT COUNT(*) FROM messages WHERE kind='answer'").fetchone()[0]
            assert count==1,count
            deliveries=con.execute("SELECT COUNT(*) FROM worker_deliveries WHERE state='delivered'").fetchone()[0]
            assert deliveries==2,deliveries
            print('PASS queued answer auto-flushed exactly once; deliveries=2 answers=1',flush=True)
            task=store.get_task(con,task_id)
            core.close(con,rt,task_id);closed=True
            assert app_worker.process(task['supervisor_identity']) is None
            assert app_worker.process(task['server_identity']) is None
            assert task_processes(str(worktree))=={}
            assert app_worker.process(viewer_identity) is None
            assert app_worker.process(tmux_identity) is None
            assert tmux('list-sessions',check=False).returncode!=0
            print('PASS close: no server/supervisor/viewer/tmux/marked child processes',flush=True)
        finally:
            tmux('kill-server',check=False)
            if not closed:
                current=store.get_task(con,task_id)
                if app_worker.enabled(current):app_worker.stop(con,current)
            con.close()
            os.environ.clear();os.environ.update(original)
    after=hashlib.sha256(config.read_bytes()).hexdigest() if config.exists() else None
    assert before==after,'global Codex config changed'
    print('PASS isolated DB/CODEX_HOME; global config unchanged; no live orchd tasks accessed',flush=True)


if __name__=='__main__':
    main()
