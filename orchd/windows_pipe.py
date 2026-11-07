"""Private connection to the local Claude named-pipe messaging endpoint."""
import ctypes
import os
import re
import json
import time
from ctypes import wintypes
from pathlib import Path

import psutil

if os.name == "nt":
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]


def open_pipe(path, executable):
    prefix = "\\\\.\\pipe\\orchd-"
    if not path.startswith(prefix) or not re.fullmatch("[a-zA-Z0-9-]+", path[len(prefix):]):
        raise ValueError("invalid Claude messaging pipe")
    handle = kernel.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error in (2, 53):
            raise FileNotFoundError(error, "Claude pipe not found", path)
        raise ctypes.WinError(error)
    try:
        pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
            raise ctypes.WinError(ctypes.get_last_error())
        peer = psutil.Process(pid.value)
        me = psutil.Process()
        if peer.username().casefold() != me.username().casefold():
            raise PermissionError("Claude pipe server has a different user")
        if Path(peer.exe()).resolve() != Path(executable).resolve():
            raise PermissionError("Claude pipe server is not the configured Claude executable")
        return handle, peer
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def listening(path, executable):
    try:
        handle, _ = open_pipe(path, executable)
    except FileNotFoundError:
        return False
    kernel.CloseHandle(handle)
    return True


def send(path, executable, data):
    handle, peer = open_pipe(path, executable)
    try:
        token = peer.environ().get("CLAUDE_CODE_MESSAGING_TOKEN")
        if not token:
            raise PermissionError("Claude pipe authentication token unavailable")
        data = (json.dumps({"type": "auth", "token": token}) + "\n").encode() + data
        written = wintypes.DWORD()
        buf = ctypes.create_string_buffer(data)
        if not kernel.WriteFile(handle, buf, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(data):
            raise OSError("partial write to Claude messaging pipe")
    finally:
        kernel.CloseHandle(handle)


def wait_for_receipt(session_id, message_id, timeout=15):
    """A transcript user event with our UUID proves that Claude admitted the frame."""
    if not re.fullmatch(r"[a-zA-Z0-9-]+", session_id) or not re.fullmatch(r"[a-zA-Z0-9-]+", message_id):
        raise ValueError("invalid Claude session or message id")
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    projects = config / "projects"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for transcript in projects.glob(f"*/{session_id}.jsonl"):
            try:
                with transcript.open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    stream.seek(max(0, size - 1024 * 1024))
                    if size > 1024 * 1024:
                        stream.readline()
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if event.get("type") == "user" and event.get("uuid") == message_id:
                            return True
            except OSError:
                continue
        time.sleep(.1)
    return False
