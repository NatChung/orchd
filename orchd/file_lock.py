"""Nonblocking exclusive locks for dedicated lock files, released on close."""
import os

if os.name == "nt":
    import errno
    import msvcrt

    def try_lock(handle):
        # Lock the same byte on every open, including on an empty file.
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno != errno.EACCES:
                raise
            raise BlockingIOError(errno.EAGAIN, "lock already held") from exc
else:
    import fcntl

    def try_lock(handle):
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
