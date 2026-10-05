# Step record - Card R1-READER-4-linksafe: the trial rollback and the kit refuse to delete a folder that contains a link

Work item `card-r1-reader-4-linksafe`. Branch `card-r1-reader-4-linksafe` from main `93939771251dbe2a74ad1b72d88a5900022a6ff0` (M-034). Runbook and kit only: no
product code. Synthetic data only; nothing real is run. This record is new (it does not extend `STEP-CARD-R1-READER-4-PREP.md`, which describes the merged card).

## In plain words

A delete can follow a folder **link** (a junction or a symbolic link) out of the folder it is cleaning and remove files somewhere else. Today the liaison found exactly
that in a test of another tool, and suspended its cleanup. Our trial rollback (step 12) and our rehearsal kit both delete a folder with `Remove-Item -Recurse -Force`,
and on the operator's desktop (most likely Windows PowerShell 5.1) that command is known to follow directory links in some cases. The trial folder never contains a link
by itself, but nothing checked it.

Now it is checked. Before step 12 deletes anything it walks the trial folder and looks at every entry **without going into any link**. If it finds one link, anywhere,
it stops with "The trial folder contains a link. Nothing was deleted. Tell the lead." and deletes nothing. The kit does the same before it deletes its own scratch
folders. The runbook's "If something goes wrong" table now also says: when a delete fails or is refused, keep the folder, tell the lead, and never try a stronger delete.

## The liaison's rule (deletion safety, 2026-10-05)

Before deleting a folder tree, check it contains no link (reparse point) without walking into links; if it does, delete nothing and tell the lead. When a delete
fails, keep the folder and tell the lead; never escalate to a stronger delete. (Also: never `robocopy <empty> <dir> /MIR` without `/XJ`, never recursive `takeown`
or `icacls` on a folder about to be deleted. The runbook and the kit use none of these.)

## What changed

1. **Step 12 of `docs/TRIAL-R1-READER-4.md`**, inside its `. { ... }` block, after the ownership-marker check and before `Remove-Item`: the trial folder itself is
   checked, then a stack walk over `[IO.DirectoryInfo]` / `GetFileSystemInfos()` looks at each entry's `Attributes` for `ReparsePoint` and pushes only entries that are
   **not** links onto the stack. No `Get-ChildItem -Recurse` is used. Any link (a folder or a file, at any depth, hidden or not) throws the sentence above. A
   cloud-sync placeholder is a reparse point too and also stops it: the safe side. One paragraph before the block gives the reason.
2. **"If something goes wrong"**: two table rows (the link sentence, and a failed or refused delete).
3. **`tests/trial/rehearse.ps1`**: `Count-Links` (the same walk) and `Remove-Scaffolding` (counts first, deletes only with no link); the kit's only
   `Remove-Item -Recurse` (the existing-folder drill's scaffolding) goes through it.
4. **The link drill** (`rehearse.ps1`, next to the other line-by-line drills): in the scratch root, a trial folder made by the real step 1 holds a junction three levels
   down (`deep\inner\link-to-outside`) pointing at a sentinel folder **outside** the trial folder (also in the scratch root). The real step 12 block is pasted into a live
   PowerShell process one line at a time. Result (in the rehearsal): the refusal sentence was printed, the trial folder and the junction are still there, and the sentinel
   file and the sentinel folder's listing are unchanged. Then the junction is removed **on its own** with `[IO.Directory]::Delete(<junction>)` (not recursive), the sentinel
   is checked again, and the scaffolding is removed through `Remove-Scaffolding`.

## Safety of the drill itself

Every path the drill creates or deletes is under the rehearsal root in the seat's scratch folder. Nothing outside it is named by any delete. The only delete that could
reach a link is the junction's own, by `[IO.Directory]::Delete` without the recursive flag.

## Gates

Rehearsal once from a clean start (`idle-older`, with the apostrophe-and-non-ASCII application-data folder): passed in 347 s, bound 20 minutes, with every earlier
drill, the existing line-by-line drill and the new link drill; the identity value is in no file outside the trial folder (0 files); repository status unchanged.
The full suite, ruff and bandit are in the reply to the lead.
