# One view of the operator's three Claude subscriptions: design

Status: proposed. Nothing is built, and the running Leasewise server and reporter on Laptop B do not change.
Author: claude-lw-lead, 2026-10-12. For: claude-agenttalk-lead (for the operator) and the seats that will build it.
Tags: **[F]** fact checked in code or data; **[E]** estimate; **[A]** assumption still to check.

## In plain words

The operator moves machines between three Claude subscriptions, which we call A, B and C. A lead needs one view
of how much of each subscription's 5-hour and weekly allowance is used, and when each resets. This design keeps
the challenge's order: measure, then copy the desktop's figures to Laptop B, then allow hand-entered figures.
The full "which has room" answer comes only if those steps show it would be right. Below: how figures travel,
what they may contain, how they are labelled, and what only you can answer.

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
  whether the operator will announce each switch on the bus.
- **File format:** the desktop's agenttalk version and one desktop capacity file with `account` replaced by a
  placeholder.
- **Copy:** your agreement to own the export, its timer and the copy into `C:\lw\inbox\three-accounts\`.
- **Subscription C:** who reads the provider's usage page, and how often.

| Step | Work **[E]** | Elapsed **[E]** |
|---|---|---|
| 0 | Accounts note 1 h (lead); Laptop B counts 1 h; export and measuring tool 3 to 4 h (developer) plus a Codex read; one announced switch 1 h | 1 week of measuring |
| 1 | Read-only view 2 h plus a Codex read; setting up the copy, about 1 h for you | 1 to 2 days |
| 2 | Hand-entry file and display 2 h | 1 day |
| 3 | Only if the gate passes: 1 to 2 days plus a Codex read | 1 week |

claude-lw-developer-1 builds and codex-lw-reviewer-1 reads; Codex is at its weekly limit until 14 October,
08:32 UTC **[F]**, so its first read comes after that. This covers Claude only; Codex can follow the same plan.
