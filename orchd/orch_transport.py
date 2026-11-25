"""Context-bound Claude Orch delivery. Native exports are never worker transports."""
import hashlib
import json
import math
import os
import socket
import sqlite3
import stat
import struct
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from . import config, paths, store
from .orch_health import owner_health


class TransportUnavailable(OSError):
    """An alive or unknown controller lacks verified transport; never revive it."""


def _private_json(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise ValueError('unprotected transport configuration')
        raw = stream.read(limit + 1)
    if len(raw) > limit:raise ValueError('transport configuration exceeds limit')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _current(orch):
    for key in ('id', 'model', 'session_id', 'job_id'):
        value = orch[key]
        if not isinstance(value,str) or not value or any(char.isspace() for char in value):
            raise ValueError('controller identity is incomplete')
    if not isinstance(orch['socket'],str) or not Path(orch['socket']).is_absolute():
        raise ValueError('controller socket is not absolute')
    db = (store.home() / 'orchd.db').resolve(strict=True)
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute('SELECT * FROM orchs WHERE id=?', (orch['id'],)).fetchone()
        keys = ('id', 'kind', 'model', 'socket', 'session_id', 'job_id', 'stopped_at')
        if (row is None or any(row[k] != orch[k] for k in keys) or row['kind'] != 'claude'
                or row['stopped_at'] is not None):
            raise ValueError('controller identity changed')


def _process(pid, home):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:raise ValueError('invalid native PID')
    proc = psutil.Process(pid)
    if not proc.is_running() or proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
        raise ValueError('native process is not running')
    ids, birth = proc.uids(), proc.create_time()
    if (ids.real != os.getuid() or ids.effective != os.getuid() or
            not isinstance(birth, (int,float)) or isinstance(birth,bool) or not math.isfinite(birth) or birth <= 0 or
            Path(proc.cwd()).resolve() != home):raise ValueError('native process identity is unknown')
    return proc, birth


def _model(job, orch, home):
    if 'model' in job:
        if job['model'] != orch['model']:raise ValueError('native model changed')
        return
    # Native agents may omit model. Its protected job state retains current
    # restart flags even when vendor handoff loses our explicit socket flag.
    job_id = orch['job_id']
    if not isinstance(job_id,str) or Path(job_id).name != job_id or job_id in ('.','..'):
        raise ValueError('invalid native job identity')
    root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').resolve()
    state, _ = _private_json(root / 'jobs' / job_id / 'state.json')
    if state.get('sessionId') != orch['session_id'] or state.get('cwd') != str(home):
        raise ValueError('native state identity changed')
    flags = state.get('respawnFlags')
    if not isinstance(flags,list) or not all(isinstance(v,str) for v in flags):raise ValueError('invalid native flags')
    values = []
    for i, value in enumerate(flags):
        if value == '--model':values.append(flags[i+1] if i+1<len(flags) else None)
        elif value.startswith('--model='):values.append(value.split('=',1)[1])
    if values != [orch['model']]:raise ValueError('native model is not exact')


def _profile(env, orch, entry, *, child=False):
    allowed = set(paths.PROFILE_PATH_VARS) | {'ORCHD_ORCH_ID','ORCHD_PROJECTS'}
    if not child and (not isinstance(env,dict) or set(env) - allowed):
        raise ValueError('MCP environment has unknown policy keys')
    routing = {
        'ORCHD_HOME': store.home(), 'ORCHD_ROOT': paths.root(),
        'ORCHD_CONFIG_DIR': paths.config_dir(), 'ORCHD_CONFIG_FILE': config.path(),
        'ORCHD_PROJECTS': paths.projects_dir(), 'ORCHD_ORCH_HOME': paths.orch_home(),
        'ORCHD_INTERFACE_HOME': paths.interface_home(), 'ORCHD_CHANNEL_HOME': paths.channel_home(),
    }
    if env.get('ORCHD_ORCH_ID') != orch['id']:
        raise ValueError('MCP logical owner changed')
    for key,target in routing.items():
        if key in env:
            value=env[key]
            if not isinstance(value,str) or not Path(value).is_absolute() or Path(value).resolve()!=target.resolve():
                raise ValueError('MCP instance policy path changed')
    if 'ORCHD_EXECUTABLE' in env and env['ORCHD_EXECUTABLE'] != entry:
        raise ValueError('MCP executable does not match its protected entry point')
    # Native may inherit an older reviewed SDK prefix. It is not a routing
    # override and is never compared with the new sender's SDK prefix.
    if child and any(key.startswith('ORCHD_') and key not in allowed | {'ORCHD_PROFILE_PREFIX'} for key in env):
        raise ValueError('MCP receiver has unknown role or policy keys')


def _peer_pid(peer):
    if sys.platform == 'darwin':
        # Darwin sys/un.h defines SOL_LOCAL=0 and LOCAL_PEERPID=0x002.
        return struct.unpack('i',peer.getsockopt(0,0x002,4))[0]
    if sys.platform.startswith('linux') and hasattr(socket,'SO_PEERCRED'):
        pid, uid, _ = struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid != os.getuid():raise ValueError('foreign socket peer')
        return pid
    raise ValueError('native endpoint attestation is unsupported on this platform')


@dataclass(repr=False)
class Endpoint:
    path: str
    pid: int
    birth: float
    child_pid: int
    child_birth: float
    mcp_sha256: str
    orch: dict = field(repr=False)
    token: str = field(repr=False)
    inode: tuple = field(repr=False)
    argv: tuple = field(repr=False)
    env: dict = field(repr=False)

    def info(self):
        return dict(kind='native_exported',socket=self.path,pid=self.pid,process_birth=self.birth,
                    mcp_pid=self.child_pid,mcp_process_birth=self.child_birth,mcp_sha256=self.mcp_sha256,
                    job_id=self.orch['job_id'],session_id=self.orch['session_id'])

    def connect(self):
        _current(self.orch)
        home = paths.orch_home().resolve()
        native, birth = _process(self.pid,home)
        child, child_birth = _process(self.child_pid,home)
        if (birth != self.birth or child_birth != self.child_birth or child.ppid() != native.pid or
                tuple(child.cmdline()) != self.argv):
            raise ValueError('native process changed')
        _, digest = _private_json(Path(self.orch['socket']).parent / 'mcp.json')
        child_env = child.environ()
        _profile(child_env,self.orch,self.argv[1],child=True)
        if (child_env.get('CLAUDE_CODE_SESSION_ID') != self.orch['session_id'] or digest != self.mcp_sha256 or any(child_env.get(k) != v for k,v in self.env.items()) or
                any(key in child_env for key in ('ORCHD_TASK_ID','ORCHD_WORKTREE')) or
                child_env.get('CLAUDE_CODE_MESSAGING_SOCKET') != self.path or
                child_env.get('CLAUDE_CODE_MESSAGING_TOKEN') != self.token):
            raise ValueError('native MCP export changed')
        info = os.lstat(self.path)
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode)!=0o600 or (info.st_dev,info.st_ino)!=self.inode):
            raise ValueError('native endpoint changed')
        peer = socket.socket(socket.AF_UNIX)
        try:
            peer.settimeout(5);peer.connect(self.path)
            if _peer_pid(peer) != self.pid or native.create_time()!=self.birth:
                raise ValueError('native endpoint peer changed')
            _current(self.orch)
            return peer
        except BaseException:
            peer.close();raise


def discover(rt, orch):
    """Read fresh native evidence; never cache, write metadata or start a process."""
    try:
        if os.name=='nt':raise ValueError('native fallback is Unix only')
        _current(orch)
        jobs = rt.live_jobs()
        if owner_health(orch,jobs)['state'] != 'alive':raise ValueError('controller is not positively alive')
        job = jobs[orch['job_id']]
        home = paths.orch_home().resolve()
        cwd = job.get('cwd')
        if (job.get('id') != orch['job_id'] or job.get('sessionId') != orch['session_id'] or
                not isinstance(cwd,str) or not Path(cwd).is_absolute() or Path(cwd).resolve()!=home):
            raise ValueError('native controller identity changed')
        native, birth = _process(job['pid'],home)
        _model(job,orch,home)
        config, digest = _private_json(Path(orch['socket']).parent / 'mcp.json')
        mcp = config['mcpServers']['orchd']
        command,args,env = mcp['command'],mcp['args'],mcp['env']
        if (not isinstance(command,str) or not Path(command).is_absolute() or not Path(command).is_file() or
                not os.access(command,os.X_OK) or not isinstance(args,list) or len(args)!=2 or
                not isinstance(args[0],str) or not Path(args[0]).is_absolute() or not Path(args[0]).is_file() or
                args[1]!='mcp' or not isinstance(env,dict) or env.get('ORCHD_ORCH_ID')!=orch['id'] or
                not isinstance(env.get('ORCHD_HOME'),str) or not Path(env['ORCHD_HOME']).is_absolute() or
                Path(env['ORCHD_HOME']).resolve()!=store.home().resolve() or
                not isinstance(env.get('ORCHD_ROOT'),str) or not Path(env['ORCHD_ROOT']).is_absolute() or
                Path(env['ORCHD_ROOT']).resolve()!=paths.root().resolve()):
            raise ValueError('MCP profile is not exact')
        _profile(env,orch,args[0])
        children=[]
        for child in native.children():
            proc=psutil.Process(child.pid)
            if proc.ppid()==native.pid and proc.cmdline()==[command,*args]:children.append(proc)
        if len(children)!=1:raise ValueError('native MCP process is unknown or ambiguous')
        proc,child_birth=_process(children[0].pid,home)
        child_env=proc.environ()
        _profile(child_env,orch,args[0],child=True)
        if (child_env.get('CLAUDE_CODE_SESSION_ID') != orch['session_id'] or any(child_env.get(k)!=v for k,v in env.items()) or
                any(key in child_env for key in ('ORCHD_TASK_ID','ORCHD_WORKTREE'))):
            raise ValueError('native MCP profile or receiver role changed')
        address,token=child_env.get('CLAUDE_CODE_MESSAGING_SOCKET'),child_env.get('CLAUDE_CODE_MESSAGING_TOKEN')
        if not isinstance(address,str) or not Path(address).is_absolute() or not isinstance(token,str) or not token:
            raise ValueError('native MCP export is missing')
        leaf=os.lstat(address)
        if not stat.S_ISSOCK(leaf.st_mode) or leaf.st_uid!=os.getuid() or stat.S_IMODE(leaf.st_mode)!=0o600:
            raise ValueError('native MCP export is not a private socket')
        return Endpoint(address,native.pid,birth,proc.pid,child_birth,digest,dict(orch),token,(leaf.st_dev,leaf.st_ino),tuple([command,*args]),dict(env))
    except Exception:
        raise TransportUnavailable('Claude Orch transport is not positively verified') from None


def send(rt, orch, text):
    if orch is None or 'kind' not in orch.keys() or orch['kind'] != 'claude':
        raise TransportUnavailable('Only a Claude Orch may use this transport')
    try:
        rt.send_uds(orch['socket'],orch['session_id'],text)
        return None
    except (FileNotFoundError,ConnectionRefusedError):
        # Only fresh positive death permits the existing revive path. Missing
        # canonical transport alone is never proof that a live owner retired.
        jobs=rt.live_jobs()
        if owner_health(orch,jobs)['state']=='dead':raise
    endpoint=discover(rt,orch)
    try:
        peer=endpoint.connect()
    except Exception:
        raise TransportUnavailable('Claude Orch endpoint changed before delivery') from None
    frame={'type':'user','session_id':orch['session_id'],'from':'orchd','priority':'next',
           'message':{'role':'user','content':text}}
    data=(json.dumps({'type':'auth','token':endpoint.token})+'\n'+json.dumps(frame,ensure_ascii=False)+'\n').encode()
    with peer:
        try:peer.sendall(data)
        except OSError:
            from .runtime import UDSWriteUncertain
            raise UDSWriteUncertain('Claude Orch delivery is uncertain') from None
    # No native acknowledgement/UUID exists on this channel. Application
    # callbacks, not a completed socket write, establish consumption.
    return None


def info(rt, orch):
    if orch is None or 'kind' not in orch.keys() or orch['kind'] != 'claude':
        raise TransportUnavailable('Only a Claude Orch may use this transport')
    if rt.socket_listening(orch['socket']) is True:
        return dict(kind='configured',socket=orch['socket'])
    endpoint=discover(rt,orch)
    try:
        with endpoint.connect():pass
    except Exception:
        raise TransportUnavailable('Claude Orch endpoint is not reachable') from None
    return endpoint.info()
