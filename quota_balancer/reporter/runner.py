"""The contained Windows runner for a receipt command (card L6a-1b). The reporter's receipt reading calls it when a `command` source is configured.

A program the reporter runs may start other programs, and when the reporter gives up on it (too slow, too much output,
or the reporter itself stops) all of them must stop, including a helper started in the very first instant. So:

* **Created inside its job.** The program is created frozen, and already inside a Windows job that ends everything in it
  (the job is named in the creation attributes, so there is no moment in which it is outside), checked to be inside, and
  only then allowed to run. If the job cannot be used the read is refused. It is never moved in afterwards, and never
  run uncontained.
* **One deadline, and no thread.** The output is read by looking at the pipe without blocking, never by a reader thread
  or a buffered stream, so nothing can wait on a helper that still holds the pipe.
* **The end is confirmed.** The whole job is ended and its own count of live processes is polled until it reads zero. If
  that cannot be confirmed, the read says so (`cleanup_confirmed` is False) and its output is never used.

The timing promise holds while the Windows calls return normally. Not covered: creating the process, the setup before the
clock starts, and a possible wait inside `PeekNamedPipe` (documented by Windows). The reporter must be the only reader of
the pipe. This is lifetime containment, not a sandbox against the operator's own command.

`run` returns one closed word and, on success, the bytes. Every Windows call goes through `_Win`, which tests replace one
call at a time. No `subprocess` is used.
"""

import ctypes
import os
import sys
import time

from . import cmdline

OK = "ok"
# The two closed modes (card R1-READER step 1). `receipts` appends `--after N --limit M --json` (the receipt command). `report` appends
# nothing: `agenttalk gateway report` takes no `--after` or `--limit`, and its required `--json` is part of the mode's fixed command,
# `REPORT_TAIL`, which the configured command must end with (else nothing is started). Everything else (containment, one deadline, a
# bounded read, the closed words) is the same for both. The command's failures (`bad_request`, `report_unavailable`, exit code 2) all
# arrive as `exit_code`: standard error is the NUL device and its text is never read or kept.
REPORT_TAIL = ("gateway", "report", "--json")
MODE_RECEIPTS = "receipts"
MODE_REPORT = "report"
MODES = (MODE_RECEIPTS, MODE_REPORT)
# read later (nothing is stored, and it is asked again)
CANNOT_RUN = "cannot_run"                 # the program could not be started
CANNOT_CONTAIN = "cannot_contain"         # a containment step failed: it never ran uncontained
TIMEOUT = "timeout"
INTERRUPTED = "interrupted"
EXIT_CODE = "exit_code"
LEFTOVER = "leftover_process"             # the program exited but a helper still held the output
READ_FAILED = "read_failed"               # looking at the pipe or the program failed
CLEANUP_INCOMPLETE = "cleanup_incomplete" # a read that would have been fine, but the end of the job was not confirmed
# refused (the output is not a page)
TOO_LARGE = "too_large"

CLEANUP_SECONDS = 2.0                     # after the deadline: the one allowance for ending and confirming the job
LINGER_SECONDS = 0.5                      # after the program exits, for the pipe to close (capped by the deadline)
POLL_SECONDS = 0.01
PIPE_BYTES = 65536
JOB_PROCESS_LIMIT = 32
INHERITED = ("SYSTEMROOT", "SYSTEMDRIVE", "PATH", "TEMP", "TMP")       # what a program needs to start; no secrets

# Windows' own numbers (documented in the Win32 headers)
_KILL_ON_JOB_CLOSE = 0x2000
_ACTIVE_PROCESS_LIMIT = 0x8
_DIE_ON_UNHANDLED_EXCEPTION = 0x400
_EXTENDED_LIMITS, _BASIC_ACCOUNTING = 9, 1
_HANDLE_LIST, _JOB_LIST = 0x00020002, 0x0002000D
_EXTENDED_STARTUPINFO_PRESENT, _CREATE_SUSPENDED = 0x00080000, 0x00000004
_CREATE_NO_WINDOW, _CREATE_UNICODE_ENVIRONMENT = 0x08000000, 0x00000400
_STARTF_USESTDHANDLES = 0x100
_ERROR_BROKEN_PIPE = 109
_ERRORS_PROGRAM_CANNOT_START = (2, 3, 193)          # not found, path not found, not a valid program
_WAIT_SIGNALED, _WAIT_TIMEOUT = 0, 0x102

if sys.platform == "win32":
    from ctypes import wintypes

    _HANDLE = ctypes.c_void_p
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

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

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", _HANDLE), ("hStdOutput", _HANDLE),
                    ("hStdError", _HANDLE)]

    class _StartupInfoEx(ctypes.Structure):
        _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", _HANDLE), ("hThread", _HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _P = ctypes.POINTER
    # Every call that is used, with its types: a handle is never cut to 32 bits, and a test can see each one.
    _SIGNATURES = {
        "CreateJobObjectW": (_HANDLE, [ctypes.c_void_p, wintypes.LPCWSTR]),
        "SetInformationJobObject": (wintypes.BOOL, [_HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]),
        "QueryInformationJobObject": (wintypes.BOOL, [_HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                      ctypes.c_void_p]),
        "TerminateJobObject": (wintypes.BOOL, [_HANDLE, wintypes.UINT]),
        "IsProcessInJob": (wintypes.BOOL, [_HANDLE, _HANDLE, _P(wintypes.BOOL)]),
        "CreatePipe": (wintypes.BOOL, [_P(_HANDLE), _P(_HANDLE), _P(_SecurityAttributes), wintypes.DWORD]),
        "SetHandleInformation": (wintypes.BOOL, [_HANDLE, wintypes.DWORD, wintypes.DWORD]),
        "CreateFileW": (_HANDLE, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, _P(_SecurityAttributes),
                                  wintypes.DWORD, wintypes.DWORD, _HANDLE]),
        "InitializeProcThreadAttributeList": (wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                              _P(ctypes.c_size_t)]),
        "UpdateProcThreadAttribute": (wintypes.BOOL, [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t,
                                                      ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                                      ctypes.c_void_p]),
        "DeleteProcThreadAttributeList": (None, [ctypes.c_void_p]),
        "CreateProcessW": (wintypes.BOOL, [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                           wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.c_void_p, ctypes.c_void_p]),
        "ResumeThread": (wintypes.DWORD, [_HANDLE]),
        "TerminateProcess": (wintypes.BOOL, [_HANDLE, wintypes.UINT]),
        "PeekNamedPipe": (wintypes.BOOL, [_HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                          _P(wintypes.DWORD), ctypes.c_void_p]),
        "ReadFile": (wintypes.BOOL, [_HANDLE, ctypes.c_void_p, wintypes.DWORD, _P(wintypes.DWORD), ctypes.c_void_p]),
        "WaitForSingleObject": (wintypes.DWORD, [_HANDLE, wintypes.DWORD]),
        "GetExitCodeProcess": (wintypes.BOOL, [_HANDLE, _P(wintypes.DWORD)]),
        "CloseHandle": (wintypes.BOOL, [_HANDLE]),
    }
    for _name, (_restype, _argtypes) in _SIGNATURES.items():
        _function = getattr(_k32, _name)
        _function.restype, _function.argtypes = _restype, _argtypes
    FUNCTIONS = tuple(_SIGNATURES)
else:
    FUNCTIONS = ()


class _Win:
    """The Windows calls, one small method each, answering plain values (a handle, a bool, a number, or None when it
    failed). The runner only ever calls these, so a test can make any single one fail."""

    def create_job(self):
        return _k32.CreateJobObjectW(None, None) or None

    def set_limits(self, job, process_limit):
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE | _ACTIVE_PROCESS_LIMIT | _DIE_ON_UNHANDLED_EXCEPTION
        info.BasicLimitInformation.ActiveProcessLimit = process_limit
        return bool(_k32.SetInformationJobObject(job, _EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)))

    def create_pipe(self, size):
        """-> (read end, write end), both inheritable, or None."""
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        read, write = _HANDLE(), _HANDLE()
        if not _k32.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(attributes), size):
            return None
        return read.value, write.value

    def make_private(self, handle):
        """The handle may not be inherited by anything."""
        return bool(_k32.SetHandleInformation(handle, 1, 0))

    def open_nul(self):
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        handle = _k32.CreateFileW("NUL", 0xC0000000, 3, ctypes.byref(attributes), 3, 0, None)
        return None if handle in (None, _INVALID_HANDLE) else handle

    def init_attributes(self, count):
        size = ctypes.c_size_t(0)
        _k32.InitializeProcThreadAttributeList(None, count, 0, ctypes.byref(size))     # fails: it only sizes the buffer
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not _k32.InitializeProcThreadAttributeList(buffer, count, 0, ctypes.byref(size)):
            return None
        return buffer

    def update_attribute(self, attributes, attribute, array):
        """`array` is a ctypes array of handles; the caller keeps it alive until `delete_attributes`."""
        return bool(_k32.UpdateProcThreadAttribute(attributes, 0, attribute, array, ctypes.sizeof(array), None, None))

    def delete_attributes(self, attributes):
        _k32.DeleteProcThreadAttributeList(attributes)

    def create_process(self, application, command_line, environment, directory, std_in, std_out, std_err, attributes):
        """The program, created frozen, inside the job named in `attributes`. -> ((process, thread, id), 0), or
        (None, the Windows error number)."""
        info = _StartupInfoEx()
        info.StartupInfo.cb = ctypes.sizeof(info)
        info.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        info.StartupInfo.hStdInput, info.StartupInfo.hStdOutput, info.StartupInfo.hStdError = std_in, std_out, std_err
        info.lpAttributeList = ctypes.addressof(attributes)
        made = _ProcessInformation()
        flags = _EXTENDED_STARTUPINFO_PRESENT | _CREATE_SUSPENDED | _CREATE_NO_WINDOW | _CREATE_UNICODE_ENVIRONMENT
        block = (ctypes.c_wchar * len(environment))(*environment)
        line = ctypes.create_unicode_buffer(command_line)
        if not _k32.CreateProcessW(application, line, None, None, True, flags, block, directory, ctypes.byref(info),
                                   ctypes.byref(made)):
            return None, ctypes.get_last_error()
        return (made.hProcess, made.hThread, made.dwProcessId), 0

    def is_process_in_job(self, process, job):
        """True or False, or None when the question could not be asked."""
        answer = wintypes.BOOL()
        if not _k32.IsProcessInJob(process, job, ctypes.byref(answer)):
            return None
        return bool(answer.value)

    def resume_thread(self, thread):
        """The thread's earlier suspend count, or -1."""
        before = _k32.ResumeThread(thread)
        return -1 if before == 0xFFFFFFFF else before

    def terminate_process(self, process):
        return bool(_k32.TerminateProcess(process, 1))

    def terminate_job(self, job):
        return bool(_k32.TerminateJobObject(job, 1))

    def job_active(self, job):
        """How many processes the job still holds, or None when it could not be asked."""
        info = _BasicAccounting()
        if not _k32.QueryInformationJobObject(job, _BASIC_ACCOUNTING, ctypes.byref(info), ctypes.sizeof(info), None):
            return None
        return info.ActiveProcesses

    def peek(self, pipe):
        """-> ("data", n bytes waiting), ("empty", 0), ("eof", 0) (every writer has closed) or ("error", number)."""
        waiting = wintypes.DWORD()
        if not _k32.PeekNamedPipe(pipe, None, 0, None, ctypes.byref(waiting), None):
            error = ctypes.get_last_error()
            return ("eof", 0) if error == _ERROR_BROKEN_PIPE else ("error", error)
        return ("data", waiting.value) if waiting.value else ("empty", 0)

    def read(self, pipe, count):
        """Exactly what is waiting (never more than `count`), or None. It cannot block: the bytes are already there."""
        buffer = ctypes.create_string_buffer(count)
        got = wintypes.DWORD()
        if not _k32.ReadFile(pipe, buffer, count, ctypes.byref(got), None):
            return None
        return buffer.raw[:got.value]

    def wait(self, handle, milliseconds):
        answer = _k32.WaitForSingleObject(handle, milliseconds)
        return "signaled" if answer == _WAIT_SIGNALED else "timeout" if answer == _WAIT_TIMEOUT else "failed"

    def exit_code(self, process):
        code = wintypes.DWORD()
        return code.value if _k32.GetExitCodeProcess(process, ctypes.byref(code)) else None

    def close(self, handle):
        return bool(_k32.CloseHandle(handle))


class Result:
    """What a read came to: a closed word, the bytes only when the word is `ok`, and whether the end of the job was
    confirmed. A read whose end was not confirmed is never `ok`."""

    __slots__ = ("word", "data", "cleanup_confirmed")

    def __init__(self, word, data=None, cleanup_confirmed=True):
        self.word, self.data, self.cleanup_confirmed = word, data, cleanup_confirmed

    def __repr__(self):
        return "Result(%s, confirmed=%s)" % (self.word, self.cleanup_confirmed)


def environment_block(extra=(), inherited=None):
    """The program's whole environment as one Windows block (`NAME=value` entries, each ended by a NUL, sorted by name
    without regard to case, the whole ended by one more NUL). Only the listed names are taken from the reporter's own
    environment (unlisted variables are excluded), then the operator's `extra` entries are merged by name without regard
    to case, and the operator's value always wins. A value with a NUL is refused (ValueError). Never a secret by itself:
    a listed variable's value is whatever the reporter's environment holds, which the operator controls."""
    inherited = os.environ if inherited is None else inherited
    merged = {}
    for name in INHERITED:
        for have, value in inherited.items():
            if have.upper() == name:
                merged[name] = (have, value)
    for name, value in extra:
        merged[name.upper()] = (name, value)
    entries = []
    for key in sorted(merged):
        name, value = merged[key]
        if "\x00" in name or "\x00" in value or "=" in name or not name:
            raise ValueError("an environment entry has a character it may not have")
        entries.append("%s=%s" % (name, value))
    return "\x00".join(entries) + "\x00\x00" if entries else "\x00\x00"


class _Owned:
    """What a run owns, closed and ended in one place (so that an exception anywhere still ends everything)."""

    def __init__(self, win):
        self.win = win
        self.handles = {}
        self.attributes = None          # the attribute list, freed at the end
        self.values = []                # the arrays the list points to: they live as long as the list does (see _finish)
        self.created = False            # a process exists (frozen or running): the job must be ended and confirmed
        self.ended_alone = True         # False when ending the process by its own handle failed

    def keep(self, label, handle):
        self.handles[label] = handle
        return handle

    def close(self, label):
        handle = self.handles.pop(label, None)
        if handle is not None:
            self.win.close(handle)


def run(command, after=None, limit=None, *, timeout_seconds, max_bytes, env=(), stop=None, clock=time.monotonic,
        sleep=time.sleep, win=None, mode=MODE_RECEIPTS):
    """Run `command` (a list: the absolute path of an .exe and its arguments) with `--after N --limit M --json` appended
    last (mode `receipts`, the default) or with nothing appended (mode `report`), under one deadline of `timeout_seconds`, and
    read at most `max_bytes` of its output. -> Result. An unknown mode, a `receipts` run without whole `after` and `limit`, or a
    `report` run whose command does not end with `REPORT_TAIL`, is `cannot_run` and nothing is started.

    The environment is `environment_block(env)`. The working directory is the folder of the program file. Standard input
    and standard error are the NUL device. `stop` is anything with `is_set()`; when it is set the read ends as
    `interrupted`. Nothing the program printed is kept in any word."""
    if sys.platform != "win32":
        return Result(CANNOT_RUN)
    win = _Win() if win is None else win
    program = command[0]
    try:
        block = environment_block(env)
        if mode == MODE_REPORT:
            if tuple(command[-len(REPORT_TAIL):]) != REPORT_TAIL:
                return Result(CANNOT_RUN)
            line = cmdline.quote_command(list(command))
        elif mode == MODE_RECEIPTS and type(after) is int and type(limit) is int:
            line = cmdline.quote_command(list(command) + ["--after", str(after), "--limit", str(limit), "--json"])
        else:
            return Result(CANNOT_RUN)
    except ValueError:
        return Result(CANNOT_RUN)
    if "\x00" in line or not os.path.isfile(program):
        return Result(CANNOT_RUN)
    owned = _Owned(win)
    word, data = CANNOT_CONTAIN, None
    try:
        word, data = _start(win, owned, program, line, block, clock, timeout_seconds, max_bytes, stop, sleep)
    finally:
        confirmed = _finish(win, owned, clock, sleep)
    if word == OK and not confirmed:
        return Result(CLEANUP_INCOMPLETE, None, False)
    return Result(word, data if word == OK else None, confirmed)


def _start(win, owned, program, line, block, clock, timeout_seconds, max_bytes, stop, sleep):
    """Set up, start, resume and read. -> (word, data)."""
    job = owned.keep("job", win.create_job())
    if job is None or not win.set_limits(job, JOB_PROCESS_LIMIT):
        return CANNOT_CONTAIN, None
    pipe = win.create_pipe(PIPE_BYTES)
    if pipe is None:
        return CANNOT_RUN, None
    read, write = owned.keep("read", pipe[0]), owned.keep("write", pipe[1])
    if not win.make_private(read):
        return CANNOT_RUN, None
    nul = owned.keep("nul", win.open_nul())
    if nul is None:
        return CANNOT_RUN, None
    attributes = owned.attributes = win.init_attributes(2)
    if attributes is None:
        return CANNOT_CONTAIN, None
    # The attribute list keeps POINTERS to these arrays until it is deleted, so they belong to the owner, are stored before
    # either is handed over, and are released only after `delete_attributes` has returned (in `_finish`).
    inherited = (ctypes.c_void_p * 2)(nul, write)          # only these two handles go to the program
    jobs = (ctypes.c_void_p * 1)(job)                      # and it is created inside this job
    owned.values.extend((inherited, jobs))
    if not win.update_attribute(attributes, _HANDLE_LIST, inherited) \
            or not win.update_attribute(attributes, _JOB_LIST, jobs):
        return CANNOT_CONTAIN, None                        # never create-then-assign: the program is not created
    made, error = win.create_process(program, line, block, os.path.dirname(program), nul, write, nul, attributes)
    if made is None:
        return (CANNOT_RUN if error in _ERRORS_PROGRAM_CANNOT_START else CANNOT_CONTAIN), None
    process, thread, _id = made
    owned.created = True
    owned.keep("process", process)
    owned.keep("thread", thread)
    owned.close("write")                                   # our copies: the output ends when the program's end
    owned.close("nul")
    if win.is_process_in_job(process, job) is not True or win.resume_thread(thread) != 1:
        # Not proven to be inside its job, or it could not be started: it never ran. End it by its own handle too (it
        # may not be in the job at all, so ending the job alone would not reach it).
        owned.ended_alone = win.terminate_process(process)
        return CANNOT_CONTAIN, None
    owned.close("thread")
    return _read(win, read, process, clock, timeout_seconds, max_bytes, stop, sleep)


def _read(win, pipe, process, clock, timeout_seconds, max_bytes, stop, sleep):
    """Read the output without ever blocking, until the end, a deadline, a stop, a limit or a failure."""
    deadline = clock() + timeout_seconds
    chunks, size = [], 0
    finished = exited = False
    grace_end = None
    while True:
        if stop is not None and stop.is_set():
            return INTERRUPTED, None
        now = clock()
        if now >= deadline:
            return TIMEOUT, None
        if not exited:
            # On EVERY pass, also the ones that just read data: has the main program exited? (a wait of zero time)
            answer = win.wait(process, 0)
            if answer == "failed":
                return READ_FAILED, None
            if answer == "signaled":
                exited = True
                grace_end = min(deadline, now + LINGER_SECONDS)        # ONE grace deadline, never past the deadline
        kind, count = win.peek(pipe)
        if kind == "error":
            return READ_FAILED, None
        if kind == "eof":
            finished = True
        elif exited and clock() >= grace_end:
            return LEFTOVER, None                          # the pipe is still open after the grace, data or not: a helper
        elif kind == "data":                               # holds it, so the output is not the command's alone
            piece = win.read(pipe, min(count, max_bytes + 1 - size))
            if piece is None:
                return READ_FAILED, None
            chunks.append(piece)
            size += len(piece)
            if size > max_bytes:
                return TOO_LARGE, None
            continue                                       # more may be waiting: look again before any pause
        if exited and finished:
            code = win.exit_code(process)
            if code is None:
                return READ_FAILED, None
            return (OK, b"".join(chunks)) if code == 0 else (EXIT_CODE, None)
        if not exited:                                     # idle: a short wait that wakes at once when the program exits
            answer = win.wait(process, int(min(POLL_SECONDS, deadline - now) * 1000) or 1)
            if answer == "failed":
                return READ_FAILED, None
            if answer == "signaled":
                exited = True
                grace_end = min(deadline, clock() + LINGER_SECONDS)
        else:
            sleep(POLL_SECONDS)


def _finish(win, owned, clock, sleep):
    """End the job, confirm it, free what was made, close every handle. -> whether the end was confirmed. Never hides a
    failure: a failed end, a failed question, or an allowance that ran out is not a confirmation."""
    confirmed = True
    job = owned.handles.get("job")
    if owned.created and job is not None:
        confirmed = False
        if win.terminate_job(job):
            limit = clock() + CLEANUP_SECONDS
            while True:
                active = win.job_active(job)
                if active is None:
                    break
                if active == 0:
                    confirmed = True
                    break
                if clock() >= limit:
                    break
                sleep(POLL_SECONDS / 2)
    confirmed = confirmed and owned.ended_alone
    if owned.attributes is not None:
        win.delete_attributes(owned.attributes)
    owned.values.clear()                    # only now: the list is gone, nothing points at the arrays any more
    for label in ("read", "write", "nul", "thread", "process", "job"):      # the job last: kill-on-close is the net
        owned.close(label)
    return confirmed
