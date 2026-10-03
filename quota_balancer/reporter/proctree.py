"""Start a program so that it and everything it starts can be stopped together, by exact process id (card L6a-1, fix
round 1).

A program the reporter runs for a source of receipts may start children of its own, and a child can hold the program's
output open after the program itself has exited. Stopping only the program would leave those children running and the
reader waiting. So the program is started in its own place:

* POSIX: its own session and process group; `kill` signals that whole group by its id (the program's own pid).
* Windows: a job object that the program is put into straight after it starts; everything it starts afterwards belongs
  to the job too, and `kill` ends the whole job. Closing the job also ends anything still in it. If a job cannot be
  made or used (for example the system refuses a nested one), the fallback is `taskkill /T /F /PID <pid>`, again by the
  exact process id.

Nothing here ever looks a process up by its name.
"""

import contextlib
import os
import signal
import subprocess
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = ctypes.c_void_p
    _k32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [ctypes.c_void_p, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = wintypes.BOOL

    def _make_job(proc):
        """A job object with the process in it, or None if the system will not allow it."""
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                            ctypes.sizeof(info)) \
                or not _k32.AssignProcessToJobObject(handle, int(proc._handle)):
            _k32.CloseHandle(handle)
            return None
        return handle


class Tree:
    """A running program and everything it starts. `proc` is the `subprocess.Popen`."""

    def __init__(self, proc, job):
        self.proc = proc
        self._job = job

    def kill(self):
        """End the whole tree now, by exact id. Safe to call twice and after the program has exited."""
        if sys.platform == "win32":
            if self._job:
                _k32.TerminateJobObject(self._job, 1)
            else:
                taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    subprocess.run([taskkill, "/T", "/F", "/PID", str(self.proc.pid)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.proc.kill()

    def close(self):
        """Let go of the job (Windows). Anything still in it is ended with it."""
        if sys.platform == "win32" and self._job:
            _k32.CloseHandle(self._job)
            self._job = None


def start(argv, env, stdout):
    """Run `argv` (no shell, no standard input, no standard error) in its own session or job. -> Tree."""
    kwargs = {"shell": False, "stdin": subprocess.DEVNULL, "stdout": stdout, "stderr": subprocess.DEVNULL, "env": env}
    if sys.platform == "win32":
        proc = subprocess.Popen(argv, **kwargs)
        return Tree(proc, _make_job(proc))
    return Tree(subprocess.Popen(argv, start_new_session=True, **kwargs), None)
