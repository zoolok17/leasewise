"""Card 2b-3: the pure OVH billing adapter (docs/DESIGN.md section 11).

Fixtures only (docs/fixtures/ovh/): two REAL sanitized captures and
clearly-named SYNTHETIC files. No network, no real key: the credential values
below are obviously fake and the transport is an in-memory fake.

Rows: billing_adapter, billing_non_ai, billing_poll_schedule, billing_tls,
billing_output_errors, privacy_2b (this card), plus one end-to-end test
through the existing reading-import path.
"""

import copy
import datetime
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO_ROOT, "docs", "fixtures", "ovh")
PYTHON = os.environ.get("AGENTTALK_PY") or sys.executable

from quota_balancer import ovh_adapter as ovh  # noqa: E402
from quota_balancer.errors import (  # noqa: E402
    CODE_INVALID_CONFIG,
    QuotaError,
)
from quota_balancer.validate_readings import validate_readings  # noqa: E402


def _ms(*args):
    return int(datetime.datetime(*args, tzinfo=datetime.timezone.utc)
               .timestamp() * 1000)


# The real capture: taken 2026-10-02T19:23:36Z; lastUpdate 19:09:51.739Z.
NOW_MS = _ms(2026, 10, 2, 19, 23, 36)
LAST_UPDATE_MS = _ms(2026, 10, 2, 19, 9, 51) + 739
OCT = _ms(2026, 10, 1)
NOV = _ms(2026, 11, 1)
REAL_EVENT_ID = "de7a8837-7c95-5282-9cae-75279c7b593c"

# Obviously fake credential values (sentinels for the privacy row).
APP_KEY = "FAKE-APP-KEY-SENTINEL"
APP_SECRET = "FAKE-APP-SECRET-SENTINEL"
CONSUMER_KEY = "FAKE-CONSUMER-KEY-SENTINEL"
PROJECT_ID = "fakeproject01"
BASE_URL = "https://api.example.invalid/1.0"
SECRETS = (APP_KEY, APP_SECRET, CONSUMER_KEY, PROJECT_ID, "api.example.invalid")


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def _fixture_json(name):
    return json.loads(_fixture_bytes(name))


def real_body():
    body = _fixture_json("usage-current-2026-10-02T192336Z.sanitized.json")
    body.pop("_capture")  # the capturer's header, not part of the OVH body
    return body


def dump(body):
    return json.dumps(body).encode("utf-8")


class FakeTransport:
    """In-memory transport: records every call, answers with `response` or
    raises `exc`. `verifies_tls` is what the module checks before calling."""

    def __init__(self, response=None, exc=None, verifies_tls=True):
        self.response = response
        self.exc = exc
        self.verifies_tls = verifies_tls
        self.calls = []

    def get(self, url, headers, timeout_seconds):
        self.calls.append((url, dict(headers), timeout_seconds))
        if self.exc is not None:
            raise self.exc
        return self.response


def ok(body=None, headers=None):
    return ovh.TransportResponse(
        200, headers or {}, dump(real_body() if body is None else body))


def answer(status, body=b"", headers=None):
    return ovh.TransportResponse(status, headers or {}, body)


def credential():
    return ovh.OvhCredential(APP_KEY, APP_SECRET, CONSUMER_KEY, PROJECT_ID)


def config(**kw):
    values = dict(machine="desktop", account="meter", scope_alias="proj-ai",
                  scope_revision=1)
    values.update(kw)
    return ovh.AdapterConfig(**values)


def run(transport, now_ms=NOW_MS, jitter=0, poll_seconds=900, cfg=None,
        base_url=BASE_URL, cred=None):
    return ovh.poll_once(
        credential=cred or credential(), transport=transport,
        clock=lambda: now_ms, jitter_seconds=jitter,
        config=cfg or config(), poll_seconds=poll_seconds,
        base_url=base_url)


def run_body(body, **kw):
    return run(FakeTransport(ok(body)), **kw)


def mutated(fn):
    body = real_body()
    fn(body)
    return body


def failure_of(result):
    assert set(result) == {"failure", "next_attempt_at"}, result
    return result["failure"]


class BillingAdapter(unittest.TestCase):
    """billing_adapter: the real capture -> the exact document."""

    def test_real_capture_exact_document(self):
        transport = FakeTransport(ok())
        result = run(transport)
        self.assertEqual(set(result), {"document", "next_attempt_at"})
        self.assertEqual(result["document"], {
            "version": 3,
            "readings": [{
                "event_id": REAL_EVENT_ID,
                "machine": "desktop",
                "account": "meter",
                "kind": "provider_billing",
                "observed_at": LAST_UPDATE_MS,
                "payload": {
                    "scope_alias": "proj-ai",
                    "scope_revision": 1,
                    "period_start": OCT,
                    "period_end": NOV,
                    "currency": "EUR",
                    "total_micro_eur": 6737286,
                    "non_ai_spend_present": False,
                },
            }],
        })
        self.assertEqual(result["next_attempt_at"], NOW_MS + 900 * 1000)

    def test_document_passes_the_existing_validator(self):
        document = run(FakeTransport(ok()))["document"]
        readings = validate_readings(copy.deepcopy(document), NOW_MS)
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["total_micro_eur"], 6737286)

    def test_observed_at_is_last_update_not_poll_time(self):
        later = run(FakeTransport(ok()), now_ms=NOW_MS + 60_000)
        self.assertEqual(later["document"]["readings"][0]["observed_at"],
                         LAST_UPDATE_MS)

    def test_unchanged_poll_is_byte_identical(self):
        first = run(FakeTransport(ok()), now_ms=NOW_MS)
        second = run(FakeTransport(ok()), now_ms=NOW_MS + 3_600_000)
        self.assertEqual(json.dumps(first["document"], sort_keys=True),
                         json.dumps(second["document"], sort_keys=True))

    def test_event_id_is_a_lowercase_uuid_over_the_canonical_values(self):
        event_id = run(FakeTransport(ok()))["document"]["readings"][0][
            "event_id"]
        self.assertEqual(event_id, event_id.lower())
        self.assertEqual(str(uuid.UUID(event_id)), event_id)
        self.assertEqual(uuid.UUID(event_id).version, 5)

    def test_each_identity_field_changes_the_event_id(self):
        base = run(FakeTransport(ok()))["document"]["readings"][0]["event_id"]

        def eid(result):
            return result["document"]["readings"][0]["event_id"]

        for cfg in (config(machine="worker"), config(account="meter2"),
                    config(scope_alias="proj-b"), config(scope_revision=2)):
            self.assertNotEqual(eid(run(FakeTransport(ok()), cfg=cfg)), base)
        # total, observed_at and non_ai each move the id as well.
        bumped = mutated(lambda b: b["totalPrice"].update(
            priceInUcents=673728700, value=6.737287))
        bumped["resourcesUsage"][0]["totalPrice"] = 6.737287
        self.assertNotEqual(eid(run_body(bumped)), base)
        later = mutated(lambda b: b.update(lastUpdate="2026-10-02T19:10:00.000Z"))
        self.assertNotEqual(eid(run_body(later)), base)
        non_ai = json.loads(_fixture_bytes(
            "usage-current-synthetic-non-ai-spend.json"))
        self.assertNotEqual(eid(run_body(non_ai)), base)

    def test_fractional_ucents_round_up(self):
        # 673728601 ucents = 6737286.01 micro-EUR -> 6737287; 99 stays 1.
        for ucents, expected in ((673728600, 6737286), (673728601, 6737287),
                                 (673728650, 6737287), (673728699, 6737287),
                                 (673728700, 6737287)):
            body = mutated(lambda b, u=ucents: b["totalPrice"].update(
                priceInUcents=u))
            result = run_body(body)
            self.assertEqual(
                result["document"]["readings"][0]["payload"][
                    "total_micro_eur"], expected, ucents)

    def test_zero_total_is_zero(self):
        def zero(b):
            b["totalPrice"].update(priceInUcents=0, value=0)
            b["resourcesUsage"] = []
        result = run_body(mutated(zero))
        self.assertEqual(result["document"]["readings"][0]["payload"][
            "total_micro_eur"], 0)

    def test_money_is_never_a_binary_float(self):
        # A value whose float image is inexact still passes the Decimal
        # consistency check at its exact digits.
        def odd(b):
            b["totalPrice"].update(priceInUcents=10_000_001, value=0.100001)
            b["resourcesUsage"][0]["totalPrice"] = 0.100001
            b["resourcesUsage"][0]["resources"] = []
        result = run_body(mutated(odd))
        self.assertEqual(result["document"]["readings"][0]["payload"][
            "total_micro_eur"], 100001)

    def test_price_in_ucents_must_be_an_exact_nonnegative_int64(self):
        for bad in (True, False, -1, 1.5, "673728600", None,
                    2 ** 63, 673728600.0):
            body = mutated(lambda b, v=bad: b["totalPrice"].update(
                priceInUcents=v))
            self.assertEqual(failure_of(run_body(body)), "schema", bad)
        for bad_raw in (b'673728600.0', b'6.737286e8'):
            raw = dump(real_body()).replace(b"673728600", bad_raw)
            result = run(FakeTransport(ok_raw(raw)))
            self.assertEqual(failure_of(result), "schema", bad_raw)

    def test_int64_ceiling_amount(self):
        top = 2 ** 63 - 1
        def big(b):
            b["totalPrice"].update(priceInUcents=top, value=top / 10 ** 8)
        # value as a float cannot be exact here: inconsistent -> schema,
        # never an overflow or a wrong number.
        self.assertEqual(failure_of(run_body(mutated(big))), "schema")

    def test_currency_other_than_eur_fails_currency(self):
        for code in ("USD", "eur", "EUR ", ""):
            body = mutated(lambda b, c=code: b["totalPrice"].update(
                currencyCode=c))
            self.assertEqual(failure_of(run_body(body)), "currency", code)
        for bad in (None, 5, ["EUR"]):
            body = mutated(lambda b, c=bad: b["totalPrice"].update(
                currencyCode=c))
            self.assertEqual(failure_of(run_body(body)), "schema", bad)
        body = mutated(lambda b: b["totalPrice"].pop("currencyCode"))
        self.assertEqual(failure_of(run_body(body)), "schema")

    # --- consistency (lead decision 2): both boundaries -------------------

    def test_value_agrees_with_ucents_within_one_micro_eur(self):
        # 673728600 ucents = 6.737286 EUR. +-1 micro-EUR passes, beyond fails.
        for value, expected in ((6.737286, True), (6.737287, True),
                                (6.737285, True), (6.7372871, False),
                                (6.7372849, False), (6.74, False)):
            body = mutated(lambda b, v=value: b["totalPrice"].update(value=v))
            # keep the category sum equal to the value being tested
            body["resourcesUsage"][0]["totalPrice"] = value
            result = run_body(body)
            self.assertEqual("document" in result, expected, value)
            if not expected:
                self.assertEqual(failure_of(result), "schema", value)

    def test_category_sum_agrees_with_value_within_one_micro_per_entry(self):
        # One entry: tolerance 1 micro-EUR.
        for amount, expected in ((6.737286, True), (6.737287, True),
                                 (6.737285, True), (6.7372871, False),
                                 (6.7372849, False)):
            body = mutated(lambda b, a=amount: b["resourcesUsage"][0].update(
                totalPrice=a))
            self.assertEqual("document" in run_body(body), expected, amount)
        # Two entries: the tolerance is 2 micro-EUR in total.
        def two(delta):
            def f(b):
                first = b["resourcesUsage"][0]
                b["resourcesUsage"].append(
                    {"type": "synthetic-zero", "totalPrice": delta,
                     "resources": []})
            return mutated(f)
        self.assertIn("document", run_body(two(0.000002)))
        self.assertEqual(failure_of(run_body(two(0.0000021))), "schema")
        # No entries at all: a positive total is unexplained.
        def none(b):
            b["resourcesUsage"] = []
        self.assertEqual(failure_of(run_body(mutated(none))), "schema")

    def test_value_must_be_a_finite_nonnegative_number(self):
        for bad in (True, "6.737286", None, -6.737286):
            body = mutated(lambda b, v=bad: b["totalPrice"].update(value=v))
            self.assertEqual(failure_of(run_body(body)), "schema", bad)
        body = mutated(lambda b: b["totalPrice"].pop("value"))
        self.assertEqual(failure_of(run_body(body)), "schema")

    # --- time and period (lead decision 4) --------------------------------

    def test_last_update_missing_or_unparsable_is_schema(self):
        for bad in ("yesterday", "2026-10-02", "2026-10-02T19:09:51.739+02:00",
                    "2026-13-02T19:09:51.739Z", "2026-10-02T25:09:51.739Z",
                    "", 1790968191739, None, True):
            body = mutated(lambda b, v=bad: b.update(lastUpdate=v))
            self.assertEqual(failure_of(run_body(body)), "schema", bad)
        body = mutated(lambda b: b.pop("lastUpdate"))
        self.assertEqual(failure_of(run_body(body)), "schema")

    def test_last_update_without_fraction_or_with_micros_parses(self):
        for text, ms in (("2026-10-02T19:09:51Z", LAST_UPDATE_MS - 739),
                         ("2026-10-02T19:09:51.7Z", LAST_UPDATE_MS - 39),
                         ("2026-10-02T19:09:51.739123Z", LAST_UPDATE_MS)):
            body = mutated(lambda b, t=text: b.update(lastUpdate=t))
            result = run_body(body)
            self.assertEqual(
                result["document"]["readings"][0]["observed_at"], ms, text)

    def test_period_must_be_exactly_one_utc_month(self):
        cases = (
            ("2026-10-01T00:00:00.000Z", "2026-12-01T00:00:00.000Z"),  # 2 mo
            ("2026-10-01T00:00:00.000Z", "2026-10-31T00:00:00.000Z"),
            ("2026-10-02T00:00:00.000Z", "2026-11-02T00:00:00.000Z"),
            ("2026-10-01T00:00:00.000Z", "2026-10-01T00:00:00.000Z"),
            ("2026-11-01T00:00:00.000Z", "2026-10-01T00:00:00.000Z"),
            ("2026-10-01T00:00:00.001Z", "2026-11-01T00:00:00.000Z"),
        )
        for start, end in cases:
            body = mutated(lambda b, s=start, e=end: b["period"].update(
                {"from": s, "to": e}))
            self.assertEqual(failure_of(run_body(body)), "period",
                             (start, end))

    def test_period_missing_or_unparsable_is_schema(self):
        for bad in (None, "x", 5, {}):
            body = mutated(lambda b, v=bad: b.update(period=v))
            self.assertEqual(failure_of(run_body(body)), "schema", bad)
        body = mutated(lambda b: b["period"].pop("to"))
        self.assertEqual(failure_of(run_body(body)), "schema")
        body = mutated(lambda b: b["period"].update({"from": "nope"}))
        self.assertEqual(failure_of(run_body(body)), "schema")

    def test_december_period_rolls_into_january(self):
        def dec(b):
            b["period"].update({"from": "2026-12-01T00:00:00.000Z",
                                "to": "2027-01-01T00:00:00.000Z"})
            b["lastUpdate"] = "2026-12-15T10:00:00.000Z"
        result = run_body(mutated(dec), now_ms=_ms(2026, 12, 15, 10, 5))
        payload = result["document"]["readings"][0]["payload"]
        self.assertEqual(payload["period_start"], _ms(2026, 12, 1))
        self.assertEqual(payload["period_end"], _ms(2027, 1, 1))

    def test_last_update_outside_the_period_fails_period(self):
        for text in ("2026-09-30T23:59:59.999Z", "2026-11-01T00:00:00.001Z"):
            body = mutated(lambda b, t=text: b.update(lastUpdate=t))
            self.assertEqual(
                failure_of(run_body(body, now_ms=_ms(2026, 11, 2))),
                "period", text)

    def test_last_update_at_either_bound_is_allowed(self):
        # The start instant and (as the validator allows) the end instant.
        start = run_body(mutated(lambda b: b.update(
            lastUpdate="2026-10-01T00:00:00.000Z")))
        self.assertEqual(start["document"]["readings"][0]["observed_at"], OCT)
        end = run_body(mutated(lambda b: b.update(
            lastUpdate="2026-11-01T00:00:00.000Z")),
            now_ms=_ms(2026, 11, 1, 0, 5))
        self.assertEqual(end["document"]["readings"][0]["observed_at"], NOV)

    def test_last_update_more_than_30s_after_now_is_schema(self):
        body = real_body()
        self.assertIn("document", run_body(
            body, now_ms=LAST_UPDATE_MS - 30_000))
        self.assertEqual(failure_of(run_body(
            body, now_ms=LAST_UPDATE_MS - 30_001)), "schema")

    def test_unknown_extra_keys_are_ignored(self):
        body = mutated(lambda b: b.update(extraneous="x", _capture={"a": 1}))
        self.assertIn("document", run_body(body))


def ok_raw(raw):
    return ovh.TransportResponse(200, {}, raw)


class BillingNonAi(unittest.TestCase):
    """billing_non_ai: an exact boolean; an unseen shape is never false."""

    def flag(self, body):
        result = run_body(body)
        return result["document"]["readings"][0]["payload"][
            "non_ai_spend_present"]

    def test_ai_only_is_false(self):
        self.assertIs(self.flag(real_body()), False)

    def test_positive_non_ai_category_is_true(self):
        body = _fixture_json("usage-current-synthetic-non-ai-spend.json")
        result = run_body(body)
        payload = result["document"]["readings"][0]["payload"]
        self.assertIs(payload["non_ai_spend_present"], True)
        self.assertEqual(payload["total_micro_eur"], 8237286)

    def test_zero_non_ai_category_is_false(self):
        def zero(b):
            b["resourcesUsage"].append({"type": "synthetic-non-ai-category",
                                        "totalPrice": 0, "resources": []})
        self.assertIs(self.flag(mutated(zero)), False)
        def zero_float(b):
            b["resourcesUsage"].append({"type": "synthetic-non-ai-category",
                                        "totalPrice": 0.0, "resources": []})
        self.assertIs(self.flag(mutated(zero_float)), False)

    def test_smallest_positive_non_ai_is_true(self):
        def tiny(b):
            b["resourcesUsage"].append({"type": "synthetic-non-ai-category",
                                        "totalPrice": 0.000001,
                                        "resources": []})
        self.assertIs(self.flag(mutated(tiny)), True)

    def test_non_empty_hourly_element_is_schema_never_false(self):
        body = _fixture_json("usage-current-synthetic-hourly-nonempty.json")
        self.assertEqual(failure_of(run_body(body)), "schema")

    def test_any_non_empty_array_anywhere_is_schema(self):
        places = (
            lambda b: b["hourlyUsage"]["instance"].append({"x": 1}),
            lambda b: b["hourlyUsage"]["quantum"]["notebook"].append(1),
            lambda b: b["monthlyUsage"]["savingsPlan"].append({}),
            lambda b: b["monthlyUsage"].update(newKind=[{"a": 1}]),
            lambda b: b["hourlyUsage"]["quantum"].update(deep={"k": [1]}),
        )
        for place in places:
            self.assertEqual(failure_of(run_body(mutated(place))), "schema")

    def test_missing_or_misshapen_usage_sections_are_schema(self):
        for section in ("hourlyUsage", "monthlyUsage"):
            for bad in (None, [], "x", 5):
                body = mutated(lambda b, s=section, v=bad: b.update({s: v}))
                self.assertEqual(failure_of(run_body(body)), "schema",
                                 (section, bad))
            body = mutated(lambda b, s=section: b.pop(s))
            self.assertEqual(failure_of(run_body(body)), "schema", section)
        body = mutated(lambda b: b["hourlyUsage"].update(instance=5))
        self.assertEqual(failure_of(run_body(body)), "schema")

    def test_bad_category_entries_are_schema(self):
        for entry in ({"totalPrice": 1.0},                      # no type
                      {"type": 5, "totalPrice": 1.0},
                      {"type": "ai-endpoints"},                  # no price
                      {"type": "ai-endpoints", "totalPrice": "1"},
                      {"type": "ai-endpoints", "totalPrice": True},
                      {"type": "ai-endpoints", "totalPrice": None},
                      {"type": "ai-endpoints", "totalPrice": -1.0},
                      "string", None, 5):
            def put(b, e=entry):
                b["resourcesUsage"] = [e]
            self.assertEqual(failure_of(run_body(mutated(put))), "schema",
                             entry)
        for bad in (None, {}, "x"):
            body = mutated(lambda b, v=bad: b.update(resourcesUsage=v))
            self.assertEqual(failure_of(run_body(body)), "schema", bad)
        body = mutated(lambda b: b.pop("resourcesUsage"))
        self.assertEqual(failure_of(run_body(body)), "schema")

    def test_components_are_not_needed(self):
        def strip(b):
            for entry in b["resourcesUsage"]:
                entry.pop("resources")
        self.assertIs(self.flag(mutated(strip)), False)
        def garbage(b):
            b["resourcesUsage"][0]["resources"] = "not even a list"
        self.assertIs(self.flag(mutated(garbage)), False)

    def test_no_token_counts_in_the_document(self):
        text = json.dumps(run_body(real_body())["document"])
        for needle in ("input_tokens", "output_tokens", "qwen", "189808",
                       "15562011", "components"):
            self.assertNotIn(needle, text)


class BillingPollSchedule(unittest.TestCase):
    """billing_poll_schedule: bounded jitter, valid Retry-After precedence,
    exactly one transport call, no retry."""

    def test_jitter_bounds(self):
        for jitter in (0, 1, 89, 90):
            result = run(FakeTransport(ok()), jitter=jitter)
            self.assertEqual(result["next_attempt_at"],
                             NOW_MS + 900_000 + jitter * 1000)

    def test_jitter_out_of_range_is_a_typed_error_before_any_call(self):
        for bad in (-1, 91, 10 ** 9, True, 1.0, "5", None):
            transport = FakeTransport(ok())
            with self.assertRaises(QuotaError) as caught:
                run(transport, jitter=bad)
            self.assertEqual(caught.exception.code, CODE_INVALID_CONFIG)
            self.assertEqual(transport.calls, [])

    def test_poll_seconds_must_be_a_positive_int(self):
        for bad in (0, -1, True, 1.5, "900", None):
            transport = FakeTransport(ok())
            with self.assertRaises(QuotaError):
                run(transport, poll_seconds=bad)
            self.assertEqual(transport.calls, [])

    def test_poll_seconds_scales_the_schedule(self):
        result = run(FakeTransport(ok()), poll_seconds=60, jitter=7)
        self.assertEqual(result["next_attempt_at"], NOW_MS + 67_000)

    def test_retry_after_seconds_wins_when_longer(self):
        transport = FakeTransport(answer(429, b"{}", {"Retry-After": "1200"}))
        result = run(transport, jitter=10)
        self.assertEqual(result, {"failure": "rate_limit",
                                  "next_attempt_at": NOW_MS + 1_200_000})

    def test_retry_after_header_name_is_case_insensitive(self):
        transport = FakeTransport(answer(429, b"{}", {"retry-after": "1200"}))
        self.assertEqual(run(transport)["next_attempt_at"],
                         NOW_MS + 1_200_000)

    def test_retry_after_http_date(self):
        target = _ms(2026, 10, 2, 20, 0, 0)  # 36m24s after now > 900s
        transport = FakeTransport(answer(
            429, b"{}", {"Retry-After": "Fri, 02 Oct 2026 20:00:00 GMT"}))
        self.assertEqual(run(transport)["next_attempt_at"], target)

    def test_retry_after_shorter_than_the_poll_is_ignored(self):
        transport = FakeTransport(answer(429, b"{}", {"Retry-After": "30"}))
        self.assertEqual(run(transport, jitter=5)["next_attempt_at"],
                         NOW_MS + 905_000)
        past = FakeTransport(answer(
            429, b"{}", {"Retry-After": "Fri, 02 Oct 2026 19:00:00 GMT"}))
        self.assertEqual(run(past)["next_attempt_at"], NOW_MS + 900_000)

    def test_invalid_retry_after_is_ignored(self):
        for bad in ("", "soon", "-5", "1.5", "1e3", "+30", " ", "0x10",
                    "Fri, 99 Oct 2026 20:00:00 GMT",
                    "Fri, 02 Oct 2026 20:00:00"):  # no zone: not an HTTP-date
            transport = FakeTransport(answer(429, b"{}", {"Retry-After": bad}))
            self.assertEqual(
                run(transport, jitter=3),
                {"failure": "rate_limit",
                 "next_attempt_at": NOW_MS + 903_000}, bad)

    def test_retry_after_counts_only_on_429(self):
        header = {"Retry-After": "7200"}
        for response in (ok(headers=header), answer(503, b"", header),
                         answer(401, b"{}", header), answer(404, b"{}", header)):
            result = run(FakeTransport(response))
            self.assertEqual(result["next_attempt_at"], NOW_MS + 900_000)

    def test_valid_retry_after_beyond_a_day_is_respected(self):
        # Fix round 1 finding 4 (lead decision 6): max(now+poll+jitter, any
        # valid Retry-After); there is no 24 h cap.
        for header, expected in (
                ("86400", NOW_MS + 86_400_000),
                ("172800", NOW_MS + 172_800_000),
                ("Sun, 04 Oct 2026 19:23:36 GMT", NOW_MS + 172_800_000),
                ("Thu, 31 Dec 2026 23:59:59 GMT", _ms(2026, 12, 31, 23, 59, 59))):
            transport = FakeTransport(answer(429, b"{}",
                                             {"Retry-After": header}))
            self.assertEqual(run(transport)["next_attempt_at"], expected,
                             header)

    def test_absurd_retry_after_clamps_to_int64_never_earlier(self):
        top = 2 ** 63 - 1
        for header in ("9" * 40, "9" * 5000, "9223372036854775807",
                       str((top - NOW_MS) // 1000 + 1)):
            transport = FakeTransport(answer(429, b"{}",
                                             {"Retry-After": header}))
            result = run(transport)
            self.assertEqual(result, {"failure": "rate_limit",
                                      "next_attempt_at": top}, header[:20])
        # The last second that still fits is not clamped.
        fits = (top - NOW_MS) // 1000
        transport = FakeTransport(answer(429, b"{}",
                                         {"Retry-After": str(fits)}))
        self.assertEqual(run(transport)["next_attempt_at"],
                         NOW_MS + fits * 1000)

    def test_exactly_one_transport_call_on_every_outcome(self):
        responses = (ok(), answer(429, b"{}"), answer(500), answer(400, b"{}"),
                     answer(401), answer(404), ok_raw(b"not json"))
        for response in responses:
            transport = FakeTransport(response)
            run(transport)
            self.assertEqual(len(transport.calls), 1)
        for exc in (OSError("boom"), TimeoutError(), ssl.SSLError("x")):
            transport = FakeTransport(exc=exc)
            run(transport)
            self.assertEqual(len(transport.calls), 1)

    def test_failure_schedule_uses_poll_plus_jitter(self):
        for response in (answer(500), answer(401), ok_raw(b"x")):
            result = run(FakeTransport(response), jitter=90)
            self.assertEqual(result["next_attempt_at"], NOW_MS + 990_000)

    def test_signed_get_request(self):
        transport = FakeTransport(ok())
        run(transport)
        (url, headers, timeout), = transport.calls
        self.assertEqual(
            url, BASE_URL + "/cloud/project/" + PROJECT_ID + "/usage/current")
        self.assertEqual(headers["X-Ovh-Application"], APP_KEY)
        self.assertEqual(headers["X-Ovh-Consumer"], CONSUMER_KEY)
        self.assertEqual(headers["X-Ovh-Timestamp"], str(NOW_MS // 1000))
        self.assertTrue(headers["X-Ovh-Signature"].startswith("$1$"))
        self.assertIsInstance(timeout, int)
        self.assertGreater(timeout, 0)

    def test_signature_for_one_known_input(self):
        # sha1("secret+consumer+GET+url++timestamp"), recomputed here with
        # hashlib independently of the module for the pinned literal below.
        headers = ovh.sign_request(
            ovh.OvhCredential("fake-app-key", "fake-app-secret",
                              "fake-consumer-key", "fakeproject01"),
            "https://api.example.invalid/1.0/cloud/project/fakeproject01"
            "/usage/current", 1790969016)
        self.assertEqual(headers, {
            "X-Ovh-Application": "fake-app-key",
            "X-Ovh-Consumer": "fake-consumer-key",
            "X-Ovh-Timestamp": "1790969016",
            "X-Ovh-Signature":
                "$1$c2a085d4ed4358f7182d1a5a41525369893c2931",
        })

    def test_default_base_url_is_the_ovh_eu_api(self):
        self.assertEqual(ovh.OVH_EU_BASE_URL, "https://eu.api.ovh.com/1.0")
        transport = FakeTransport(ok())
        ovh.poll_once(credential=credential(), transport=transport,
                      clock=lambda: NOW_MS, jitter_seconds=0, config=config())
        self.assertTrue(transport.calls[0][0].startswith(
            "https://eu.api.ovh.com/1.0/cloud/project/"))

    def test_bad_injected_parts_are_typed_errors_before_any_call(self):
        cases = [
            dict(cfg=config(machine="Desktop")),
            dict(cfg=config(account="")),
            dict(cfg=config(scope_alias="a b")),
            dict(cfg=config(scope_revision=0)),
            dict(cfg=config(scope_revision=True)),
            dict(cfg=config(scope_revision=2 ** 63)),
            dict(cred=ovh.OvhCredential("", APP_SECRET, CONSUMER_KEY,
                                        PROJECT_ID)),
            dict(cred=ovh.OvhCredential(APP_KEY, APP_SECRET, CONSUMER_KEY,
                                        "../x")),
            dict(cred=ovh.OvhCredential(APP_KEY, APP_SECRET, CONSUMER_KEY,
                                        "a/b")),
            dict(now_ms=-1),
            dict(now_ms=True),
            dict(now_ms="5"),
        ]
        for kw in cases:
            transport = FakeTransport(ok())
            with self.assertRaises(QuotaError) as caught:
                run(transport, **kw)
            self.assertEqual(caught.exception.code, CODE_INVALID_CONFIG)
            self.assertEqual(transport.calls, [])
            for secret in SECRETS:
                self.assertNotIn(secret, caught.exception.message)
                self.assertNotIn(secret, repr(caught.exception))


class BillingTls(unittest.TestCase):
    """billing_tls: the transport contract refuses non-HTTPS and disabled
    verification BEFORE any call; a TLS error from the transport is tls."""

    def test_http_url_is_refused_before_the_transport(self):
        for base in ("http://api.example.invalid/1.0", "ftp://x/1.0",
                     "//api.example.invalid/1.0", "api.example.invalid",
                     "", "HTTP://api.example.invalid/1.0"):
            transport = FakeTransport(ok())
            result = run(transport, base_url=base, jitter=4)
            self.assertEqual(result, {"failure": "tls",
                                      "next_attempt_at": NOW_MS + 904_000})
            self.assertEqual(transport.calls, [], base)

    def test_https_scheme_is_case_insensitive(self):
        transport = FakeTransport(ok())
        result = run(transport, base_url="HTTPS://api.example.invalid/1.0")
        self.assertIn("document", result)

    def test_disabled_verification_is_refused_before_the_transport(self):
        for flag in (False, None, 0, "yes", 1):
            transport = FakeTransport(ok(), verifies_tls=flag)
            result = run(transport)
            self.assertEqual(failure_of(result), "tls", flag)
            self.assertEqual(transport.calls, [], flag)

    def test_a_transport_without_the_flag_is_refused(self):
        class Bare:
            calls = 0

            def get(self, url, headers, timeout_seconds):
                Bare.calls += 1
                return ok()

        self.assertEqual(failure_of(run(Bare())), "tls")
        self.assertEqual(Bare.calls, 0)

    def test_tls_error_from_the_transport_is_tls(self):
        for exc in (ssl.SSLError("bad"), ssl.SSLCertVerificationError("x"),
                    ovh.TlsError()):
            result = run(FakeTransport(exc=exc), jitter=2)
            self.assertEqual(result, {"failure": "tls",
                                      "next_attempt_at": NOW_MS + 902_000})

    def test_other_transport_errors_are_transport_not_tls(self):
        for exc in (OSError("refused"), ConnectionError(), TimeoutError(),
                    RuntimeError("anything"), ValueError("x")):
            self.assertEqual(failure_of(run(FakeTransport(exc=exc))),
                             "transport", exc)

    def test_non_exception_signals_are_not_swallowed(self):
        with self.assertRaises(KeyboardInterrupt):
            run(FakeTransport(exc=KeyboardInterrupt()))


class BillingOutputErrors(unittest.TestCase):
    """billing_output_errors: every failure enum and the success shape are
    closed; the real 400 'Invalid signature' is auth."""

    FAILURES = {"auth", "transport", "rate_limit", "schema", "currency",
                "period", "tls"}

    def test_the_failure_enum_is_exactly_the_closed_set(self):
        self.assertEqual(set(ovh.FAILURES), self.FAILURES)

    def test_real_invalid_signature_is_auth(self):
        capture = _fixture_json("auth-refused-invalid-signature.sanitized.json")
        self.assertEqual(capture["http_status"], 400)
        result = run(FakeTransport(answer(
            capture["http_status"], dump(capture["body"]))))
        self.assertEqual(result, {"failure": "auth",
                                  "next_attempt_at": NOW_MS + 900_000})

    def test_other_400s_are_schema(self):
        bodies = (
            {"class": "Client::BadRequest", "message": "Something else"},
            {"class": "Client::Other", "message": "Invalid signature"},
            {"class": "Client::BadRequest"},
            {"message": "Invalid signature"},
            [], "text", None, 5)
        for body in bodies:
            self.assertEqual(
                failure_of(run(FakeTransport(answer(400, dump(body))))),
                "schema", body)
        for raw in (b"", b"<html>", b"\xff\xfe"):
            self.assertEqual(
                failure_of(run(FakeTransport(answer(400, raw)))), "schema")

    def test_401_and_403_are_auth(self):
        for status in (401, 403):
            self.assertEqual(failure_of(run(FakeTransport(
                answer(status, b"")))), "auth")
            self.assertEqual(failure_of(run(FakeTransport(
                answer(status, b"<html>no</html>")))), "auth")

    def test_other_4xx_are_schema(self):
        for status in (402, 404, 405, 408, 409, 410, 422, 451, 499):
            self.assertEqual(failure_of(run(FakeTransport(
                answer(status, b"{}")))), "schema", status)

    def test_429_is_rate_limit(self):
        capture = _fixture_json("error-synthetic-rate-limited.json")
        result = run(FakeTransport(answer(
            capture["http_status"], dump(capture["body"]),
            capture["relevant_headers"])))
        self.assertEqual(failure_of(result), "rate_limit")
        self.assertEqual(result["next_attempt_at"], NOW_MS + 900_000)  # 120<900

    def test_5xx_are_transport(self):
        html = _fixture_bytes("error-synthetic-html-page.html")
        for status in (500, 502, 503, 504, 599):
            self.assertEqual(failure_of(run(FakeTransport(
                answer(status, html)))), "transport", status)

    def test_connection_errors_and_timeouts_are_transport(self):
        for exc in (ConnectionResetError(), TimeoutError(), OSError(11001)):
            self.assertEqual(failure_of(run(FakeTransport(exc=exc))),
                             "transport")

    def test_unexpected_statuses_are_schema(self):
        for status in (100, 201, 204, 301, 302, 304, 600, 0, -1, True, None,
                       "200", 200.0):
            self.assertEqual(failure_of(run(FakeTransport(
                answer(status, dump(real_body()))))), "schema", status)

    def test_malformed_200_bodies_are_schema(self):
        html = _fixture_bytes("error-synthetic-html-page.html")
        truncated = _fixture_bytes("usage-current-synthetic-truncated.json.txt")
        missing = _fixture_bytes(
            "usage-current-synthetic-missing-total-price.json")
        bodies = (html, truncated, missing, b"", b"null", b"[]", b"5",
                  b'"x"', b"{}", b"\xff\xfe\x00", b"NaN", b'{"a": NaN}',
                  b'{"totalPrice": Infinity}',
                  b'{"a":1,"a":2}',                       # duplicate key
                  b"[" * 100000, b'{"a":' * 5000 + b"1" + b"}" * 5000,
                  dump(real_body()) + b" trailing",
                  b" " * (ovh.MAX_BODY_BYTES + 1))
        for raw in bodies:
            self.assertEqual(
                failure_of(run(FakeTransport(ok_raw(raw)))), "schema", raw[:20])

    def test_non_finite_numbers_are_never_read_as_numbers(self):
        # Zero totals everywhere except a NaN/Infinity where a number goes:
        # were the constant read as 0 this body would be a valid document.
        def zero(b):
            b["totalPrice"].update(priceInUcents=0, value=0)
            b["resourcesUsage"] = []
        raw = dump(mutated(zero)).replace(b'"value": 0', b'"value": NaN')
        self.assertIn(b"NaN", raw)
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))), "schema")
        raw = raw.replace(b"NaN", b"-Infinity")
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))), "schema")

    def test_body_size_limit_is_enforced_on_valid_json(self):
        padded = dump(real_body()) + b" " * ovh.MAX_BODY_BYTES
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(padded)))),
                         "schema")
        edge = dump(real_body())
        edge += b" " * (ovh.MAX_BODY_BYTES - len(edge))
        self.assertEqual(len(edge), ovh.MAX_BODY_BYTES)
        self.assertIn("document", run(FakeTransport(ok_raw(edge))))

    def test_duplicate_keys_cannot_hide_a_field(self):
        raw = dump(real_body()).replace(
            b'"currencyCode": "EUR"',
            b'"currencyCode": "USD", "currencyCode": "EUR"')
        self.assertNotEqual(raw, dump(real_body()))
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))), "schema")

    def test_str_body_is_accepted_like_bytes(self):
        response = ovh.TransportResponse(200, {}, dump(real_body()).decode())
        self.assertIn("document", run(FakeTransport(response)))

    def test_malformed_transport_answers_are_transport_failures(self):
        for response in (None, "text", 5, (200,), (200, {}, b"", 1),
                         ovh.TransportResponse(200, None, b"{}"),
                         ovh.TransportResponse(200, {}, None),
                         ovh.TransportResponse(200, {}, 5)):
            self.assertEqual(failure_of(run(FakeTransport(response))),
                             "transport", response)

    def test_every_result_is_closed_and_json_serializable(self):
        results = [
            run(FakeTransport(ok())),
            run(FakeTransport(answer(401))),
            run(FakeTransport(answer(429, b"{}"))),
            run(FakeTransport(answer(500))),
            run(FakeTransport(exc=OSError("x"))),
            run(FakeTransport(ok()), base_url="http://x"),
            run_body(mutated(lambda b: b["totalPrice"].update(
                currencyCode="USD"))),
            run_body(mutated(lambda b: b["period"].update(
                {"to": "2026-12-01T00:00:00.000Z"}))),
            run_body(mutated(lambda b: b.pop("totalPrice"))),
        ]
        seen = set()
        for result in results:
            self.assertIsInstance(result["next_attempt_at"], int)
            self.assertNotIsInstance(result["next_attempt_at"], bool)
            json.dumps(result)
            if "document" in result:
                self.assertEqual(set(result), {"document", "next_attempt_at"})
                self.assertEqual(set(result["document"]),
                                 {"version", "readings"})
                self.assertEqual(result["document"]["version"], 3)
                self.assertEqual(len(result["document"]["readings"]), 1)
            else:
                self.assertEqual(set(result), {"failure", "next_attempt_at"})
                self.assertIn(result["failure"], self.FAILURES)
                seen.add(result["failure"])
        self.assertEqual(seen, self.FAILURES)


class Privacy2b(unittest.TestCase):
    """privacy_2b (this card): no credential, project id, URL, header or
    response-body text reaches any output or error."""

    BODY_SENTINEL = "RESPONSE-BODY-SENTINEL"

    def assert_clean(self, value):
        text = json.dumps(value) if not isinstance(value, str) else value
        for secret in SECRETS + (self.BODY_SENTINEL,):
            self.assertNotIn(secret, text)

    def test_no_sentinel_in_any_output(self):
        poisoned = real_body()
        poisoned["message"] = self.BODY_SENTINEL
        poisoned["class"] = self.BODY_SENTINEL
        poisoned["resourcesUsage"][0]["type"] = "ai-endpoints"
        poisoned["resourcesUsage"][0]["resources"][0]["region"] = \
            self.BODY_SENTINEL
        outputs = [
            run(FakeTransport(ok(poisoned))),
            run(FakeTransport(ok_raw(self.BODY_SENTINEL.encode()))),
            run(FakeTransport(answer(400, dump(
                {"class": "Client::BadRequest",
                 "message": self.BODY_SENTINEL})))),
            run(FakeTransport(answer(401, self.BODY_SENTINEL.encode()))),
            run(FakeTransport(answer(429, self.BODY_SENTINEL.encode(),
                                     {"Retry-After": self.BODY_SENTINEL}))),
            run(FakeTransport(answer(500, self.BODY_SENTINEL.encode()))),
            run(FakeTransport(exc=OSError(
                "%s %s %s" % (APP_SECRET, BASE_URL, PROJECT_ID)))),
            run(FakeTransport(exc=ssl.SSLError(
                "%s %s" % (CONSUMER_KEY, self.BODY_SENTINEL)))),
            run(FakeTransport(ok()), base_url="http://api.example.invalid/1.0"),
            run(FakeTransport(ok(), verifies_tls=False)),
            run_body(mutated(lambda b: b.update(
                lastUpdate=self.BODY_SENTINEL))),
            run_body(mutated(lambda b: b["totalPrice"].update(
                currencyCode=self.BODY_SENTINEL))),
        ]
        for out in outputs:
            self.assert_clean(out)
        # The poisoned-but-valid body still yields only aliases and numbers.
        document = outputs[0]["document"]
        self.assertEqual(set(document["readings"][0]["payload"]), {
            "scope_alias", "scope_revision", "period_start", "period_end",
            "currency", "total_micro_eur", "non_ai_spend_present"})

    def test_errors_and_reprs_do_not_carry_secrets(self):
        cred = credential()
        for text in (repr(cred), str(cred), repr([cred])):
            self.assert_clean(text)
        with self.assertRaises(QuotaError) as caught:
            run(FakeTransport(ok()), jitter=-1, cred=cred)
        self.assert_clean(caught.exception.message)
        self.assert_clean(repr(caught.exception))
        self.assert_clean(str(caught.exception))

    def test_signed_headers_never_leave_the_transport_call(self):
        transport = FakeTransport(ok())
        result = run(transport)
        signature = transport.calls[0][1]["X-Ovh-Signature"]
        self.assertNotIn(signature, json.dumps(result))
        self.assertNotIn(str(NOW_MS // 1000) + "\"", json.dumps(result))

    def test_the_module_does_no_file_env_or_network_io(self):
        path = os.path.join(REPO_ROOT, "quota_balancer", "ovh_adapter.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        for needle in ("import os", "from os", "import socket", "urllib.request",
                       "http.client", "open(",
                       "os.environ", "getenv", "import subprocess", "sqlite3",
                       "import requests", "import time", "datetime.now",
                       "time.time(", "random"):
            self.assertFalse(needle in source, needle)


def money_body(value, amount, ucents=None):
    """The real body with total `value` (a literal string, kept exact) and
    one category of `amount` (a literal string)."""
    body = real_body()
    body["totalPrice"].update(
        priceInUcents=int(Decimal(value) * 10 ** 8) if ucents is None
        else ucents, value="VALUE_LITERAL")
    body["resourcesUsage"] = [{"type": "ai-endpoints",
                               "totalPrice": "AMOUNT_LITERAL"}]
    raw = dump(body).replace(b'"VALUE_LITERAL"', value.encode())
    return raw.replace(b'"AMOUNT_LITERAL"', amount.encode())


class FixRound1MoneyContext(unittest.TestCase):
    """Fix round 1 finding 1: money validation is independent of the
    caller's Decimal context and exact on bounded inputs."""

    def test_caller_precision_does_not_change_the_result(self):
        expected = run(FakeTransport(ok()))
        self.assertIn("document", expected)
        import decimal
        with decimal.localcontext() as ctx:
            ctx.prec = 6
            self.assertEqual(run(FakeTransport(ok())), expected)
        with decimal.localcontext() as ctx:
            ctx.prec = 1
            self.assertEqual(run(FakeTransport(ok())), expected)

    def test_caller_traps_do_not_change_the_result(self):
        import decimal
        expected = run(FakeTransport(ok()))
        for traps in ((decimal.Inexact,), (decimal.Rounded,),
                      (decimal.Inexact, decimal.Rounded,
                       decimal.InvalidOperation, decimal.Overflow)):
            with decimal.localcontext() as ctx:
                ctx.prec = 6
                for trap in traps:
                    ctx.traps[trap] = True
                self.assertEqual(run(FakeTransport(ok())), expected)

    def test_caller_context_does_not_change_a_schema_failure_either(self):
        import decimal
        raw = money_body("1", "1.000001000001")
        baseline = run(FakeTransport(ok_raw(raw)))
        self.assertEqual(failure_of(baseline), "schema")
        with decimal.localcontext() as ctx:
            ctx.prec = 200
            ctx.traps[decimal.Inexact] = True
            self.assertEqual(run(FakeTransport(ok_raw(raw))), baseline)

    def test_subtolerance_noise_cannot_round_onto_the_tolerance(self):
        # Under the default 28-digit context 1.00000100000000000000000000001
        # - 1 rounds to exactly 0.000001 and used to be accepted.
        raw = money_body("1", "1.00000100000000000000000000001")
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))), "schema")

    def test_twelve_decimal_case_just_over_tolerance_is_schema(self):
        raw = money_body("1", "1.000001000001")
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))), "schema")

    def test_exactly_one_micro_eur_is_still_accepted(self):
        for amount, expected in (("1.000001", True), ("0.999999", True),
                                 ("1.0000009", True), ("1.0000011", False),
                                 ("0.9999989", False)):
            result = run(FakeTransport(ok_raw(money_body("1", amount))))
            self.assertEqual("document" in result, expected, amount)

    def test_representation_bound_on_money_numbers(self):
        # At most 15 digits before the point and 12 after, judged on the
        # parsed Decimal: 12 decimals pass, 13 (even trailing zeros) fail.
        def zero_total(amount):
            body = real_body()
            body["totalPrice"].update(priceInUcents=0, value=0)
            body["resourcesUsage"] = [{"type": "ai-endpoints",
                                       "totalPrice": "AMOUNT_LITERAL"}]
            return dump(body).replace(b'"AMOUNT_LITERAL"', amount.encode())

        for amount, expected in (("0.000000000001", True),
                                 ("0.0000000000001", False),
                                 ("0.0000000000000", False),
                                 ("1E-13", False),
                                 ("1E-12", True),
                                 ("0", True)):
            result = run(FakeTransport(ok_raw(zero_total(amount))))
            self.assertEqual("document" in result, expected, amount)
            if not expected:
                self.assertEqual(failure_of(result), "schema", amount)

    def test_representation_bound_on_the_integer_digits(self):
        # 16 integer digits is out; so is any exponent that makes it so.
        for literal in ("1" + "0" * 15 + ".5", "1E+15", "1E+400",
                        "9999999999999999"):
            body = real_body()
            body["totalPrice"].update(value="VALUE_LITERAL")
            raw = dump(body).replace(b'"VALUE_LITERAL"', literal.encode())
            self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))),
                             "schema", literal)
        body = real_body()
        body["resourcesUsage"][0]["totalPrice"] = "AMOUNT_LITERAL"
        raw = dump(body).replace(b'"AMOUNT_LITERAL"', b"1E+15")
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))), "schema")


class FixRound1ExtremeNumbers(unittest.TestCase):
    """Fix round 1 finding 2: a number literal can never escape as a raw
    exception, whichever field carries it."""

    EXTREME = (b"1e9999999999999999999", b"-1e9999999999999999999",
               b"1E+99999999999999999999999", b"1e-9999999999999999999")

    def test_200_body_with_an_ignored_extreme_exponent_is_schema(self):
        for literal in self.EXTREME:
            raw = dump(real_body())[:-1] + b', "ignored": ' + literal + b"}"
            result = run(FakeTransport(ok_raw(raw)), jitter=3)
            self.assertEqual(result, {"failure": "schema",
                                      "next_attempt_at": NOW_MS + 903_000},
                             literal)

    def test_extreme_exponent_in_a_known_field_is_schema(self):
        raw = dump(real_body()).replace(b'"value": 6.737286',
                                        b'"value": 1e9999999999999999999')
        self.assertIn(b"e9999999999999999999", raw)
        self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))), "schema")

    def test_400_body_with_an_extreme_exponent_is_schema(self):
        for literal in self.EXTREME:
            raw = (b'{"class": "Client::BadRequest", "message": '
                   b'"Invalid signature", "ignored": ' + literal + b"}")
            result = run(FakeTransport(answer(400, raw)), jitter=3)
            self.assertEqual(result, {"failure": "schema",
                                      "next_attempt_at": NOW_MS + 903_000},
                             literal)

    def test_untrapped_caller_context_cannot_turn_it_into_nan(self):
        import decimal
        raw = dump(real_body())[:-1] + b', "ignored": 1e9999999999999999999}'
        with decimal.localcontext() as ctx:
            ctx.traps[decimal.InvalidOperation] = False
            ctx.traps[decimal.Overflow] = False
            self.assertEqual(failure_of(run(FakeTransport(ok_raw(raw)))),
                             "schema")

    def test_number_literals_are_bounded_to_64_characters(self):
        def with_ignored(literal):
            return dump(real_body())[:-1] + b', "ignored": ' + literal + b"}"

        ok64 = b"0." + b"1" * 62               # exactly 64 characters
        too_long = b"0." + b"1" * 63           # 65
        self.assertEqual(len(ok64), 64)
        self.assertIn("document", run(FakeTransport(ok_raw(with_ignored(ok64)))))
        self.assertEqual(failure_of(run(FakeTransport(
            ok_raw(with_ignored(too_long))))), "schema")
        self.assertEqual(failure_of(run(FakeTransport(
            ok_raw(with_ignored(b"1" * 65))))), "schema")
        self.assertIn("document", run(FakeTransport(
            ok_raw(with_ignored(b"1" * 64)))))


class FixRound1MalformedBaseUrl(unittest.TestCase):
    """Fix round 1 finding 3: a malformed base_url raises the static
    invalid-input error before any transport call, with nothing of it
    reachable from the exception."""

    SENTINEL = "SYNTHETIC-URL-SENTINEL"

    def assert_static_and_clean(self, base_url, sentinels):
        import traceback
        transport = FakeTransport(ok())
        with self.assertRaises(QuotaError) as caught:
            run(transport, base_url=base_url)
        exc = caught.exception
        self.assertEqual(exc.code, CODE_INVALID_CONFIG)
        self.assertEqual(transport.calls, [])
        self.assertIsNone(exc.__cause__)
        self.assertIsNone(exc.__context__)
        text = "\n".join((str(exc), repr(exc), repr(exc.args),
                          "".join(traceback.format_exception(exc))))
        for sentinel in sentinels:
            self.assertNotIn(sentinel, text)

    def test_the_reviewers_sentinel_url(self):
        self.assert_static_and_clean(
            "https://" + self.SENTINEL + "／host.invalid/1.0",
            (self.SENTINEL, "host.invalid"))

    def test_other_malformed_urls(self):
        cases = (
            "https://" + self.SENTINEL + "\x00host.invalid/1.0",
            "https://" + self.SENTINEL + " host.invalid/1.0",
            "https://héllo-" + self.SENTINEL + ".invalid/1.0",
            "https://user:" + self.SENTINEL + "@host.invalid/1.0",
            "https://" + self.SENTINEL + "@host.invalid/1.0",
            "https://host.invalid/1.0?token=" + self.SENTINEL,
            "https://host.invalid/1.0#" + self.SENTINEL,
            "https:///" + self.SENTINEL,
            "https://host.invalid:" + self.SENTINEL + "/1.0",
            "https://host.invalid:99999999/" + self.SENTINEL,
            "https://[" + self.SENTINEL + "/1.0",
            "https://\t" + self.SENTINEL + ".invalid/1.0",
            "https://" + self.SENTINEL + "\n.invalid/1.0",
        )
        for base_url in cases:
            self.assert_static_and_clean(base_url, (self.SENTINEL,))

    def test_well_formed_non_https_urls_still_fail_tls(self):
        for base in ("http://api.example.invalid/1.0", "ftp://x/1.0",
                     "HTTP://api.example.invalid/1.0"):
            transport = FakeTransport(ok())
            self.assertEqual(failure_of(run(transport, base_url=base)), "tls")
            self.assertEqual(transport.calls, [])

    def test_a_wellformed_https_url_with_a_port_and_path_works(self):
        transport = FakeTransport(ok())
        result = run(transport, base_url="https://api.example.invalid:8443/1.0/")
        self.assertIn("document", result)
        self.assertTrue(transport.calls[0][0].startswith(
            "https://api.example.invalid:8443/1.0/cloud/project/"))


class FixRound2RetryAfterLeadingZeros(unittest.TestCase):
    """Fix round 2 finding 5: magnitude is judged on the significant
    digits, never on the string length."""

    def after(self, header):
        transport = FakeTransport(answer(429, b"{}", {"Retry-After": header}))
        result = run(transport)
        self.assertEqual(failure_of(result), "rate_limit")
        return result["next_attempt_at"]

    def test_zeros_mean_zero_seconds_and_never_stall_the_poll(self):
        for header in ("0", "00", "0" * 19, "0" * 20, "0" * 5000):
            self.assertEqual(self.after(header), NOW_MS + 900_000, header[:6])

    def test_leading_zeros_do_not_change_the_value(self):
        for header, seconds in (("0" * 20 + "1200", 1200),
                                ("0" * 5000 + "1200", 1200),
                                ("0001200", 1200), ("0" * 19 + "172800", 172800),
                                ("0" * 40 + "86400", 86400)):
            self.assertEqual(self.after(header), NOW_MS + seconds * 1000,
                             header[-8:])

    def test_genuine_overflow_still_clamps_and_the_edges_hold(self):
        top = 2 ** 63 - 1
        fits = (top - NOW_MS) // 1000
        self.assertEqual(self.after(str(fits)), NOW_MS + fits * 1000)
        self.assertEqual(self.after("0" * 30 + str(fits)), NOW_MS + fits * 1000)
        for header in (str(fits + 1), "9" * 19, "9" * 40, "9" * 5000,
                       "0" * 30 + "9" * 40, "1" + "0" * 5000):
            self.assertEqual(self.after(header), top, header[:6])

    def test_invalid_headers_are_still_ignored(self):
        for header in ("", " ", "-0", "+0", "0.5", "0x0", "1_0", "soon"):
            transport = FakeTransport(answer(429, b"{}",
                                             {"Retry-After": header}))
            self.assertEqual(run(transport)["next_attempt_at"],
                             NOW_MS + 900_000, header)


class EndToEndIngest(unittest.TestCase):
    """The adapter's document for the real capture goes through the
    existing reading-import path: accepted, then accepted again as a
    replay with no change."""

    def cli(self, *args):
        return subprocess.run(
            [PYTHON, "-m", "quota_balancer", *args], cwd=REPO_ROOT,
            capture_output=True, text=True)

    def test_document_is_accepted_then_replays_without_change(self):
        base = os.environ.get("AGENTTALK_SCRATCH")
        if base:
            os.makedirs(base, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base,
                                         prefix="quota-card2b3-") as tmp:
            db = os.path.join(tmp, "inventory.db")
            self.assertEqual(self.cli("--db", db, "init").returncode, 0)
            inventory = {
                "version": 1,
                "accounts": [{"alias": "meter", "vendor": "ovh",
                              "kind": "metered",
                              "budget_micro_eur": 100000000}],
                "machines": [{"alias": "desktop"}],
                "gateways": [{"alias": "g1", "account": "meter",
                              "machine": "desktop",
                              "share_micro_eur": 100000000,
                              "period": "utc_month"}],
            }
            policy = {"version": 1, "policies": [{
                "account": "meter", "reporter_machine": "desktop",
                "scope_alias": "proj-ai", "scope_revision": 1,
                "dedicated_ai": True, "non_ai_allowance_micro_eur": 0,
                "cap_basis": "ex_vat", "provider_basis": "ex_vat",
                "ledger_basis": "ex_vat", "vat_bps": None,
                "poll_seconds": 900, "max_age_seconds": 7200,
                "lag_seconds": 14400, "abs_tolerance_micro_eur": 50000,
                "relative_tolerance_bps": 100, "mode": "advisory",
                "stale_action": "alert", "divergence_action": "hold"}]}
            files = {}
            for name, doc in (("inv", inventory), ("pol", policy)):
                files[name] = os.path.join(tmp, name + ".json")
                with open(files[name], "w", encoding="utf-8") as handle:
                    json.dump(doc, handle)
            self.assertEqual(self.cli("--db", db, "config-import", "--file",
                                      files["inv"]).returncode, 0)
            self.assertEqual(self.cli("--db", db, "billing-policy-import",
                                      "--file", files["pol"]).returncode, 0)

            result = run(FakeTransport(ok()))
            document_file = os.path.join(tmp, "doc.json")
            with open(document_file, "w", encoding="utf-8") as handle:
                json.dump(result["document"], handle)

            first = self.cli("--db", db, "reading-import", "--file",
                             document_file)
            self.assertEqual(first.returncode, 0, first.stderr)
            def rows():
                conn = sqlite3.connect(db)
                try:
                    return [conn.execute("SELECT * FROM " + table).fetchall()
                            for table in ("readings", "billing_totals")]
                finally:
                    conn.close()

            stored = rows()
            self.assertEqual(len(stored[0]), 1)
            before = self.cli("--db", db, "status", "--json").stdout
            totals = json.loads(before)["billing"][0]["totals"][0]
            self.assertEqual(totals["latest_micro_eur"], 6737286)
            self.assertEqual(totals["observed_at"], LAST_UPDATE_MS)
            self.assertEqual(totals["period_start"], OCT)
            self.assertIs(totals["conflict"], False)

            # An unchanged poll an hour later: byte-identical document.
            again = run(FakeTransport(ok()), now_ms=NOW_MS + 3_600_000)
            self.assertEqual(again["document"], result["document"])
            with open(document_file, "w", encoding="utf-8") as handle:
                json.dump(again["document"], handle)
            replay = self.cli("--db", db, "reading-import", "--file",
                              document_file)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            after = self.cli("--db", db, "status", "--json").stdout
            totals = json.loads(after)["billing"][0]["totals"][0]
            self.assertEqual(totals["latest_micro_eur"], 6737286)
            self.assertEqual(totals["observed_at"], LAST_UPDATE_MS)
            # No new reading and no changed row (status ages move with the
            # wall clock, so compare the stored rows, not the projection).
            self.assertEqual(rows(), stored)


if __name__ == "__main__":
    unittest.main()
