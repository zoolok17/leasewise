# Live trial of the money reader: the operator's runbook

Mode: how-to. For: the operator, at the desktop where the gateway runs, in a maintenance window. Status: **rehearsed end to end on made-up data**
(`tests/trial/`, `docs/STEP-CARD-R1-READER-4-PREP.md`); **not yet run against the real gateway**. Leasewise commit to install: `<COMMIT>` (the lead fills
this in at the merge). Plan: lead decision D-221.

## What this trial does, in plain words

Leasewise has a "money reader" that asks the gateway for its own account of the month's spending, works out how much of it no lease accounts for,
and tells Leasewise. This trial runs that reader once, for real, on this machine, to prove three things: Leasewise accepts the reports, you can approve
and adopt one, and Leasewise's own `status` then calls the gateway's figures **usable** and the settled total matches the gateway's.

Nothing is installed. Everything lives in **one folder**. Leasewise only **reads** from the gateway: two read-only commands, `gateway report --json`
(about once a minute) and `gateway receipts --after N --json` (about once a minute; the settings below keep it from being asked every few seconds). It changes nothing on the gateway and nothing in agenttalk.
Stopping it is Ctrl-C in two windows, and undoing it is deleting the folder.

## Before you start

You need all of these. If one is missing, stop.

1. **The maintenance window is open** and the gateway runs an agenttalk release that includes the receipt-month and ledger-report changes (agenttalk
   pull requests #309 and #316). Check with the version command your agenttalk release documents, and compare with the version the lead names:
   `<the version the lead names>`. (Not scripted here, because it is your agenttalk's own command.)
2. **You are logged in as the Windows user the gateway runs as.** The receipt command reads that user's own files, so another user would read nothing
   useful. Check: `whoami` prints the gateway user.
3. **Python 3.11 or newer** is installed. Leasewise needs no other package, and **you install nothing**: never run `pip install` for this, because it
   would try to download a build tool from the network. This runbook runs Leasewise straight from the extracted source.
4. **Two files from the lead**: `leasewise-<COMMIT>.zip` (a source archive of the exact commit) and its SHA-256 checksum.
5. **Nothing else uses port 8443** on this machine (Leasewise's server listens only on `127.0.0.1`, which is this machine; it refuses any other address).

You will use **two PowerShell windows**, both opened as the gateway user. Window 1 runs the Leasewise server, window 2 runs the reader. Everything else
(the setup steps) you can run in a third window, or in window 2 before you start the reader.

## Step 1: choose the names

Edit the lines in angle brackets `<like this>` and run the block. Everything else is derived from them. Use plain letters and digits in aliases.

<!-- rehearse: replace -->
```powershell
$Trial        = Join-Path $env:LOCALAPPDATA 'leasewise-trial'   # the one folder; deleting it undoes the trial
$Py           = '<C:\path\to\python.exe>'                       # Python 3.11 or newer
$GatewayExe   = '<C:\path\to\agenttalk.exe>'                    # the gateway's program, an .exe
$GatewayPre   = @()                                             # extra leading arguments, if your program needs any (normally none)
$MachineAlias = 'desktop'                                       # this machine
$AccountAlias = '<account alias>'                               # the gateway's own metered account, as you want it named in Leasewise
$GatewayAlias = '<gateway alias>'                               # the gateway, as you want it named in Leasewise
$BudgetMicroEur = <budget in micro-euro>                        # the account's budget (1 euro = 1000000)
$ShareMicroEur  = <share in micro-euro>                         # the gateway's share of that budget (not above the budget)
$Archive      = '<C:\path\to\leasewise-COMMIT.zip>'
$ExpectedSha  = '<the SHA-256 the lead gave you>'
```

Then run this block. It makes the folder, works out the other names and saves them all to `names.ps1` so the other windows can load them:

<!-- rehearse: run -->
```powershell
$Src        = Join-Path $Trial 'src'                     # Leasewise's source, extracted
$Store      = Join-Path $Trial 'store\inventory.db'      # Leasewise's store
$StateDir   = Join-Path $Trial 'state'                   # the reader's own state
$Journal    = Join-Path $Trial 'journal'                 # an empty folder the reader insists on
$PolicyIds  = Join-Path $Trial 'policy-ids.json'
$ConfigPath = Join-Path $Trial 'reporter.toml'           # private: it holds the identity value
$Helper     = Join-Path $Trial 'newest-report.py'
$Names      = Join-Path $Trial 'names.ps1'
$BaseUrl    = 'http://127.0.0.1:8443'
$env:PYTHONDONTWRITEBYTECODE = '1'                       # keeps the source folder clean

New-Item -ItemType Directory -Force $Trial, $StateDir, $Journal, (Split-Path $Store) | Out-Null
$Saved = foreach ($Name in 'Trial','Py','GatewayExe','GatewayPre','MachineAlias','AccountAlias','GatewayAlias','BudgetMicroEur','ShareMicroEur',
                  'Archive','ExpectedSha','Src','Store','StateDir','Journal','PolicyIds','ConfigPath','Helper','Names','BaseUrl') {
    $Value = Get-Variable -Name $Name -ValueOnly
    if ($Value -is [array]) { '${0} = @({1})' -f $Name, (($Value | ForEach-Object { "'$_'" }) -join ',') }
    else { '${0} = ''{1}''' -f $Name, $Value }
}
$Saved += '$env:PYTHONDONTWRITEBYTECODE = ''1'''
Set-Content -Path $Names -Value $Saved -Encoding ascii
Test-Path $Names
```

It prints `True`. In window 1 and window 2 you will start by running `. "<the Trial folder>\names.ps1"` (the dot and the space matter).

## Step 2: check the archive, then unpack it

The checksum must match exactly. If it does not, **stop and tell the lead**; do not unpack.

<!-- rehearse: run -->
```powershell
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash
if ($Actual -ne $ExpectedSha) { throw 'The archive does not match its checksum. Stop here and tell the lead.' }
New-Item -ItemType Directory -Force $Src | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $Src
Get-ChildItem $Src | Select-Object -ExpandProperty Name
```

It prints the names of the unpacked items: `README.md`, `docs`, `pyproject.toml`, `quota_balancer`, `tests` and similar. From now on run every Leasewise
command from inside that folder:

<!-- rehearse: run -->
```powershell
Set-Location $Src
```

## Step 3: check Python and the gateway program

Both checks only read. Neither changes anything.

<!-- rehearse: run -->
```powershell
& $Py -c 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python is older than 3.11.' }
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'The gateway program could not print its report.' }
'{0} {1} {2}' -f $Rep.gateway_report_version, $Rep.child_cap_ready, $Rep.child_receipt_report_version
```

It prints the Python version (for example `3.12.4`), then `1 True 2`. If the second line differs (a different report version, `False` for the caps,
or an empty last number), **stop**: the gateway is not ready for this trial. Leasewise will not guess; it would only count a closed word such as
`status_shape` or `child_cap_not_ready` and send nothing.

## Step 4: set up the store

Leasewise's store is a file. It needs to know this machine, the gateway's account, and the gateway bound to that machine and account (Leasewise only
accepts a gateway's report from the machine **and** the account that own the gateway).

<!-- rehearse: run -->
```powershell
$Inventory = @"
{"version":2,
 "accounts":[{"alias":"$AccountAlias","vendor":"ovh","kind":"metered","budget_micro_eur":$BudgetMicroEur}],
 "machines":[{"alias":"$MachineAlias"}],
 "gateways":[{"alias":"$GatewayAlias","account":"$AccountAlias","machine":"$MachineAlias","share_micro_eur":$ShareMicroEur,"period":"utc_month"}],
 "projects":[{"alias":"trial"}],
 "teams":[{"project":"trial","alias":"team"}],
 "project_accounts":[{"project":"trial","account":"$AccountAlias"}],
 "seats":[], "models":[], "profiles":[], "limits":[]}
"@
$InventoryFile = Join-Path $Trial 'inventory.json'
Set-Content -Path $InventoryFile -Value $Inventory -Encoding ascii
& $Py -m quota_balancer --db $Store init
if ($LASTEXITCODE -ne 0) { throw 'init failed' }
& $Py -m quota_balancer --db $Store config-import --file $InventoryFile
if ($LASTEXITCODE -ne 0) { throw 'config-import failed' }
& $Py -m quota_balancer --db $Store policy-ids --json | Out-File -Encoding ascii $PolicyIds
if ($LASTEXITCODE -ne 0) { throw 'policy-ids failed' }
(Get-Content $PolicyIds -Raw | ConvertFrom-Json).gateways | ForEach-Object { $_.alias }
```

The first three commands print nothing when they work. The last line prints your gateway alias once. (No seats, models or limits are needed: this trial
does not make any lease.)

## Step 5: make the identity value and the private file

Leasewise knows the reader by a secret **identity value**. It is made here, on this machine, from the computer's secure random source. **It is never
printed, never logged and never pasted into any message, to anyone, ever.** It lives in a variable, in Leasewise's store, and in one private file.

<!-- rehearse: run -->
```powershell
$Bytes = New-Object byte[] 24
$Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$Rng.GetBytes($Bytes)
$Id = ($Bytes | ForEach-Object { $_.ToString('x2') }) -join ''
& $Py -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-ref $Id --enabled true
if ($LASTEXITCODE -ne 0) { throw 'identity-set failed' }

$GatewayReport  = @($GatewayExe) + $GatewayPre + @('gateway','report','--json')
$GatewayReceipt = @($GatewayExe) + $GatewayPre + @('gateway','receipts')
$AsToml = { param($Items) ($Items | ForEach-Object { "'$_'" }) -join ', ' }
$ConfigText = @"
version = 1
base_url = "$BaseUrl"
identity = "$Id"
machine = "$MachineAlias"
journal_root = '$Journal'
state_path = '$StateDir\reporter.db'
policy_ids = '$PolicyIds'
poll_seconds = 60

[gateway_receipts]
gateway = "$GatewayAlias"
admission_store = '$Trial\admission.db'
command = [$(& $AsToml $GatewayReceipt)]

[gateway_receipts.env]
LOCALAPPDATA = '$env:LOCALAPPDATA'

[gateway_report]
gateway = "$GatewayAlias"
account = "$AccountAlias"
command = [$(& $AsToml $GatewayReport)]

[gateway_report.env]
LOCALAPPDATA = '$env:LOCALAPPDATA'
"@
New-Item -ItemType File -Force $ConfigPath | Out-Null
icacls $ConfigPath /inheritance:r /grant:r "$($env:USERDOMAIN)\$($env:USERNAME):F" | Out-Null
Set-Content -Path $ConfigPath -Value $ConfigText -Encoding ascii
Remove-Variable Id, Bytes, ConfigText
(icacls $ConfigPath) -join "`n"
```

Why `poll_seconds = 60`: the reader looks at everything once per cycle, and the default cycle is 5 seconds. With 60, the gateway is asked for its report at most once a
minute and for receipts about once a minute (measured in the rehearsal; see the step card), and a report is never older than about a minute and a half when you adopt it.

Why the `LOCALAPPDATA` lines: the reader starts the gateway commands with almost no environment (it passes only `SYSTEMROOT`, `SYSTEMDRIVE`, `PATH`,
`TEMP` and `TMP`, plus the `env` table you give it). The gateway's program finds the ledger and the user's front-token file under the user's
application-data folder, so its variable **`LOCALAPPDATA`** is named in both `env` tables. The reporter itself never opens the front-token file.

The last command lists who can open the file: one line with the file and **your** user name and `(F)`, then `Successfully processed 1 files`. If any other name
appears, delete `reporter.toml`, and do this step again. Leasewise refuses to start with a file anyone else can open.

## Step 6: start it (two windows)

**Window 1: the Leasewise server.** It prints nothing while it works.

<!-- rehearse: window ServeArgs -->
```powershell
. "$env:LOCALAPPDATA\leasewise-trial\names.ps1"
Set-Location $Src
$ServeArgs = @('-m','quota_balancer','--db',$Store,'serve','--host','127.0.0.1','--port','8443')
& $Py @ServeArgs
```

**Window 2: the reader.** Start it after window 1 has been running for a few seconds. It prints nothing either.

<!-- rehearse: window RunArgs -->
```powershell
. "$env:LOCALAPPDATA\leasewise-trial\names.ps1"
Set-Location $Src
$RunArgs = @('-m','quota_balancer','reporter','run','--config',$ConfigPath)
& $Py @RunArgs
```

(If you changed the folder in step 1, type its real path instead of `$env:LOCALAPPDATA\leasewise-trial`.) A refusal to start prints one fixed sentence
and a closed code, for example `CONFIG_INVALID`; it never prints what the file said. See "If something goes wrong".

## Step 7: wait for the first report

In your third window, run this. It asks Leasewise's `status` every few seconds for up to two minutes.

<!-- rehearse: run -->
```powershell
$Deadline = (Get-Date).AddSeconds(120)
do {
    Start-Sleep -Seconds 5
    $Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
    $Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
} until ($Gateway.evidence -or (Get-Date) -gt $Deadline)
if (-not $Gateway.evidence) { throw 'No report arrived in two minutes. See "If something goes wrong".' }
'state={0} latest report number={1}' -f $Gateway.evidence.state, $Gateway.evidence.latest.seq
```

It prints `state=unadopted latest report number=1`. **Unadopted** is right: nothing is trusted until you adopt a report. Leasewise accepted the first
report. A new report follows about every minute.

## Step 8: compare the two fingerprints, then approve

The gateway has two policy fingerprints (a price one and a child-cap one). You approve the pair you have **seen**. First compare what the gateway
prints now with what Leasewise holds from the report it received. They must be equal; if they are not, **stop**.

<!-- rehearse: run -->
```powershell
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
$Held = $Gateway.report.policy_hashes
'price equal: {0}   cap equal: {1}' -f ($Rep.policy_hash -eq $Held.price), ($Rep.child_cap_policy_hash -eq $Held.child_cap)
if (($Rep.policy_hash -ne $Held.price) -or ($Rep.child_cap_policy_hash -ne $Held.child_cap)) { throw 'The fingerprints differ. Do not approve.' }
& $Py -m quota_balancer --db $Store gateway-approve --gateway $GatewayAlias --price-hash $Rep.policy_hash --child-cap-hash $Rep.child_cap_policy_hash
if ($LASTEXITCODE -ne 0) { throw 'gateway-approve failed' }
```

It prints `price equal: True   cap equal: True`, then `{"approved":true}`.

## Step 9: look at what blocks adoption, then adopt

`adopt_blockers` lists every reason the newest report cannot be adopted yet. After the approval it should be empty.

<!-- rehearse: run -->
```powershell
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
'state={0}  blockers=[{1}]  open items={2}' -f $Gateway.evidence.state, ($Gateway.evidence.adopt_blockers -join ','), @($Gateway.evidence.open_transitions).Count
```

If it prints `blockers=[]`, go on. If it names `not_fresh`, the newest report is older than 120 seconds: wait for the next one and run this block again.
If it names `open_transitions_unnamed`, the adopt command below names every open item for you.

Now the helper that finds the newest report's id. Leasewise's `status` deliberately shows no report id, so this tiny read-only script reads it from the
store. It changes nothing.

<!-- rehearse: run -->
```powershell
$HelperText = @'
import json, sqlite3, sys, time
db, alias = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
row = conn.execute("SELECT t.event_id, t.seq, t.settled_micro_eur, t.observed_at FROM gateway_totals t JOIN gateways g ON g.id = t.gateway_id "
                   "WHERE g.alias = ? ORDER BY t.observed_at DESC, t.seq DESC LIMIT 1", (alias,)).fetchone()
print(json.dumps({"event_id": row[0], "seq": row[1], "settled_micro_eur": row[2], "age_seconds": round(time.time() - row[3] / 1000.0)}))
'@
Set-Content -Path $Helper -Value $HelperText -Encoding ascii
```

Adopt the newest report. **It must be done within 120 seconds of that report**, so this block fetches it and adopts it straight away:

<!-- rehearse: run -->
```powershell
$New = (& $Py $Helper $Store $GatewayAlias) | ConvertFrom-Json
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
$Open = @($Gateway.evidence.open_transitions | ForEach-Object { '--resolve'; [string]$_.id })
"adopting report number $($New.seq), $($New.age_seconds) seconds old"
& $Py -m quota_balancer --db $Store gateway-adopt --gateway $GatewayAlias --evidence $New.event_id @Open
"exit code $LASTEXITCODE"
```

When it works it prints the report number and its age (a few seconds), then `{"adoption_id":1}` (the number grows with each adoption), then `exit code 0`. A refusal prints
one fixed sentence (`ADOPTION_REFUSED ... This report cannot be adopted now. Run status for the reason`; PowerShell may show it in red with extra lines about the
command: the sentence inside is the message) and `exit code 2`; the **reason** is in `status` (see "If an adopt is refused as too old").

## Step 10: check that it passed

"Passed" means all four are true at once: Leasewise accepted reports, you adopted one, `status` says **usable**, and the settled total Leasewise holds
equals the gateway's own. Run this when the gateway has been quiet for a minute (so the two totals are taken at the same moment):

<!-- rehearse: run -->
```powershell
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
$New = (& $Py $Helper $Store $GatewayAlias) | ConvertFrom-Json
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
$Settled = ($Rep.periods | Select-Object -Last 1).committed_micro_eur
'state={0}  adopted report number={1}' -f $Gateway.evidence.state, $Gateway.evidence.adopted.seq
'Leasewise settled={0}  gateway settled={1}' -f $New.settled_micro_eur, $Settled
if ($Gateway.evidence.state -ne 'usable') { throw 'status does not say usable' }
if ($New.settled_micro_eur -ne $Settled) { throw 'The settled totals differ.' }
'PASSED'
```

It prints `state=usable adopted report number=<n>`, two equal totals, and `PASSED`. If the gateway spent money between the two readings the totals differ
by exactly that spending: run the block again when it is quiet. A Leasewise total **above** the gateway's is never right: stop and tell the lead.

Let it keep running for a few minutes while the gateway works normally, then run this block again: new reports keep arriving (the report number grows),
and the state stays `usable`.

## Step 11: stop

Press **Ctrl-C** in window 2 (the reader), then in window 1 (the server). Both stop at once; nothing is left running. (The rehearsal ended them by process id instead, which is harsher; the reader restarted cleanly after it.) You can check that no Leasewise
process remains:

<!-- rehearse: run -->
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$Trial*" } | Select-Object ProcessId, Name
```

It prints nothing when everything has stopped. (Your own PowerShell window may show itself: ignore that.)

## Step 12: roll back

Rolling back is deleting the folder. The gateway, agenttalk and the rest of the machine were never changed.

<!-- rehearse: run -->
```powershell
Set-Location $env:USERPROFILE
Remove-Item -LiteralPath $Trial -Recurse -Force
Test-Path $Trial
```

It prints `False`. **What is left behind:** nothing, apart from the archive you were given (delete it if you like) and the variables in your
PowerShell windows (close them). No scheduled task, service, firewall rule, registry entry or change to agenttalk was made. Python wrote no files
into the source folder (`PYTHONDONTWRITEBYTECODE`), and the folder is the only place Leasewise wrote to, apart from short-lived files in your
temp folder that Leasewise removes itself.

## If an adopt is refused as too old (rehearsed)

A report must be adopted within 120 seconds. If you were slow, or the reader was stopped for a while, `gateway-adopt` prints its fixed refusal sentence
and exit code 2, and `status` says why:

<!-- rehearse: run -->
```powershell
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
'state={0}  blockers=[{1}]' -f $Gateway.evidence.state, ($Gateway.evidence.adopt_blockers -join ',')
```

You will see `not_fresh` in the blockers (and the state `stale` if a report was already adopted). **Nothing is broken and nothing needs repair.**
Make sure the reader (window 2) is running; it makes a new report at once when it starts and then about every minute. Wait for a report whose age is
under a minute, then run the Step 9 adopt block again.

## If something goes wrong

Everything the reader cannot do ends in **one closed word**, counted in `reporter status`. A word never carries a value from the gateway.

<!-- rehearse: run -->
```powershell
& $Py -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty money_reader | ConvertTo-Json -Depth 4
```

| What you see | What it means | What to do |
|---|---|---|
| Window 2 or 1 prints `CONFIG_INVALID`, `CONFIG_PERMISSIONS` or `CONFIG_UNRESOLVED` and stops | the file is wrong, other people can open it, or an alias is not in `policy-ids.json` | fix the file named by the sentence (never paste its contents anywhere); redo steps 4 and 5 if aliases changed |
| No report after two minutes; `status_failed` counts up | the gateway program could not run, ran too long, exited with an error, or its output was too large | run the Step 3 check by hand; check the paths in `reporter.toml` and the `LOCALAPPDATA` line |
| `status_shape` | the gateway's report is not in the form this reader knows | stop; tell the lead the gateway program's version |
| `child_cap_not_ready` | the gateway has no lease binding or its caps are not ready | stop; the gateway is not ready for the trial |
| `receipts_failed`, `generation_changed` | the receipts could not be read up to the report's number, or the ledger changed between the two commands | usually passes by itself next minute; if it repeats, run the receipts command by hand |
| `receipts_rolled_back` | the same ledger now reports a lower receipt number than the reader already added (for example a restored ledger copy) | **stop and tell the lead.** Saved sums cannot be taken back; the way out is `reporter reset --confirm` and a new adoption |
| `period_mismatch` | the gateway reports a month later than this computer's clock | check the clock |
| `receipts_exceed_settled`, `amount_invalid`, `unattributed_unavailable` | the gateway's figures are damaged or incomplete | nothing is sent; stop; tell the lead |
| `status` shows `transition_open` | Leasewise saw something change that it wants a person to look at (for example money no lease accounts for went up). Open items are listed in `status` under `open_transitions` | it is not a failure of the reader; do not adopt blindly; tell the lead what `status` lists. An adopt names every open item with `--resolve` (the Step 9 block does this) |
| `send_failed` | Leasewise did not answer (is window 1 running?) | the same saved report is sent again; nothing is lost |
| **`send_refused`** (the reader stopped itself) | Leasewise refused the waiting report five times in a row | see the next paragraph |
| `continuity_conflict`, `state_unavailable`, `reader_halted` | the saved state is damaged or the reader stopped for good | run `reporter reset --confirm` (it archives the old state and starts a fresh reader identity), then approve and adopt again |

**`send_refused`: `resume` or `reset`.** The report that is waiting was saved as fixed bytes. Fixing the configuration does not change them, and
`resume` only gives the same bytes five more tries. So:

- If the cause was on **Leasewise's side** (for example the gateway's account is registered wrongly in the store), correct it there, then run
  `reporter resume --confirm`: the same bytes are accepted.
- If the cause was on the **reader's side** (for example the `account` in `reporter.toml` was wrong), the waiting report can never be accepted: fix
  the file, then run `reporter reset --confirm` (it keeps the old state in an archive and starts a fresh reader identity), and approve and adopt again.

Both commands take `--config $ConfigPath`, work on local files only and never talk to Leasewise. Stop the reader (Ctrl-C) before using either.

## Tell the lead when you are done

Say only: passed or not passed, the step you stopped at, and the **closed word** you saw. **Never send the identity value, `reporter.toml`, or
any report.** The rehearsal that this runbook was checked with is described in `docs/STEP-CARD-R1-READER-4-PREP.md`.
