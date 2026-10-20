# One view of the operator's three Claude subscriptions: design

Status: proposed. Nothing is built, and the running Leasewise server and reporter on Laptop B do not change.
Author: claude-lw-lead, 2026-10-12; section 6 added 2026-10-13. For: claude-agenttalk-lead (for the operator) and
the seats that will build it.
Tags: **[F]** fact checked in code or data; **[E]** estimate; **[A]** assumption still to check.

## In plain words

The operator moves machines between three Claude subscriptions, which we call A, B and C. A lead needs one view
of how much of each subscription's 5-hour and weekly allowance is used, and when each resets. This design keeps
the challenge's order: measure, then copy the desktop's figures to Laptop B, then allow hand-entered figures.
The full "which has room" answer comes only if those steps show it would be right. Below: how figures travel,
what they may contain, how they are labelled, and what only you can answer. Section 6 adds a reserve for the
people who use the same subscriptions themselves, and shows their use as its own category.

## What is true today

- agenttalk keeps one small file per seat, `<seat>.capacity.json` in the bus's `state` folder, with the latest
  5-hour and weekly percentage, each with its reset time and the time it was observed **[F]** (agenttalk 0.99.0
  `store.py:7570-7576`, `capacity.py:116-163`).
- Its `account` field is `claude:<Windows user>:home-<hash of the config folder>` **[F]** (`capacity.py:189-198`).
  The label follows the folder: if a folder is logged into another subscription, the label silently stays.
- A figure exists only after a seat runs a turn, and it counts as current for 10 minutes **[F]** (`capacity.py:53`).
  Laptop B's figures arrive at :04 and :34 past each hour, which is when my half-hourly checks run **[F]**
  (Leasewise store, 10 to 12 October).
- Within a reset period a percentage only rises; Leasewise keeps the highest one per period **[F]**
  (`DESIGN.md` section 5). A's weekly figure is 44%, resetting on 17 October, 01:00 UTC **[F]**.
- The Leasewise server listens on 127.0.0.1 only **[F]** (M-047), so the desktop cannot send to it today.

## 1. The steps, and what I would change

The steps stay as adopted, with these changes:

1. **Step 0(c) is answered by the code.** The label follows the folder **[F]**. The step should instead test two
   remedies: one config folder per subscription on each machine, so a switch means using another folder **[A]**
   (the operator may prefer logging in and out); and a switch detector **[A]**: each subscription likely has its
   own weekly reset time, so a reset time that changes when no reset has passed marks that label "uncertain"
   until the accounts note confirms it.
2. **Copy a cleaned extract, never the raw files.** The raw file holds the Windows user name (section 3).
3. **One program serves step 0(b) and step 1.** The desktop export is also the measuring tool: Laptop B keeps
   each copy, and that history is the week's count. Laptop B's own count needs no new tool; its store already
   holds every reading since 10 October **[F]**.
4. **Show an old figure as a floor.** Within its reset period it reads "at least 44% at 21:34"; after its reset
   it reads "reset since, no new figure". Old figures then stay useful instead of "unknown".
5. **The step-1 view is a read-only command** that prints a table. It writes nothing into Leasewise and starts no
   service.
6. **Make the stop rule countable.** "Unknown" means no figure in the current reset period for the window asked.
   "Most questions" means more than half of the 30-minute samples in the week, per subscription in use. "A label
   was wrong" means one switch where the view's label differed from the accounts note.
7. **Defer a reporter on the desktop.** The design's end state is every machine sending to one coordinator
   **[F]** (`DESIGN.md` section 5). That needs the server opened to the private network, a change to the running
   server, so it waits until step 3 proves its worth.

## 2. Data that moves between machines

- **What:** one file, `desktop-capacity.json`: its export time, then one row per subscription and window with
  the label, machine, provider, window, percent used, reset time, observed time, source (`claude_stream` or
  `claude_statusline`) and number of seats.
- **Direction and owner:** desktop to Laptop B only. The desktop lead owns the export and its timer, every
  5 minutes **[E]**, into `C:\lw\inbox\three-accounts\` (new, outside `C:\lw\live`). Laptop B only reads.
- **Safe writes:** the export writes under a temporary name, then renames, so half a file is never read. A file
  that fails the shape check is skipped and shown as "desktop copy unreadable".
- **Staleness:** every row shows the figure's age and the copy's age. A copy older than 15 minutes **[E]** adds
  "desktop copy late", and the floor rule applies to all its rows.

## 3. Privacy

- The export keeps only the fields in section 2 (a list of what to keep, not of what to drop). It drops the
  `account` text, which holds the Windows user name **[F]**, the `plan_type` and `limit_id` codes, seat names and
  the context figures.
- These files hold no prompts, keys or session text **[F]** (`capacity.py:14-16`).
- The map from a folder's account text to A, B or C lives only in a small local table on each machine.
- If any value contains `@`, `:\`, `home-` or the local user name, the export writes nothing and records a count.
- A hand entry holds only the label, window, percentage, time, who entered it and "provider usage page".

## 4. Keeping the measures apart

Each number carries its kind, machine and age. The kinds are never merged:

1. **seat:** the percentage the provider sent to a seat during a turn, from the capacity files. The view reads
   Laptop B's own files the same way as the desktop copy; Leasewise's highest-per-period figure stays inside
   Leasewise.
2. **manual:** a figure a person copied from the provider's usage page (step 2). It is shown beside a seat
   figure, never in place of one, and neither updates the other's time.
3. **refused:** a seat was refused or parked on a usage limit. This is a yes or no with a reset time, never
   turned into "100%".

The step-3 answer names its basis, for example (proposed output, nothing runs yet): "B: weekly at least 71%
(seat, desktop, 8 min old), resets Thu 03:00 UTC".

## 5. What I need from you, and estimates

- **Accounts note:** which machine uses A, B and C today; how many Claude config folders each machine has;
  whether the operator will announce each switch on the bus (answered 2026-10-13: no; see section 6.4).
- **File format:** the desktop's agenttalk version and one desktop capacity file with `account` replaced by a
  placeholder.
- **Copy:** your agreement to own the export, its timer and the copy into `C:\lw\inbox\three-accounts\`.
- **Subscription C:** who reads the provider's usage page, and how often (answered 2026-10-13: nobody; see
  section 6.4).

| Step | Work **[E]** | Elapsed **[E]** |
|---|---|---|
| 0 | Accounts note 1 h (lead); Laptop B counts 1 h; export and measuring tool 3 to 4 h (developer) plus a Codex read; one announced switch 1 h | 1 week of measuring |
| 1 | Read-only view 2 h plus a Codex read; setting up the copy, about 1 h for you | 1 to 2 days |
| 2 | Hand-entry file and display 2 h | 1 day |
| 3 | Only if the gate passes: 1 to 2 days plus a Codex read | 1 week |

claude-lw-developer-1 builds and codex-lw-reviewer-1 reads; Codex is at its weekly limit until 14 October,
08:32 UTC **[F]**, so its first read comes after that. This covers Claude only; Codex can follow the same plan.

## 6. Addendum, 2026-10-13: people who use the same subscriptions

Status: proposed, design only. The operator approved both measures in outline on 2026-10-12 **[F]** (liaison note
20261013-074323-853642-CZbi); they are two of four, and the other two are outside this design.

### In plain words

The people who pay for a subscription also use it themselves, outside agenttalk: the operator does, and a client
team will too. This section adds two things. First, a reserve for people: each team has a line, for example 70% of
the weekly allowance, and past it the team's lead starts urgent work only, so the rest stays for people. Second,
Leasewise shows a rise that no agent caused as "outside agenttalk": a normal category, never an error or a switch.
Section 6.4 adapts the design to the operator's answers: nobody announces a switch, and nobody reads C. It leaves
open one choice for you: which line our own team uses (section 6.3).

### 6.1 A reserve for people

- **What the line is measured on.** The provider gives one weekly figure per subscription, and it covers agents and
  people together **[F]** (`ACCOUNTS-NOTE.md`, "Use not from any seat"). So the line is on that total: agents start
  non-urgent work only while the figure is below 70%. When people use more, the agents step back sooner, which is
  what a reserve is for. A line on the agents' own share would need a split that we can only guess (section 6.2),
  so I do not recommend it.
- **Where it sits.** Leasewise already has a 90% ceiling that blocks all new work, and keeps cold sweeps out above
  80% **[F]** (`DESIGN.md` lines 177-178). The people's line is a second, lower line per team: below it, any work;
  from it up to the ceiling, urgent work only; at the ceiling, nothing new. A line set above the ceiling changes
  nothing.
- **Urgent** means what the team rule already says: reads that gate a merge and their fixes, a broken `main`, and
  what the liaison or the operator asks for **[F]** (the operator's weekly limit rule of 2026-10-07).
- **Which figure decides.** The newest weekly figure in the current reset period. An older one counts as "at least"
  (section 1, change 4), so an old figure already past the line is enough to step back. A lead deciding during a
  turn sees the figure its previous turn left, not one from the turn it is in **[F]** (`agenttalk capacity show`
  at 07:47 UTC on 13 October, mid-turn, still showed the lead's 01:23 UTC figure). With hourly checks that figure
  can be about an hour old. An old figure below the line still allows new work, with its age shown, because
  people's use since then stays invisible until a seat on that subscription runs again.
- **Who sets it.** The operator, one number per team (in a client team, its owner). A lead or a seat never changes
  it. Where it is stored is for step 1 to settle; Leasewise already knows each seat's team **[F]** (`DESIGN.md`
  line 272).
- **Warnings.** The view shows "near the people's line" from 10 points **[E]** below it, and "past the people's
  line" from the line on. These replace today's hand-run messages to the liaison at 60% and 80%.
- **What it does not cover.** It is about starting new work: a turn already running finishes, so a long build
  started at 69% can end above 70%. The hourly wake-up checks keep running past the line and each costs a little
  (about half a point of A's weekly allowance per check on the lead's old, very long conversation **[F]**, same
  liaison note). Leases are advisory today **[F]** (register D-286, D-288), so Leasewise only shows that the line is
  passed and the lead steps back; nothing is refused by a machine. Two teams sharing a subscription read the same
  figure against their own lines. The 5-hour window gets no line: it refills within hours, and the ceiling covers
  it.

A row of the step-1 view would read (proposed output, nothing runs yet): "A: weekly at least 52% (seat, Laptop B,
6 h old); people's line 70%: below; resets Sat 17 Oct 01:00 UTC".

### 6.2 Use from outside agenttalk, shown as its own category

- **What it is.** A rise between two seat figures of one subscription in the same reset period, when no seat on
  that subscription ran a turn in between, apart from the turn that took the second figure. Leasewise shows it as
  "outside agenttalk": expected, never an error, never a switch. The switch detector looks only at reset times that
  move and at figures that fall **[F]** (register D-342), so such a rise does not trip it.
- **Why "about".** The turn that took the second figure is itself part of the rise, so the amount is approximate.
  A's rise on 12 October would read (proposed output): "outside agenttalk: about 7 points of the 5-hour window and
  1 point of the weekly, on 12 Oct".
- **When agents were working,** a rise cannot be split between agents and people. It is shown as "during agent
  work", with no guess at the share.
- **What it needs.** Proof that no seat on that subscription ran. Leasewise's design already defines that proof for
  another purpose: an idle reading stays current only if no turn ran since it (`DESIGN.md` lines 183-188). Until
  every turn asks Leasewise for a lease, the step-0 measuring tool uses the seats' own files, where each turn leaves
  a new observed time **[A]**. If a machine using the subscription does not report (today the desktop, on
  agenttalk 0.98.0 **[F]**, `ACCOUNTS-NOTE.md`), the view says "cannot tell" instead.
- **Rules it does not change.** The design already treats such use as "unmanaged use": coverage stays incomplete,
  and a figure counts as current for 10 minutes only **[F]** (`DESIGN.md` lines 179-188). This category changes
  what is shown, not those rules. Because the operator uses all three subscriptions, coverage stays incomplete for
  A, B and C.
- **Privacy.** It records amounts, never what a person did. The view shows totals per day; exact times stay in the
  local store of the machine that measured them, so a lead does not get a timetable of when a person worked. Our
  accounts note keeps its timed rows while we measure; the operator can ask to drop the times.
- **Where it shows.** In the step-1 view, one line per subscription ("outside agenttalk this week: about N points"),
  and in the daily note next to the weekly figure. It sends no message by itself; only the people's line warns.
  Because the line is on the total, this line also shows why a team reached its line early.

### 6.3 Your choice, and estimates

- **Which line for our team.** Today we step back at 80% (the 2026-10-07 rule) and I tell the liaison at 60% and
  80%. I recommend the operator's example, 70%, applied by hand from now on: it needs no build, A's weekly figure
  is 52% **[F]** (reporter status, 13 Oct 01:23 UTC), and it resets on 17 October, 01:00 UTC **[F]**. I would then
  warn at 60% and step back at 70%.
  **Answered 2026-10-13: no line for our team** **[F]** (liaison note 20261013-080639-047698-FXmk). The
  subscriptions exist only for this work, so the agents use them as the work needs and wait for the resets if all
  run out. The setting stays for teams whose people share a subscription, such as a client team.
- **Work [E]:** the line and its warnings in the step-1 view, 1 h; the category in the measuring tool and the view,
  2 to 3 h. Both go into step 1. Neither changes card 3acc-0, which waits for its Codex read.

### 6.4 Nobody announces a switch, and nobody reads C

The operator answered the two open questions on 13 October **[F]** (liaison note 20261013-074622-599015-pkis):
real users will forget to announce a switch, so Leasewise must notice one from the readings alone, and an
announcement is only a bonus; and nobody reads subscription C's usage page. The design leaned on being told in
three places, and on someone reading C in one. Each changes as follows.

- **The detector itself needs no change.** Card 3acc-0 already flags a switch from the readings alone: a weekly
  reset time that moves more than 5 minutes before the earlier one has passed, or a weekly figure that falls more
  than 1 point within one period **[F]** (`tools/three_accounts/measure.py` lines 229-239, card head 17739faa).
  It cannot see a switch to a subscription whose reset time is within those 5 minutes and whose figure is the
  same or higher, and a switch while every seat is idle shows only at the next turn. Real users most likely have
  one subscription per provider, so this should be rare.
- **Section 1, change 1** said a flagged label stays "uncertain" until the accounts note confirms it. Instead, the
  view shows that folder's figures as "unknown subscription" until its weekly reset time matches a label we
  already know; each subscription keeps the same weekday and hour for its weekly reset **[A]**. The lead then logs
  the switch in the accounts note from that evidence, not from being told. A reset time that matches no known
  label stays "unknown subscription".
- **Section 1, change 6** judged "a label was wrong" against the accounts note. It now means a switch the detector
  missed or named wrongly, found later in the readings themselves (a later flag, or a reset time that shows the
  label was wrong).
- **The step-0 plan** (section 5 table) counted on one announced switch. That stays only as a test arranged in
  advance with the operator, so that the detector is checked against a switch we know about. Nothing in use waits
  for an announcement.
- **Step 2 (hand-entered figures) is paused.** It existed mainly for C, and nobody will read C's page. C shows "no
  figures: no seat uses it", never "0%". If a seat is ever logged into C, its figures arrive like any other, as
  "unknown subscription" (C's reset time was never read), until the lead logs it.

The repository copy of this design (card 3acc-0's branch) still has the old wording in sections 1 and 5; step 1
brings it up to date with this section.
