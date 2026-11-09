"""Secret references are resolved only through a source-scoped provider."""
import os
import re


class SecretUnavailable(ValueError):
    pass


_REFERENCE = re.compile(r"os-store:[A-Za-z0-9][A-Za-z0-9._/-]*\Z")


def resolve(reference, *, adapter, source, provider):
    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise SecretUnavailable("invalid secret reference")
    if any(part in ("", ".", "..") for part in reference[len("os-store:"):].split("/")):
        raise SecretUnavailable("invalid secret reference")
    if not adapter or not source or provider is None:
        raise SecretUnavailable("secret provider unavailable")
    try:
        value = provider.get(adapter, source, reference)
    except Exception:  # OS providers may include sensitive material in their errors.
        raise SecretUnavailable("secret unavailable for source") from None
    if not isinstance(value, bytes) or not value:
        raise SecretUnavailable("secret unavailable for source")
    return value


class FakeSecretStore:
    """In-memory provider for synthetic tests; never writes secrets to disk."""
    def __init__(self, values):
        self._values = dict(values)

    def get(self, adapter, source, reference):
        return self._values[(adapter, source, reference)]


class WindowsSecretStore:
    """Generic Windows credentials scoped to an adapter and source."""
    def __init__(self, *, backend=None):
        self.backend = backend if backend is not None else _WindowsCredentials()

    @staticmethod
    def _prefix(adapter, source):
        if not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_-]+', x) for x in (adapter, source)):
            raise ValueError('invalid adapter or source')
        return f'orchd/channel/{adapter}/{source}/'

    def _target(self, adapter, source, reference):
        if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference) or any(p in ('', '.', '..') for p in reference[9:].split('/')):
            raise ValueError('invalid secret reference')
        return self._prefix(adapter, source) + reference

    def get(self, adapter, source, reference):
        return self.backend.read(self._target(adapter, source, reference))

    def put(self, adapter, source, reference, value):
        if not isinstance(value, bytes) or not value:
            raise ValueError('secret must be non-empty bytes')
        self.backend.write(self._target(adapter, source, reference), value)

    def delete(self, adapter, source, reference):
        self.backend.delete(self._target(adapter, source, reference))

    def list(self, adapter, source):
        prefix = self._prefix(adapter, source)
        return sorted(target[len(prefix):] for target in self.backend.list(prefix) if target.startswith(prefix))


class _WindowsCredentials:
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Windows Credential Manager is only available on Windows')
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        class Credential(ctypes.Structure):
            _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
                ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
                ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]
        self.Credential = Credential
        self.api = ctypes.WinDLL('advapi32', use_last_error=True)
        ptr = ctypes.POINTER(Credential)
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ptr)]
        self.api.CredWriteW.argtypes = [ptr, wintypes.DWORD]
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.POINTER(ptr))]
        self.api.CredFree.argtypes = [ctypes.c_void_p]

    def _error(self):
        raise OSError(self.ctypes.get_last_error(), 'Windows credential operation failed')

    def read(self, target):
        c = self.ctypes
        item = c.POINTER(self.Credential)()
        if not self.api.CredReadW(target, 1, 0, c.byref(item)): self._error()
        try:
            return c.string_at(item.contents.CredentialBlob, item.contents.CredentialBlobSize)
        finally:
            self.api.CredFree(item)

    def write(self, target, value):
        c = self.ctypes
        data = (c.c_ubyte * len(value)).from_buffer_copy(value)
        item = self.Credential(Type=1, TargetName=target, CredentialBlobSize=len(value), CredentialBlob=data, Persist=2)
        if not self.api.CredWriteW(c.byref(item), 0): self._error()

    def delete(self, target):
        if not self.api.CredDeleteW(target, 1, 0): self._error()

    def list(self, prefix):
        c = self.ctypes
        from ctypes import wintypes
        count = wintypes.DWORD()
        items = c.POINTER(c.POINTER(self.Credential))()
        if not self.api.CredEnumerateW(prefix + '*', 0, c.byref(count), c.byref(items)):
            if c.get_last_error() == 1168: return []
            self._error()
        try:
            return [items[i].contents.TargetName for i in range(count.value)]
        finally:
            self.api.CredFree(items)
