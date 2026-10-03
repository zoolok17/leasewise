"""Card L6a-2, step 1: the per-key operating-system lock (DESIGN-6A 8.6, requirement 4).

64 stripe files at most; a non-blocking, bounded wait; the system releases a lock when its holder dies (a real child
killed by its exact process id); on Windows the lock is `msvcrt.locking` on one byte at a fixed offset. Synthetic keys.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

from quota_balancer.reporter import admission_lock as lock


class _Dir(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        self._dir = tempfile.TemporaryDirectory(dir=base, prefix="admission-lock-")
        self.addCleanup(self._dir.cleanup)
        self.store = os.path.join(self._dir.name, "admission.db")
        self.folder = os.path.join(self._dir.name, lock.FOLDER)


class Stripes(_Dir):

    def test_a_key_always_maps_to_the_same_one_of_64_stripes(self):
        self.assertEqual(lock.STRIPES, 64)
        first = lock.stripe("alpha", "m1")
        self.assertEqual(first, lock.stripe("alpha", "m1"))
        seen = {lock.stripe("agent%d" % n, "msg-%d" % n) for n in range(2000)}
        self.assertTrue(seen <= set(range(64)))
        self.assertGreater(len(seen), 50)                     # spread over the stripes

    def test_the_two_parts_of_a_key_cannot_be_confused(self):
        self.assertNotEqual(lock.lock_path(self.store, "a", "bc"), lock.lock_path(self.store, "ab", "c"))

    def test_only_64_lock_files_can_ever_exist_however_many_keys_are_locked(self):
        for number in range(300):
            held = lock.KeyLock(self.store, "agent%d" % (number % 7), "message-%d" % number)
            self.assertTrue(held.acquire(time.monotonic() + 1))
            held.release()
        names = sorted(os.listdir(self.folder))
        self.assertLessEqual(len(names), 64)
        self.assertTrue(all(name.startswith("lock-") and len(name) == 7 for name in names))
        self.assertTrue(all(os.path.getsize(os.path.join(self.folder, name)) == 0 for name in names))   # created empty
        self.assertEqual(os.listdir(self._dir.name).count(lock.FOLDER), 1)                  # beside the store file

    def test_the_lock_is_the_systems_own_kind_for_this_platform(self):
        self.assertEqual(lock.IMPLEMENTATION, "msvcrt.locking" if sys.platform == "win32" else "fcntl.flock")
        if sys.platform == "win32":
            self.assertEqual((lock.LOCK_OFFSET, lock.LOCK_LENGTH), (0, 1))       # one byte at a fixed offset


class Waiting(_Dir):

    def test_a_second_holder_waits_for_the_first_and_gets_it_when_it_is_released(self):
        first = lock.KeyLock(self.store, "alpha", "m1")
        second = lock.KeyLock(self.store, "alpha", "m1")
        self.assertTrue(first.acquire(time.monotonic() + 1))
        self.assertFalse(second.acquire(time.monotonic() + 0.2))
        first.release()
        self.assertTrue(second.acquire(time.monotonic() + 1))
        second.release()

    def test_the_wait_is_bounded_by_the_deadline_and_uses_the_clock_it_is_given(self):
        first = lock.KeyLock(self.store, "alpha", "m1")
        self.assertTrue(first.acquire(time.monotonic() + 1))
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds
        waiter = lock.KeyLock(self.store, "alpha", "m1", clock=lambda: now[0], sleep=sleep)
        self.assertFalse(waiter.acquire(110.0))                  # ten seconds of waiting, then it gives up
        self.assertAlmostEqual(sum(slept), 10.0, places=6)
        self.assertLessEqual(max(slept), lock.POLL_SECONDS)
        first.release()

    def test_a_key_that_shares_a_stripe_waits_but_other_stripes_do_not(self):
        first = lock.KeyLock(self.store, "alpha", "m1")
        self.assertTrue(first.acquire(time.monotonic() + 1))
        other = next(number for number in range(1000)
                     if lock.stripe("alpha", "x%d" % number) != lock.stripe("alpha", "m1"))
        free = lock.KeyLock(self.store, "alpha", "x%d" % other)
        self.assertTrue(free.acquire(time.monotonic() + 0.5))
        free.release()
        first.release()


HOLDER = r"""
import sys, time
from quota_balancer.reporter import admission_lock as lock
held = lock.KeyLock(sys.argv[1], "alpha", "m1")
assert held.acquire(time.monotonic() + 5)
print("held", flush=True)
time.sleep(60)
"""


class Death(_Dir):

    def test_a_killed_holder_releases_the_key_without_any_cleanup(self):
        child = subprocess.Popen([sys.executable, "-c", HOLDER, self.store], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        try:
            self.assertEqual(child.stdout.readline().strip(), b"held")
            mine = lock.KeyLock(self.store, "alpha", "m1")
            self.assertFalse(mine.acquire(time.monotonic() + 0.3))         # really held by the other process
            child.kill()                                                   # this child, by its exact process id
            child.wait(timeout=10)
            self.assertTrue(mine.acquire(time.monotonic() + 5))            # the system let go; nothing was cleaned up
            mine.release()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()
        self.assertEqual(len(os.listdir(self.folder)), 1)


if __name__ == "__main__":
    unittest.main()
