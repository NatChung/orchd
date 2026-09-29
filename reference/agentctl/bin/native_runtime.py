"""Native session hosting. One private Codex server or Claude background job per agent.

The watcher is a subscriber, not a terminal owner. Attaching/detaching a TUI never
stops a job. SQLite records the exact job/process generation that we may stop.
"""
import authorization
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid


class UnixWebSocket:
    """Bounded RFC6455 client for the local Codex control socket (stdlib only)."""
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(30)
        self.buf = b""
        self.fragments = bytearray()
        try:
            self.sock.connect(path)
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                               "Sec-WebSocket-Key: " + key + "\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buf:
                self._read()
            header, self.buf = self.buf.split(b"\r\n\r\n", 1)
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
            if b" 101 " not in header.split(b"\r\n")[0] or expected not in header:
                raise RuntimeError("Codex control socket rejected WebSocket handshake")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.sock.close()

    def _read(self):
        part = self.sock.recv(65536)
        if not part:
            raise EOFError("Codex control socket closed")
        self.buf += part
        if len(self.buf) > 32 * 1024 * 1024:
            raise RuntimeError("Codex frame exceeds 32 MiB")

    def frame(self, data, opcode=1):
        mask = os.urandom(4)
        size = len(data)
        head = bytes([0x80 | opcode])
        head += (bytes([0x80 | size]) if size < 126 else
                 b"\xfe" + struct.pack("!H", size) if size < 65536 else b"\xff" + struct.pack("!Q", size))
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def send(self, message):
        self.frame(json.dumps(message).encode())

    def receive(self):
        while True:
            while len(self.buf) < 2:
                self._read()
            first, second = self.buf[:2]
            size, offset = second & 127, 2
            if size in (126, 127):
                extra = 2 if size == 126 else 8
                while len(self.buf) < 2 + extra:
                    self._read()
                size = int.from_bytes(self.buf[2:2 + extra], "big")
                offset += extra
            if second & 128 or size > 32 * 1024 * 1024:
                raise RuntimeError("invalid Codex server WebSocket frame")
            while len(self.buf) < offset + size:
                self._read()
            data, self.buf = self.buf[offset:offset + size], self.buf[offset + size:]
            opcode = first & 15
            if opcode == 8:
                raise EOFError("Codex closed the session connection")
            if opcode == 9:
                self.frame(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RuntimeError("unsupported Codex WebSocket frame")
            self.fragments.extend(data)
            if len(self.fragments) > 32 * 1024 * 1024:
                raise RuntimeError("Codex message exceeds 32 MiB")
            if first & 128:
                data = bytes(self.fragments)
                self.fragments.clear()
                return json.loads(data)

    def request(self, method, params, callback=lambda event: None):
        rid = uuid.uuid4().hex
        self.send(dict(id=rid, method=method, params=params))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            event = self.receive()
            if event.get("id") == rid:
                if "error" in event:
                    raise RuntimeError(str(event["error"]))
                return event["result"]
            callback(event)
        raise TimeoutError("Codex request timed out: " + method)


def schema(con):
    columns = {r[1] for r in con.execute("PRAGMA table_info(workers)")}
    if "backend" not in columns:
        con.execute("ALTER TABLE workers ADD COLUMN backend TEXT NOT NULL DEFAULT 'tmux'")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS management(id INTEGER PRIMARY KEY CHECK(id=1), backend TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS native_sessions(
            worker_id TEXT PRIMARY KEY, generation TEXT NOT NULL, kind TEXT NOT NULL,
            session_id TEXT, job_id TEXT, endpoint TEXT NOT NULL,
            watcher_pid INTEGER, watcher_start TEXT, pid INTEGER, process_start TEXT,
            stopping INTEGER NOT NULL DEFAULT 0, error TEXT, log_path TEXT NOT NULL);
    """)


def row(con, wid):
    return con.execute("SELECT * FROM native_sessions WHERE worker_id=?", (wid,)).fetchone()


def alive(ctl, record, field="pid"):
    if not record or not record[field]:
        return False
    info = ctl.process_info(record[field])
    start = record["watcher_start" if field == "watcher_pid" else "process_start"]
    return bool(info and info["start"] == start)


def active(ctl, con, w):
    r = row(con, w["id"])
    return bool(r and not r["stopping"] and alive(ctl, r) and alive(ctl, r, "watcher_pid"))


def environment(ctl, w):
    env = dict(os.environ, AGENTCTL_HOME=ctl.HOME, AGENTCTL_WORKER=w["id"], AGENTCTL_NATIVE="1")
    for key in ("TMUX", "TMUX_PANE", "CLAUDECODE", "AGENTCTL_ORCH_SESSION"):
        env.pop(key, None)
    return env


def brief(ctl, con, w):
    command = "AGENTCTL_HOME=%s %s" % (shlex.quote(ctl.HOME), shlex.quote(ctl.BIN))
    role = ("You are the Orchestrator. Dispatch project work with agentctl send to the matching worker. "
            "For managed task worktrees use scheduler enqueue/plan and inspect scheduler status; "
            "do not use direct send for a scheduled task. Create and start matching-cwd workers explicitly. "
            "Task done is task acceptance, distinct from report done. "
            "Read worker reports using inbox --mark-read. Do not perform project work yourself."
            if w["role"] == "orchestrator" else "You are a managed worker. Execute only dispatched or human-requested work.")
    return ("[agentctl native] " + role + " Your id is " + w["id"] + ". Command prefix: " + command +
            ". For every [agentctl msg:ID], run `ack ID` FIRST, read its message file, then run "
            "`report ID --status done|blocked|question SUMMARY` exactly once. A wake is a notification: "
            "read `inbox --mark-read`, never report a wake. Stop alone is not task completion. "
            "Do not poll/wait for work; report and finish the turn. "
            "Foreground detach leaves work running; only agentctl stop ends the group. "
            "Use status to discover workers and tasks. For goal work read docs/goal-coordination.md in the agentctl repo. "
            "For external intake read docs/incoming-messages.md in the agentctl repo. Use intake list/show/resolve; inbox read is not handling or reply. "
            + authorization.PROMPT +
            "Orch: inspect goal list/show and question list, answer queued questions, and record overall verification. "
            "Worker: ask question new --task TASK --from YOUR_ID [--blocking] without closing the dispatch; "
            "answers arrive via inbox. Preserve goal pause and failure limits. "
            "The management mode is native: do not use tmux or start an independent orchestrator.")


def start(ctl, con, w, resume=False, args=(), recovery_owner=None):
    if not con.in_transaction:
        con.execute("BEGIN IMMEDIATE")
    ctl.recovery.worker_mutation_gate(ctl, con, w, recovery_owner)
    old = row(con, w["id"])
    if old and (alive(ctl, old) or alive(ctl, old, "watcher_pid")):
        raise RuntimeError("%s still has a native process; stop it before restarting" % w["id"])
    if resume and (not old or not old["session_id"] or old["kind"] != w["kind"]):
        raise RuntimeError("no saved %s session for %s" % (w["kind"], w["id"]))
    # A short private directory avoids macOS's 104-byte Unix socket limit.
    runtime = Path(tempfile.mkdtemp(prefix="agentctl-", dir="/tmp"))
    generation = uuid.uuid4().hex
    logdir = Path(ctl.HOME) / "native"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = str(logdir / (generation + ".log"))
    requestfile = logdir / (generation + ".json")
    requestfile.write_text(json.dumps({"args": list(args), "resume": old["session_id"] if resume else None}))
    requestfile.chmod(0o600)
    con.execute("INSERT OR REPLACE INTO native_sessions(worker_id,generation,kind,session_id,endpoint,log_path) "
                "VALUES(?,?,?,?,?,?)", (w["id"], generation, w["kind"], old["session_id"] if resume else None,
                                       str(runtime / "session.sock"), logfile))
    reservation = ctl.process_info(os.getpid())
    con.execute("UPDATE native_sessions SET watcher_pid=?,watcher_start=? WHERE worker_id=?",
                (reservation["pid"], reservation["start"], w["id"]))
    ctl.reconcile_restart(con, w, "native session restarted; explicit resend required")
    con.execute("UPDATE workers SET backend='native',session_id=NULL WHERE id=?", (w["id"],))
    if not resume:
        con.execute("UPDATE workers SET last_message=NULL WHERE id=?", (w["id"],))
    ctl.set_state(con, w["id"], "starting")
    con.commit()
    with open(logfile, "a") as log:
        subprocess.Popen([sys.executable, ctl.BIN, "native-watch", w["id"], generation],
                         env=environment(ctl, w), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        r = row(con, w["id"])
        if r["error"]:
            raise RuntimeError(r["error"] + "; log: " + logfile)
        if r["session_id"] and alive(ctl, r) and ctl.get_worker(con, w["id"])["state"] != "starting":
            return
        time.sleep(.1)
    raise RuntimeError("native launch timed out; inspect status/log and stop before retrying: " + logfile)


def claude_agents(env=None):
    result = subprocess.run(["claude", "agents", "--json"], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=15, env=env, check=True)
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise RuntimeError("unsupported claude agents JSON format")
    return data


def codex_options(args):
    """Translate supported launch overrides. Never silently discard TUI arguments."""
    params, config = {}, {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--dangerously-bypass-approvals-and-sandbox":
            params.update(approvalPolicy="never", sandbox="danger-full-access")
        elif arg in ("-m", "--model", "-c", "--config", "-s", "--sandbox", "-a", "--ask-for-approval") and i + 1 < len(args):
            i += 1
            value = args[i]
            if arg in ("-c", "--config"):
                key, sep, val = value.partition("=")
                if not sep:
                    raise RuntimeError("config override needs key=value")
                try:
                    val = json.loads(val)
                except ValueError:
                    pass
                config[key] = val
            else:
                params[{"-m": "model", "--model": "model", "-s": "sandbox", "--sandbox": "sandbox",
                        "-a": "approvalPolicy", "--ask-for-approval": "approvalPolicy"}[arg]] = value
        else:
            raise RuntimeError("unsupported native Codex launch argument: %s (use --resume to resume)" % arg)
        i += 1
    params["config"] = config
    return params


def codex_event(ctl, con, w, event):
    method, p = event.get("method"), event.get("params", {})
    if method == 'account/rateLimits/updated':
        r = row(con, w['id'])
        if r:
            ctl.quota.unavailable(con, w['id'], r['generation'], 'codex', 'provider update; quota refresh required', dirty=True)
            con.commit()
        return
    if method not in {"turn/started", "turn/completed", "thread/status/changed", "item/started", "item/completed"}:
        return
    r = row(con, w["id"])
    if p.get("threadId") != r["session_id"]:
        return
    if method == "turn/started":
        ctl.set_state(con, w["id"], "busy")
    elif method == "turn/completed":
        ctl.set_state(con, w["id"], "idle")
    elif method == "thread/status/changed":
        status = p.get("status", {}).get("type")
        if status in ("idle", "active", "systemError"):
            ctl.set_state(con, w["id"], {"idle": "idle", "active": "busy", "systemError": "error"}[status])
    elif method in ("item/started", "item/completed"):
        item = p.get("item", {})
        if item.get("type") == "userMessage":
            text = "\n".join(part.get("text", "") for part in item.get("content", []) if isinstance(part, dict))
            ctl.ack_native(con, w, text, "codex " + method)
        elif item.get("type") == "agentMessage" and method == "item/completed":
            con.execute("UPDATE workers SET last_message=? WHERE id=?", (item.get("text", "")[:500], w["id"]))
    ctl.log_event(con, "native." + str(method), w["id"], payload={"turn": p.get("turnId")})
    con.commit()


def codex_thread(ws, params, resume, name):
    if resume:
        params["threadId"] = resume
    sid = ws.request("thread/resume" if resume else "thread/start", params)["thread"]["id"]
    if not resume:
        # Codex defers an empty rollout until persistence is requested. Naming
        # materializes its metadata without starting a model turn.
        ws.request("thread/name/set", {"threadId": sid, "name": name})
        # Naming alone leaves empty paginated history without its source rollout.
        # Hydrate once before the TUI's metadata-only (excludeTurns) resume.
        ws.request("thread/resume", {"threadId": sid})
    return sid


def run(ctl, a):
    con = ctl.db()
    w, r = ctl.get_worker(con, a.worker), row(con, a.worker)
    if not r or r["generation"] != a.generation or r["stopping"]:
        return
    own = ctl.process_info(os.getpid())
    con.execute("UPDATE native_sessions SET watcher_pid=?,watcher_start=? WHERE worker_id=?",
                (own["pid"], own["start"], w["id"]))
    con.commit()
    requestfile = Path(ctl.HOME) / "native" / (a.generation + ".json")
    request = json.loads(requestfile.read_text())
    requestfile.unlink()
    env = environment(ctl, w)
    child, ws = None, None
    stopping = False

    def interrupt(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        if w["kind"] == "codex":
            params = codex_options(ctl.cli_args("codex", request["args"]))
            child = subprocess.Popen(["codex", "app-server", "--listen", "unix://" + r["endpoint"]],
                                     cwd=w["cwd"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=sys.stdout, stderr=sys.stderr, start_new_session=True)
            process = ctl.process_info(child.pid)
            con.execute("UPDATE native_sessions SET pid=?,process_start=? WHERE worker_id=?",
                        (child.pid, process["start"], w["id"]))
            con.commit()
            deadline = time.monotonic() + 25
            while not os.path.exists(r["endpoint"]):
                if child.poll() is not None or stopping or time.monotonic() > deadline:
                    raise RuntimeError("private Codex server failed to start")
                time.sleep(.1)
            ws = UnixWebSocket(r["endpoint"])
            ws.request("initialize", {"clientInfo": {"name": "agentctl", "version": "1"},
                                      "capabilities": {"experimentalApi": True}})
            ws.send({"method": "initialized", "params": {}})
            params.update(cwd=w["cwd"], developerInstructions=brief(ctl, con, w))
            sid = codex_thread(ws, params, request["resume"], "agentctl " + w["id"])
            job = sid
        else:
            # stdin must be DEVNULL: --bg treats piped stdin as a launch prompt.
            launch_args = ctl.cli_args('claude', request['args'])
            try:
                launch_args = ctl.quota.claude_launch_settings(ctl, w, a.generation, launch_args)
            except (OSError, ValueError, TypeError) as error:
                reason = str(error) if type(error) is ValueError else type(error).__name__
                ctl.quota.unavailable(con, w['id'], a.generation, 'claude', 'collector not configured: ' + reason)
                con.commit()
                launch_args = ctl.quota.claude_launch_settings(ctl, w, a.generation, launch_args, collect=False)
            command = ["claude", "--bg"] + launch_args
            command += ["--messaging-socket-path", r["endpoint"],
                        "--append-system-prompt", brief(ctl, con, w), "--system-prompt-snapshot", "off"]
            if request["resume"]:
                command += ["--resume", request["resume"]]
            result = subprocess.run(command, cwd=w["cwd"], env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=40, check=True)
            print(result.stdout, end="", flush=True)
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="", flush=True)
            match = re.search(r"claude attach ([a-zA-Z0-9-]+)", result.stdout)
            if not match:
                raise RuntimeError("Claude background launch returned no job ID: " + result.stdout[-1000:])
            job = match[1]
            con.execute("UPDATE native_sessions SET job_id=? WHERE worker_id=?", (job, w["id"]))
            con.commit()
            deadline = time.monotonic() + 25
            while True:
                agent = next((x for x in claude_agents(env) if x.get("id") == job), None)
                if agent and agent.get("sessionId") and os.path.exists(r["endpoint"]):
                    break
                if stopping or time.monotonic() > deadline:
                    raise RuntimeError("Claude background job did not publish its session/socket")
                time.sleep(.2)
            sid = agent["sessionId"]
            process = ctl.process_info(agent["pid"])
            if not process:
                raise RuntimeError("Claude background process exited during launch")
        con.execute("UPDATE native_sessions SET session_id=?,job_id=?,pid=?,process_start=? WHERE worker_id=?",
                    (sid, job, process["pid"], process["start"], w["id"]))
        con.execute("UPDATE workers SET session_id=? WHERE id=?", (sid, w["id"]))
        ctl.set_state(con, w["id"], "idle")
        ctl.log_event(con, "native.started", w["id"], payload={"kind": w["kind"], "session": sid})
        if request["resume"] and request["resume"] != sid:
            ctl.log_event(con, "native.resumed_copy", w["id"], payload={"from": request["resume"], "to": sid})
        con.commit()
        w = ctl.get_worker(con, w["id"])
        if w['kind'] == 'codex':
            try:
                ctl.quota.spawn_refresh(ctl, w['id'], a.generation)
            except OSError:
                ctl.quota.unavailable(con, w['id'], a.generation, 'codex', 'initial quota refresh unavailable')
                con.commit()
        tick = 0
        quota_refreshed = time.monotonic()
        while not stopping:
            r = row(con, w["id"])
            if not r or r["generation"] != a.generation or r["stopping"]:
                break
            if not alive(ctl, r):
                raise RuntimeError("native agent exited unexpectedly; workers retained, no automatic restart")
            if ws and (ws.buf or select.select([ws.sock], [], [], .2)[0]):
                codex_event(ctl, con, w, ws.receive())
            elif not ws:
                time.sleep(.2)
            if time.monotonic() - tick < 1:
                continue
            tick = time.monotonic()
            control = con.execute('SELECT enabled FROM scheduler_control WHERE id=1').fetchone()
            if ws and control and control['enabled'] and time.monotonic() - quota_refreshed >= 60:
                # A scheduler must refresh evidence even with no terminal viewer.
                # This is the existing read-only adapter, never a model turn.
                try:
                    ctl.quota.spawn_refresh(ctl, w['id'], a.generation)
                except OSError:
                    ctl.quota.unavailable(con, w['id'], a.generation, 'codex', 'periodic quota refresh unavailable')
                    con.commit()
                quota_refreshed = time.monotonic()
            if not ws:
                agent = next((x for x in claude_agents(env) if x.get("id") == job), None)
                if not agent:
                    raise RuntimeError("Claude background job no longer listed")
                status = agent.get("status")
                # Unknown native states never authorize dispatch.
                mapped = {"idle": "idle", "busy": "busy", "waiting_permission": "waiting_permission"}
                ctl.set_state(con, w["id"], mapped.get(status, "waiting_permission"))
                con.commit()
            if w["role"] == "orchestrator":
                ctl.pump_orchestrator(con)
                con.commit()
                ctl.recovery.spawn_pending(ctl, con)
            else:
                ctl.pump_worker(con, ctl.get_worker(con, w["id"]))
            con.commit()
    except Exception as error:
        con.rollback()
        con.execute("UPDATE native_sessions SET error=? WHERE worker_id=? AND generation=?",
                    (str(error), w["id"], a.generation))
        ctl.set_state(con, w["id"], "error")
        ctl.log_event(con, "native.error", w["id"], payload={"error": str(error)})
        con.commit()
        print(str(error), file=sys.stderr, flush=True)
    finally:
        if ws:
            ws.close()
        r = row(con, w["id"])
        # A failed observer must not silently stop a live Claude job. Stop remains explicit.
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        if w["kind"] == "claude" and r and r["stopping"] and r["job_id"]:
            # Cancellation may race --bg before the job's PID has been published.
            result = subprocess.run(["claude", "stop", r["job_id"]], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=20)
            if result.returncode and alive(ctl, r):
                con.execute("UPDATE native_sessions SET error=? WHERE worker_id=?",
                            ("Claude stop failed: " + result.stderr[-1000:], w["id"]))
        if r and r["stopping"]:
            ctl.set_state(con, w["id"], "stopped")
        con.commit()
        con.close()


def send(ctl, con, w, text):
    r = row(con, w["id"])
    if not active(ctl, con, w):
        raise RuntimeError("native receiver/observer unavailable; inspect status before retrying")
    if w["kind"] == "codex":
        subprocess.run(["codex", "queue", "--remote", "unix://" + r["endpoint"],
                        "--thread", r["session_id"], "--message", text],
                       env=environment(ctl, w), stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=30, check=True)
    else:
        # On macOS Claude accepts same-user UDS frames. Socket writes are transport
        # evidence only; model ack/report is required before any later dispatch.
        with socket.socket(socket.AF_UNIX) as peer:
            peer.settimeout(5)
            peer.connect(r["endpoint"])
            peer.sendall((json.dumps({"type": "user", "session_id": r["session_id"],
                                     "uuid": str(uuid.uuid4()), "from": "agentctl",
                                     "priority": "next", "message": {"role": "user", "content": text}}) + "\n").encode())
            peer.shutdown(socket.SHUT_WR)


def stop(ctl, con, w):
    r = row(con, w["id"])
    if not r:
        return
    con.execute("UPDATE native_sessions SET stopping=1 WHERE worker_id=?", (w["id"],))
    con.commit()
    if alive(ctl, r):
        if r["kind"] == "claude":
            subprocess.run(["claude", "stop", r["job_id"]], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=20, check=True)
        else:
            os.kill(r["pid"], signal.SIGTERM)
    if alive(ctl, r, "watcher_pid"):
        os.kill(r["watcher_pid"], signal.SIGTERM)
    deadline = time.monotonic() + 10
    while (alive(ctl, r) or alive(ctl, r, "watcher_pid")) and time.monotonic() < deadline:
        time.sleep(.1)
    if alive(ctl, r) or alive(ctl, r, "watcher_pid"):
        con.execute("UPDATE native_sessions SET error=? WHERE worker_id=?", ("stop failed: processes still running", w["id"]))
        ctl.set_state(con, w["id"], "error")
        con.commit()
        raise RuntimeError("native processes still running; stop failed for " + w["id"])
    ctl.reconcile_restart(con, w, "explicit native stop")
    con.execute("UPDATE messages SET state='interrupted' WHERE recipient=? AND kind='dispatch' "
                "AND state IN ('queued','sending')", (w["id"],))
    ctl.set_state(con, w["id"], "stopped")
    ctl.log_event(con, "native.stopped", w["id"])
    con.commit()


def attach_argv(ctl, con, w):
    r = row(con, w["id"])
    if not active(ctl, con, w):
        raise RuntimeError("native session is not running; start it first (or use --resume)")
    if w["kind"] == "claude":
        return ["claude", "attach", r["job_id"]]
    return ["codex", "resume", "--remote", "unix://" + r["endpoint"],
            "--cd", w["cwd"], r["session_id"]]
