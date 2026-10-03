"""Card 7c-2, fix round 2, item 3: the cycle summary does not depend on which pass ran last.

An unresolved credential refusal (401) from EITHER sending pass stays visible: credential ranks above
unreachable above nothing. Synthetic data only."""

import os
import tempfile
import types
import unittest

from quota_balancer.reporter import client
from quota_balancer.reporter.run import CREDENTIAL, UNREACHABLE, Reporter
from quota_balancer.reporter.state import State

from tests.test_card7c2_state import NOW, body


class CycleSummary(unittest.TestCase):
    """Both passes fail; the summary must not hide a login problem whichever pass ran last."""

    def run_cycle(self, limit_status, lease_status):
        base = os.environ.get("AGENTTALK_SCRATCH") or None
        if base:
            os.makedirs(base, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, prefix="quota-7c2sum-") as tmp:
            journal = os.path.join(tmp, "journal")
            os.makedirs(journal)
            state = State(os.path.join(tmp, "r.db"))
            try:
                state.add_limit_event("synthetic-account", body(), NOW)
                state.conn.execute("INSERT INTO acquisition(agent, message_id, turn_uuid, request_id, "
                                   "body_original, body_current) VALUES('alpha','m1','t1','r1','{}','{}')")
                calls = []

                def transport(method, path, headers, raw, timeout):
                    calls.append(path)
                    return (limit_status if path == "/v1/limit-events" else lease_status), b"{}"
                config = types.SimpleNamespace(agents={}, journal_root=journal, silent_after_seconds=86400,
                                               machine="desktop", poll_seconds=5)
                reporter = Reporter(config, {}, state, client.HttpClient("http", "127.0.0.1", 9, "synthetic",
                                                                         transport=transport),
                                    clock=lambda: NOW, rng=lambda: 0.5)
                result = reporter.cycle()
                return {"result": result, "calls": calls, "failures": reporter.failures,
                        "limit": state.one("SELECT state, tries FROM limit_event"),
                        "lease": state.one("SELECT state, tries FROM acquisition"),
                        "note": state.note("credential_active"), "dict": result.as_dict()}
            finally:
                state.close()

    def test_a_credential_refusal_from_either_pass_stays_visible_in_every_order(self):
        for limit_status, lease_status in ((401, 503), (503, 401), (401, 401)):
            with self.subTest(limit=limit_status, lease=lease_status):
                out = self.run_cycle(limit_status, lease_status)
                self.assertEqual(out["calls"], ["/v1/limit-events", "/v1/leases"])        # both passes ran
                self.assertEqual(out["result"].blocked, CREDENTIAL)
                self.assertEqual(out["dict"]["blocked"], CREDENTIAL)
                self.assertIsNotNone(out["note"])                                          # the note is kept
                self.assertEqual((out["limit"], out["lease"]), (("pending", 1), ("pending", 1)))
                self.assertEqual(out["failures"], 1)

    def test_two_unreachable_passes_say_unreachable_and_leave_no_credential_note(self):
        out = self.run_cycle(503, 503)
        self.assertEqual(out["result"].blocked, UNREACHABLE)
        self.assertIsNone(out["note"])
        self.assertEqual((out["limit"], out["lease"]), (("pending", 1), ("pending", 1)))
        self.assertEqual(out["failures"], 1)


if __name__ == "__main__":
    unittest.main()
