"""Unix socket operations for CPython builds lacking the Windows AF_UNIX binding."""
import os
import select
import socket


def create():
    return socket.socket(getattr(socket, "AF_UNIX", 1))


def _address(path):
    import ctypes
    class Sockaddr(ctypes.Structure):
        _fields_ = [("family", ctypes.c_ushort), ("path", ctypes.c_char * 108)]
    encoded = os.fsencode(path)
    if len(encoded) >= 108:
        raise ValueError("Unix socket path exceeds Windows limit")
    return Sockaddr(1, encoded)


def _winsock(name, sock, path):
    import ctypes
    lib = ctypes.WinDLL("ws2_32", use_last_error=True)
    function = getattr(lib, name)
    function.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
    address = _address(path)
    if function(sock.fileno(), ctypes.byref(address), ctypes.sizeof(address)):
        raise OSError(lib.WSAGetLastError(), f"Unix socket {name} failed")


def connect(sock, path, timeout=None):
    if os.name != "nt" or hasattr(socket, "AF_UNIX"):
        sock.settimeout(timeout)
        sock.connect(path)
    else:
        # CPython 3.12 can create AF_UNIX sockets on Windows, but cannot encode their addresses.
        _winsock("connect", sock, path)
        sock.settimeout(timeout)


def bind(sock, path):
    if os.name != "nt" or hasattr(socket, "AF_UNIX"):
        sock.bind(path)
    else:
        _winsock("bind", sock, path)


def accept(sock):
    if os.name != "nt" or hasattr(socket, "AF_UNIX"):
        return sock.accept()
    import ctypes
    if not select.select([sock], [], [], sock.gettimeout())[0]:
        raise socket.timeout()
    lib = ctypes.WinDLL("ws2_32", use_last_error=True)
    lib.accept.argtypes = [ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    lib.accept.restype = ctypes.c_size_t
    fd = lib.accept(sock.fileno(), None, None)
    if fd == ctypes.c_size_t(-1).value:
        raise OSError(lib.WSAGetLastError(), "Unix socket accept failed")
    return socket.socket(1, socket.SOCK_STREAM, fileno=fd), None
