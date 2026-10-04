# Step record - Card R019: the runner's "error after the resume" test no longer depends on machine load

Work item `card-r019-runner-test` (residual risk R-019). Branch `card-r019-runner-test` from main `593647c4a145c376128f131aae52bf2faa6100ed`. Tests only; the product is unchanged.

## In plain words

One test starts a small program, makes the runner fail on purpose, and checks that the program was still ended and everything closed. It found the program through a handle that the test's helper takes when it first looks at the program's marker file. On a busy machine the deliberate failure could come before that first look, so there was no handle and the test crashed with a `KeyError` instead of checking anything. The test now makes sure the handle is taken before it fails the runner. It checks exactly what it checked before.

## What changed

- `tests/test_card7l6a1b_runner.py`: the helper `Probe` gained `look()` (the part of `is_set` that takes the handles; `is_set` calls it first, so the runner's behaviour is the same) and `hold(path)` (look until the process is held, up to 10 seconds, else fail).
- `Deadlines.test_an_error_after_the_resume_still_ends_the_program_and_closes_everything`: the fault calls `probe.hold(marker)` just before it raises. Same assertions: the runner raises, the program is gone, every handle is closed.

## Evidence

- Reproduced on demand before the fix: a probe that never looks on the runner's calls gives the same `KeyError` on `boom.pid`.
- After the fix the same forced condition passes; the file passes 20 of 20 runs while the card 1-4 tests run alongside, and alone.
- The runner is not at fault: the fault came while the program was alive, and the runner ended it every time.

## Siblings checked

The stop test (`test_a_stop_ends_the_read_as_interrupted_with_the_job_ended`) is safe: it waits for the handle (`ready=[marker]`) before the stop. Tests that end by a deadline or by the program's own exit let the runner poll the probe for the whole run, so the handle is taken by the runner's own looks; they are not changed.
