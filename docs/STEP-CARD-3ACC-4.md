# Step record - Card 3acc-4: a label per time period in the alias table

Work item `card-3acc-4`. Branch `card-3acc-4`, from main `b10e169`. Audience: the lead, who keeps the alias table, and the reviewer.
Python 3.11 or newer, standard library only. Changes `tools/three_accounts/measure.py` (the alias table reader); nothing else in the program changes.

## In plain words

On 2026-10-23 at about 07:50 UTC the operator moved Laptop B's two Claude seats from subscription A to subscription C. The Leasewise store tells accounts apart by
the seat's config folder, not by subscription, so it keeps filing Laptop B's readings under the same store alias. The alias table could give an alias only one label
for all time: keeping A filed C's figures under A, and changing to C moved A's whole history to C.

The alias table may now give an alias a list of **periods**, each with its own label. A reading gets the label of the period that holds its observation time.
A table written the old way (`"alias": "A"`) works exactly as before.

## The format

A table value is either a label (`"A"`, `"B"` or `"C"`, for all time) or a non-empty list of periods. A period is an object with:

- `"label"`: `"A"`, `"B"` or `"C"` (required);
- `"from"` (optional): the start, inclusive;
- `"until"` (optional): the end, exclusive;
- no other keys.

Times are UTC text in exactly the form `YYYY-MM-DDTHH:MM:SSZ` (a real date; no fractions, no date-only, no `Z`-less form). Periods are in time order and never overlap:
every period except the last has an `until`; every period after the first has a `from` no earlier than the previous `until`; `from` must be before `until`. Gaps are allowed.

Example (a made-up alias):

```
{"main-one": [{"label": "A", "until": "2026-10-23T07:50:00Z"}, {"label": "C", "from": "2026-10-23T07:50:00Z"}]}
```

A reading in no period (a gap, or before the first `from`) is skipped and counted with the skipped ones, as an unmapped alias is.

## Mistakes in the table

The program exits 3 with one plain sentence and never prints a path or a value from the table:

| Mistake | Sentence after `not done:` |
| --- | --- |
| a value that is neither a label nor a list (and a table that is not an object) | the alias table must map an account alias to exactly A, B or C |
| an empty list | an alias with a list of periods needs at least one period |
| an entry that is not an object | each period must be an object with a label and optional from and until |
| an unknown key | a period may only have the keys label, from and until |
| a missing or bad label | a period label must be exactly A, B or C |
| a badly formed or impossible time | a period time must look like 2026-10-23T07:50:00Z |
| `from` not before `until` | a period must start before it ends |
| a period other than the last without `until` | every period except the last needs an until |
| a period after the first without `from` | every period after the first needs a from |
| periods out of order | periods of one alias must be in time order |
| overlapping periods | periods of one alias must not overlap |

## Choices the brief did not settle

1. **A period after the first without `from`** gets its own sentence (the brief said such a period needs a `from`).
2. **Order versus overlap:** a period that starts before the previous one starts, or ends no later than it starts, is "out of order"; otherwise a start before the previous `until` is "overlap".
3. **The reading's time** is its observation time in whole seconds (the store's milliseconds divided by 1000, cut), the same value `now` and `report` use.
4. **Checking happens when the table is read**, for every alias, so a mistake in an alias with no readings still stops the program.
5. **The table is not checked for readings that fall in no period of an alias that has them** beyond counting them as skipped.

## Tests

`tests/test_three_accounts_periods.py` (9 tests), with made-up aliases and the shapes of the real case. They cover: the repro (the 07:35 readings under A and the 07:52 readings under C
through `read_store`, `from_store` and `now_command`; A's weekly row shows 92%, C's 0%, no "possible switch" in the table or the `--explain` block, and the explanation output goes
through the shared `check_wording`); the store still opened with `mode=ro`; the old form and one all-time period giving byte-identical `from-store` and `now` output; a reading exactly at a
`from` (in) and at an `until` (out); readings in a gap or before the first `from` skipped and counted; and each mistake above exiting 3 with its sentence. Every earlier test passes untouched.
