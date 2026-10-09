"""Run a command inside a real Windows pseudo console (ConPTY), feed it keystrokes and return everything it displayed (card 7b-on tests).

The masked prompt of a switch-on script only works on a real console, so its interactive branch is tested here, with synthetic loud markers.
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
HPCON = wt.HANDLE
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFO(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE),
                ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE)]


class STARTUPINFOEX(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def run_in_console(command_line, keystrokes, env=None, cwd=None, wait_seconds=60, first_delay=3.0, step_delay=1.0):
    """-> (exit code, everything displayed). `keystrokes` is a list of strings sent one by one (e.g. 'secret\\r')."""
    in_read, in_write, out_read, out_write = wt.HANDLE(), wt.HANDLE(), wt.HANDLE(), wt.HANDLE()
    assert k32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0)
    assert k32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0)
    hpc = HPCON()
    hr = k32.CreatePseudoConsole(COORD(120, 40), in_read, out_write, 0, ctypes.byref(hpc))
    assert hr == 0, hr
    size = ctypes.c_size_t()
    k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attrs = ctypes.create_string_buffer(size.value)
    assert k32.InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size))
    assert k32.UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpc, ctypes.sizeof(hpc), None, None)
    si = STARTUPINFOEX()
    si.StartupInfo.cb = ctypes.sizeof(si)
    si.StartupInfo.dwFlags = 0x100              # STARTF_USESTDHANDLES with no handles: the child's output goes to the pseudo console, not to ours
    si.lpAttributeList = ctypes.cast(attrs, ctypes.c_void_p)
    pi = PROCESS_INFORMATION()
    block = None
    if env is not None:
        block = ctypes.create_unicode_buffer("\0".join("%s=%s" % item for item in env.items()) + "\0\0")
    ok = k32.CreateProcessW(None, ctypes.create_unicode_buffer(command_line), None, None, False, EXTENDED_STARTUPINFO_PRESENT | 0x00000400,
                            block, cwd, ctypes.byref(si), ctypes.byref(pi))
    assert ok, ctypes.get_last_error()
    k32.CloseHandle(in_read)
    k32.CloseHandle(out_write)
    chunks = []

    def reader():
        buf = ctypes.create_string_buffer(4096)
        got = wt.DWORD()
        while k32.ReadFile(out_read, buf, 4096, ctypes.byref(got), None) and got.value:
            chunks.append(buf.raw[:got.value])
    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(first_delay)
    for text in keystrokes:
        data = text.encode("utf-8")
        written = wt.DWORD()
        k32.WriteFile(in_write, data, len(data), ctypes.byref(written), None)
        time.sleep(step_delay)
    deadline = time.time() + wait_seconds
    code = wt.DWORD(259)
    while time.time() < deadline:
        k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        if code.value != 259:
            break
        time.sleep(0.3)
    if code.value == 259:
        k32.TerminateProcess(pi.hProcess, 1)           # exact process only
    time.sleep(0.5)
    k32.ClosePseudoConsole(hpc)
    thread.join(3)
    k32.CloseHandle(in_write)
    k32.CloseHandle(pi.hProcess)
    k32.CloseHandle(pi.hThread)
    return code.value, b"".join(chunks).decode("utf-8", "replace")
