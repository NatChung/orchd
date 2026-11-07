"""Platform-specific private worker directories and detached process options."""
import os
import subprocess


def detached_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


def make_private(directory):
    if os.name != "nt":
        directory.chmod(0o700)
        return
    import csv
    import io
    output = subprocess.check_output(["whoami", "/user", "/fo", "csv", "/nh"], text=True)
    sid = next(csv.reader(io.StringIO(output)))[1]
    subprocess.run(["icacls", str(directory), "/inheritance:r", "/remove:g",
                    "*S-1-5-18", "*S-1-5-32-544", "*S-1-3-4"], check=True, capture_output=True)
    subprocess.run(["icacls", str(directory), "/grant:r",
                    f"*{sid}:(OI)(CI)F"], check=True, capture_output=True)
    validate_private(directory)


def validate_private(directory):
    if os.name != "nt":
        stat = directory.lstat()
        if directory.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o777 != 0o700:
            raise ValueError("app-server directory is not private")
        return
    import ctypes
    from ctypes import wintypes

    if directory.is_symlink() or directory.is_junction():
        raise ValueError("app-server directory is not private")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    current = ctypes.c_void_p()
    token = wintypes.HANDLE()
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                    ctypes.POINTER(wintypes.DWORD)]
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    error = advapi.GetNamedSecurityInfoW(str(directory), 1, 0x5, ctypes.byref(owner), None,
                                         ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise OSError(error, "cannot inspect worker directory ACL")
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not control.value & 0x1000:  # SE_DACL_PROTECTED
            raise ValueError("app-server directory ACL inherits permissions")
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x8, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buf, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        current = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        if not owner or not dacl or not advapi.EqualSid(owner, current):
            raise ValueError("app-server directory is not private")
        # ACL header stores AceCount at byte offset 4.
        count = ctypes.c_ushort.from_address(dacl.value + 4).value
        if count == 0:
            raise ValueError("app-server directory has no access rules")
        for index in range(count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            # ACCESS_ALLOWED_ACE has SID at offset 8. Fail closed on other ACE types.
            if ctypes.c_ubyte.from_address(ace.value).value != 0 or not advapi.EqualSid(ace.value + 8, current):
                raise ValueError("app-server directory is not private")
    finally:
        if token:
            kernel.CloseHandle(token)
        kernel.LocalFree(descriptor)
