"""One operating-system lock per admission key (card L6a-2, DESIGN-6A 8.6 and requirement 4).

Two helpers asked about the same message must not both talk to Leasewise and save a grant at the same time. The lock
makes "look up, send or rebuild, save the grant" one section per message.

* **64 files, never more.** The key (agent and message id) is hashed with SHA-256 and reduced to one of 64 stripes:
  `lock-00` to `lock-3f` in a folder `admission.locks` beside the store file. A file is created empty on first use and
  never deleted. Two unrelated messages that share a stripe merely wait for each other.
* **The operating system owns it.** POSIX uses `fcntl.flock`; Windows uses `msvcrt.locking` on ONE byte at a fixed
  offset (byte 0), because that call does not lock "a whole file". Both are tried without blocking, in a loop that stops
  at the caller's deadline. When a process exits or dies the system releases its lock, so a crash can never leave a key
  locked and nothing is ever cleaned up by hand.
* **Not fair.** Waiting is bounded, not ordered: under heavy contention a helper may give up and answer "busy".
"""

import hashlib
import os
import sys
import time

STRIPES = 64
FOLDER = "admission.locks"
POLL_SECONDS = 0.05

if sys.platform == "win32":
    import msvcrt
    IMPLEMENTATION = "msvcrt.locking"
    LOCK_OFFSET = 0                         # the fixed place of the one locked byte
    LOCK_LENGTH = 1

    def _try_lock(fd):
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, LOCK_LENGTH)
        except OSError:
            return False
        return True

    def _unlock(fd):
        os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, LOCK_LENGTH)
        except OSError:
            pass
else:
    import fcntl
    IMPLEMENTATION = "fcntl.flock"

    def _try_lock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(fd):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


def stripe(agent, message_id):
    """0 to 63, from the SHA-256 of the key. The separator cannot appear in either part."""
    digest = hashlib.sha256(("%s\n%s" % (agent, message_id)).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % STRIPES


def lock_path(store_path, agent, message_id):
    return os.path.join(os.path.dirname(os.path.abspath(store_path)), FOLDER,
                        "lock-%02x" % stripe(agent, message_id))


class KeyLock:
    """`acquire(deadline)` -> True when the key's lock is held, False when the deadline came first. `deadline` is a
    time on `clock` (the monotonic clock by default). Use as `if lock.acquire(...)` then `lock.release()`."""

    def __init__(self, store_path, agent, message_id, clock=time.monotonic, sleep=time.sleep):
        self._path = lock_path(store_path, agent, message_id)
        self._clock = clock
        self._sleep = sleep
        self._fd = None

    def acquire(self, deadline):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)     # not inherited by a child process
        while True:
            if _try_lock(fd):
                self._fd = fd
                return True
            if self._clock() >= deadline:
                os.close(fd)
                return False
            self._sleep(min(POLL_SECONDS, max(0.0, deadline - self._clock())))

    def release(self):
        if self._fd is not None:
            _unlock(self._fd)
            os.close(self._fd)
            self._fd = None
