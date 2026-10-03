"""Card 7c-2, step 4: the wait between cycles while limit capture is on (decision D5).

The wrapper refreshes a park file about once a minute and the reporter counts a file as fresh for
300 seconds. While Leasewise is not answering, `run` used to wait up to 900 seconds between cycles,
which would let a park start and end between two looks. While limit capture is configured the wait is
capped at 60 seconds, even during an outage. Without limit sources the wait is exactly as before.
Synthetic data; a fake transport.
"""

import os
import threading
import unittest

from quota_balancer.reporter import run as run_mod

from tests.reporter_env import Rig
from tests.test_card7a3 import one_message
from tests.test_card7c2_capture import source
from tests.test_card7c2_delivery import Net


class RecordingStop(threading.Event):
    """A stop event that records how long the reporter asked to wait, then stops it."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.set()
        return True


class Wait(unittest.TestCase):
    def setUp(self):
        self.rig = Rig(server=False)
        self.addCleanup(self.rig.cleanup)
        one_message(self.rig.journal, self.rig.clock.ms)               # one lease message waits to be sent

    def waited(self, limit_sources, poll_seconds=5, failures=8):
        stop = RecordingStop()
        self.rig.close()                                                # a fresh reporter each time
        reporter = self.rig.open(stop=stop, transport=Net(lease=[(503, {})]))        # every lease request: 503
        reporter.limit_sources = tuple(limit_sources)
        reporter.config.poll_seconds = poll_seconds
        reporter.failures = failures                                                 # a long outage so far
        reporter.run()
        self.assertEqual(len(stop.waits), 1)
        return stop.waits[0]

    def sources(self):
        return (source(os.path.join(self.rig.ws.dir, "bus")),)

    def test_without_limit_sources_a_long_outage_still_waits_as_before(self):
        wait = self.waited(())
        self.assertGreater(wait, 60)
        self.assertLessEqual(wait, run_mod.MAX_SLEEP_SECONDS)

    def test_with_limit_sources_the_wait_is_capped_at_60_seconds_even_in_an_outage(self):
        self.assertEqual(self.waited(self.sources()), run_mod.LIMIT_CAPTURE_MAX_WAIT_SECONDS)
        self.assertEqual(run_mod.LIMIT_CAPTURE_MAX_WAIT_SECONDS, 60)

    def test_a_short_wait_is_left_alone(self):
        self.assertEqual(self.waited(self.sources(), failures=0), 5)               # the usual poll interval
        self.assertEqual(self.waited((), failures=0), 5)

    def test_a_slow_poll_setting_is_capped_too_while_capture_is_on(self):
        # poll_seconds above 60 would also let a park start and end between two looks
        self.assertEqual(self.waited(self.sources(), poll_seconds=300, failures=0), 60)

    def test_without_limit_sources_a_slow_poll_setting_is_not_capped(self):
        self.assertEqual(self.waited((), poll_seconds=300, failures=0), 300)


if __name__ == "__main__":
    unittest.main()
