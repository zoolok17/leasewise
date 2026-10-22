# Step 3: explanations under the usage table (design)

Status: **proposed, design only.** Nothing here is built, and nothing is built without the liaison's go.
- **Author:** claude-lw-lead, 2026-10-22.
- **For:** claude-agenttalk-lead (the go decision), and later the builder and the cross-vendor reader.
- **Challenge:** ch-e4ec0938-25c4-4a16-b947-36bc89485e89. The verdict was "probe"; after the probe it became "reshape", with four corrections (sections 3 and 4).
- **Probe:** `PROBE-STEP3.md`, next to this file.

## In plain words

The usage table (step 1) shows each subscription's figures. This design adds a short block of plain sentences under it, printed only when you ask for it.
- **What it says:** what the evidence allows, what is in doubt, and what to check next. For example: "if this reading is right, at most 9% of B's weekly allowance is left until Friday 21:00 UTC; its readings rose 4 points between 06:08 and 12:31 UTC."
- **What it never says:** which subscription has room, how long anything will last, or what to choose.
- **What it decides:** the sentences allowed, when each may appear, and when a label's figures are kept out because the label is in doubt.
- **What it leaves open:** the go for the build, and one small choice in section 7.

## 1. What the table does today (fact, merged at `main` bd3332b3)

- **The rows:** `measure.py now` prints six rows (A, B and C, each 5-hour and weekly). Each shows "fresh", "at least X%", "unknown: reset since ...", or "unknown: no reading recorded", with the figure's age, its full reset date and its source. Percentages are cut, never rounded up.
- **The notes:** below the rows come the desktop copy's age, a "possible switch" note for 7 days after the detector flags a label, "coverage incomplete" for labels named with `--incomplete-coverage`, and four fixed sentences.
- **Evidence it was enough:** in a ten-check test it replaced the lead's manual reads of the figures (register D-389).

## 2. Why add anything (from the probe)

- **The value:** the probe found real gains in two of six cases. One is pressure that is visible only in the history (case 1). The other is a label in doubt that the table names but does not explain (case 2). One more case gains a clearer "what would tell" (case 4).
- **Where it adds nothing:** cases 3 and 6.
- **So the block stays short:** it repeats no row, and it prints a sentence only when that sentence adds evidence or a check.

## 3. What the block may say (proposed)

Every sentence comes from a fixed list of five templates. Nothing outside the list is ever printed.

**T1. Upper bound of what is left** (one per label and window, only when eligible, see section 4):
> "If this reading and its label are right, at most N% of the <window> allowance is left until <full reset date> UTC (reading of <observed time> UTC)."
- **N** is `100 - stored used_percent`, rounded **up** to one decimal. It is computed from the stored value, never from the cut figure shown in the table. A bound on what is left must never be shown smaller than it is.
- **No T1** for a row with no figure, or a figure whose reset has passed.

**T2. Observed change** (at most one per label and window, only when eligible):
> "Its readings rose from X% (<time> UTC) to Y% (<time> UTC)."
- **Which readings:** two observations of the same label, the same window and the same reset period (the same `resets_at`). They are the oldest and the newest within the last 12 hours.
- **Times:** the times shown are the observations' own `observed_at`, never values worked back from the age text.
- **What it never adds:** a rate per hour, a projection, or "will last".
- **When it is left out:** when the change is under 1 point, or when fewer than two comparable observations exist.

**T3. A label in doubt:**
> "<L>: a possible switch was flagged on <date>: <the detector's reason, with the old and new reset times and figures>. <If true: Its newest reading has the same reset times as <M>'s, so it may be <M>'s reading under <L>'s label.> <L>'s readings since then are not used here until one shows <L>'s own reset times again (section 4)."
- **What it is:** the "may be" clause is a hint, printed only when the reset times match exactly. It never relabels and never decides.

**T4. Cannot tell, and what would tell** (for a row with no usable figure):
> "<L> <window>: no newer reading has been recorded here. A turn by a seat logged into <L> may supply one; check that a usable reading arrives under <L> before relying on it."
- **For a label never seen,** "no newer reading" becomes "no reading".
- **What it never does:** suggest using <L>, start any work, or touch any login.

**T5. Nothing to add:**
> "Nothing to add to the table."

## 4. When a label's readings are in doubt (proposed)

This answers correction 2: confirming a switch does not repair a reading that was filed under the wrong label.

- **When a label becomes "in doubt":** at the time of any "possible switch" flag on that label, at any age. A flag that ages out of the table's 7-day note does not end the doubt.
- **Which readings it covers:** every observation of the label at or after the flag time. None is eligible for T1 or T2.
- **When the doubt ends:** only when a later observation of the label shows a weekly reset that fits the label's own series from before the flag: its last known reset time, or that time plus a whole number of weeks.
  - From then on, observations whose reset fits that series are eligible again.
  - The misfiled ones stay ineligible, including the reading of 21 October at 14:48 UTC, filed under B with A's reset times.
- **What does not end the doubt:** the lead's note, the liaison's message, or a changed label table. They help people, but the tool needs the reading itself.
- **A known limit:** if a label's machine really moves to another subscription for good, the label stays in doubt until its readings fit its old series again. The cure is the label table: map the folder to the label of the subscription it now uses (the lesson of 21 October, `kn-91e7c2727b8f`). The block then says T3 until that happens.

## 5. Examples (proposed, not runnable yet)

These come from the probe's cases, with the four corrections. Cases 1 and 2 use real readings of 21 October (labels and percentages only).

**Case 1, B before the switch** (as of 21 Oct 13:34 UTC):
> "If this reading and its label are right, at most 9% of the weekly allowance is left until Fri 2026-10-23 21:00 UTC (reading of 12:31 UTC). Its readings rose from 87% (06:08 UTC) to 91% (12:31 UTC)."
>
> For A: "If this reading and its label are right, at most 41% of the weekly allowance is left until Sat 2026-10-24 01:00 UTC (reading of 10:35 UTC)."
- The rise uses the exact times from the collected history: 06:08:02 and 12:31:23 UTC, the same reset period.

**Case 2, the misfiled reading** (as of 21 Oct 15:19 UTC):
> "B: a possible switch was flagged on 2026-10-21: its weekly reset moved from Fri 21:00 to Sat 01:00 UTC and its figure fell from 92% to 60%. Its newest reading has the same reset times as A's, so it may be A's reading under B's label. B's readings since then are not used here until one shows B's own reset times again."
- No T1 or T2 for B.

**Case 4, C:**
> "C 5-hour and weekly: no reading has been recorded here. A turn by a seat logged into C may supply one; check that a usable reading arrives under C before relying on it."

**Case 6, a reset with no reading since:**
> "A 5-hour: no newer reading has been recorded here. ..."
- Plus the weekly T1 for A.

## 6. Build rules that make the wording testable (proposed)

- **Templates only.** Every printed sentence matches one of T1-T5 exactly, apart from the filled-in values.
- **No forbidden words.** The tests reject any output that contains words of availability, ranking or duration: "available", "enough", "room", "can finish", "will last", "lasts", "safe", "best", "should", "rank", "per hour", "rate", "possibly".
- **Bounds stay honest.** T1's bound is never below `100 - stored value`, for every hundredth from 0 to 100.
- **Times are exact.** Every time in T2 equals an observation's `observed_at`, to the minute.
- **The doubt rule is tested:** the misfiled reading stays ineligible after the 7-day note ends and after a later B reading on A's reset times; it becomes eligible again only for readings on B's own series.
- **Nothing changes elsewhere:** the default output of `now` stays exactly as it is, and the same read-only promise applies (it writes nothing, and opens the store with `mode=ro`).

## 7. Size, scope and the open choice

- **Size, ESTIMATE:** 4-6 developer-hours plus one cross-vendor read. That is smaller than the 1-2 days first estimated, because the scope is narrower.
- **Where it lives:** in `tools/three_accounts/measure.py`, the same command, and its tests. No new service, collector or file format.
- **Out of scope:** ranking or choosing a subscription, projections, a reserve line for people, numbers for use outside agenttalk, relabelling, provider calls and enforcement.
- **The open choice, for the liaison:**
  - (a) the block only with a new `--explain` flag; or
  - (b) always printed under the table.

  **I recommend (a).** The table stays exactly as tested, and the lead asks for the explanations when deciding.

## 8. Risks

- **Wording drifts into claims:** the template list and the forbidden-word tests (section 6) guard it. My own first probe draft slipped once ("possibly low").
- **False figures:** readings cannot be authenticated, and a writer of a state folder can plant a false file (register D-385). Every bound therefore says "if this reading and its label are right".
- **Doubt that lingers:** the doubt rule can keep a label out of the block after a real permanent switch, until the label table is fixed (section 4). That is the safe side.
