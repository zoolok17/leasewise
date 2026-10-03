"""One money basis with checked arithmetic (card R1-M, DESIGN-R1-MONEY.md).

Every amount Leasewise adds or compares is a whole number of micro-EUR, in the account's cap basis. This module is the one place
that does the arithmetic, so that nothing adds money inside the database and nothing silently overflows or rounds.

* **Exact.** Python integers do not overflow. A value is checked when it enters (`check`) and when it is stored or sent
  (`check` with the interface's own maximum). A sum that is compared with a limit stops early (`exceeds`, `reaches`) and never needs
  the overflowed number. A total that is shown is exact; a total that goes to the pure admission rule is `saturate`d.
* **Unknown is never zero.** `UNKNOWN` is its own object. A sum with an unknown term is unknown; but amounts only add, so a known part
  that already passes the limit still decides (`exceeds` answers True): missing evidence never makes a lease look cheaper.
* **Conversions** between the declared tax bases round by the ROLE of the amount: an *exposure* (money that counts against a limit)
  rounds up, a *limit* (the figure compared against) rounds down. A conversion that cannot be done is `UNKNOWN`, never 0.
* **SQL.** The database's `SUM` stops with an error at big numbers, and its `+`, `-` and `*` silently turn into decimals. So the
  database only selects, counts and filters; `exact_sums` adds the high and the low half of each number separately (neither half can
  overflow) and puts them together in Python.

Pure: no clock, no store of its own, no I/O. `exact_sums` uses the connection it is given.
"""

from . import reduce

INT64_MAX = 2 ** 63 - 1
SATURATION = 2 ** 63                 # one above the largest cap: a usage at this point is above every cap
WIRE_MAX = 10 ** 12                  # the reporter's receipt wire (SPEC v2.2 3.4): an amount is 0 .. 10^12
ADMIT_WIRE_MAX = 10 ** 12 - 1        # the admission answer's money fields (L6a-2 wire)
ROW_LIMIT = 2 ** 31 - 1              # most rows a split sum may add (neither half can overflow below this)
_HALF = 32
_EXACT_MAX = 2 ** 126               # an exact sum may exceed every stored bound; it is still a whole non-negative number
_LOW_MASK = 4294967295               # 2**32 - 1

EXPOSURE = "exposure"                # counts against a limit: rounds up
LIMIT = "limit"                      # is the figure compared against: rounds down
ROLES = (EXPOSURE, LIMIT)


class MoneyError(ValueError):
    """A contract violation: a value that is not an amount, or a store that does not keep its own rules. Carries no value."""

    def __init__(self, reason="money"):
        super().__init__(reason)
        self.reason = reason


class RowCountError(MoneyError):
    """Too many rows for a split sum to be exact (the guard that makes the claim checkable)."""

    def __init__(self):
        super().__init__("row_count")


class _Unknown:
    """An amount that is not known. It is never 0 and never a number: using it as a truth value is an error."""

    __slots__ = ()
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "UNKNOWN"

    def __bool__(self):
        raise TypeError("an unknown amount has no truth value")

    def __reduce__(self):
        return (_Unknown, ())


UNKNOWN = _Unknown()


def is_known(value):
    return value is not UNKNOWN


def check(value, maximum=INT64_MAX):
    """A whole non-negative number not above `maximum`. Floats, booleans, negative numbers and text are refused."""
    if type(value) is not int or value < 0 or value > maximum:
        raise MoneyError("amount")
    return value


def saturate(value):
    """An exact total, made safe for the pure admission rule: any total above the largest cap becomes `SATURATION`."""
    return min(value, SATURATION)


def _terms(terms):
    for term in terms:
        if term is UNKNOWN:
            yield term
        else:
            yield check(term, maximum=_EXACT_MAX)      # exact, but still a whole non-negative number


def add(*terms):
    """The exact sum, or UNKNOWN when any term is unknown."""
    total = 0
    for term in _terms(terms):
        if term is UNKNOWN:
            return UNKNOWN
        total += term
    return total


def lower_bound(terms):
    """The sum of the known terms: amounts only add, so the true total is at least this."""
    return sum(term for term in _terms(terms) if term is not UNKNOWN)


def _compare(terms, limit, inclusive):
    if limit is UNKNOWN:
        return UNKNOWN
    check(limit, maximum=_EXACT_MAX)
    if inclusive and limit <= 0:
        return True                                      # a total of non-negative amounts has always reached 0: read no term
    total, unknown = 0, False
    for term in _terms(terms):
        if term is UNKNOWN:
            unknown = True
            continue
        total += term
        if total > limit or (inclusive and total >= limit):
            return True                                  # the known part alone decides, whatever the unknown terms are
    return UNKNOWN if unknown else False


def exceeds(terms, limit):
    """True when the total is above `limit`; False when every term is known and it is not; else UNKNOWN. Stops at the first
    point at which the known part passes the limit, so it never needs an overflowed number. `terms` may be a generator."""
    return _compare(terms, limit, inclusive=False)


def reaches(terms, limit):
    """Like `exceeds` for 'has the total reached the limit' (at or above)."""
    return _compare(terms, limit, inclusive=True)


def headroom(limit, terms):
    """`limit` minus the total (it may be negative), or UNKNOWN when the limit or any term is unknown."""
    total = add(*terms)
    if limit is UNKNOWN or total is UNKNOWN:
        return UNKNOWN
    return check(limit, maximum=_EXACT_MAX) - total


def convert(amount, source, target, vat_bps, role):
    """An amount from one tax basis to another, in exact integer arithmetic, rounded by `role`.

    ex_vat -> invoice_total: amount * (10000 + vat_bps) / 10000; invoice_total -> ex_vat: amount * 10000 / (10000 + vat_bps).
    An EXPOSURE rounds up (never counts less than it is), a LIMIT rounds down (never allows more than it is). Equal known bases
    return the amount unchanged. UNKNOWN (never 0) when either basis is `unknown`, when the bases differ and the rate is missing
    or outside 0..10000, or when the result would be above INT64_MAX. A value that is not an amount, a basis that is not in the
    closed set, or a role that is not one of ROLES is a MoneyError."""
    check(amount)
    if role not in ROLES or source not in reduce.CLOSED_BASES or target not in reduce.CLOSED_BASES:
        raise MoneyError("conversion")
    if source == reduce.BASIS_UNKNOWN or target == reduce.BASIS_UNKNOWN:
        return UNKNOWN
    if source == target:
        return amount
    if type(vat_bps) is not int or not 0 <= vat_bps <= reduce.VAT_BPS_MAX:
        return UNKNOWN
    if source == reduce.BASIS_EX_VAT:
        numerator, denominator = amount * (10_000 + vat_bps), 10_000
    else:
        numerator, denominator = amount * 10_000, 10_000 + vat_bps
    result = (numerator + denominator - 1) // denominator if role == EXPOSURE else numerator // denominator
    return result if result <= INT64_MAX else UNKNOWN


class Sums:
    """What `exact_sums` read: the row count, the exact total of each column, and the count of rows for each guard."""

    __slots__ = ("count", "totals", "guards")

    def __init__(self, count, totals, guards):
        self.count, self.totals, self.guards = count, totals, guards


def exact_sums(conn, tail, params, columns, guards=()):
    """Exact totals of non-negative integer columns, with no arithmetic on amounts inside the database.

    `tail` is the SQL after FROM (tables, joins, WHERE), `params` its bound values, `columns` the column expressions to add (fixed
    text written in this code base, never input) and `guards` conditions to count (fixed text too). For each column the database adds
    `x >> 32` and `x & 4294967295` separately: each high half is below 2^31 and each low half below 2^32, so up to 2^31 - 1 rows
    cannot reach 2^63, and Python puts them together exactly. A NULL is skipped, as `SUM` skips it.

    Refused with a closed error (the caller answers it as a store fault, never as a number): more than `ROW_LIMIT` rows, and any
    row whose column is negative (the schema forbids it by a CHECK; this makes the claim checked, not assumed). -> Sums."""
    select = ["COUNT(*)"]
    for column in columns:
        select.append("COALESCE(SUM(%s >> %d), 0)" % (column, _HALF))
        select.append("COALESCE(SUM(%s & %d), 0)" % (column, _LOW_MASK))
        select.append("COALESCE(SUM(CASE WHEN %s < 0 THEN 1 ELSE 0 END), 0)" % column)
    for guard in guards:
        select.append("COALESCE(SUM(CASE WHEN %s THEN 1 ELSE 0 END), 0)" % guard)
    row = conn.execute("SELECT " + ", ".join(select) + " FROM " + tail, params).fetchone()  # nosec B608 - fixed column and guard text, values bound
    count = row[0]
    if count > ROW_LIMIT:
        raise RowCountError()
    totals, position = [], 1
    for _column in columns:
        high, low, negative = row[position], row[position + 1], row[position + 2]
        position += 3
        if negative:
            raise MoneyError("negative")
        totals.append((high << _HALF) + low)
    return Sums(count, totals, list(row[position:]))


def exact_sum(conn, tail, params, column):
    """The exact total of one column (see `exact_sums`)."""
    return exact_sums(conn, tail, params, [column]).totals[0]
