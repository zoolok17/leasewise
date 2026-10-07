# Live trial of the money reader: the operator's runbook

Mode: how-to. For: the operator, at the desktop where the gateway runs, in a maintenance window. Status: **rehearsed end to end on made-up data**
(`tests/trial/`, `docs/STEP-CARD-R1-READER-4-PREP.md`), and **passed against the real gateway on 2026-10-07, all 12 steps**. Leasewise commit to install: `<COMMIT>` (the lead fills
this in at the merge). Plan: lead decision D-221.

**How to paste.** Paste each block **whole** and press Enter **once**. Every block starts with `. {` and ends with `}`, so PowerShell runs it only when all of it has arrived, and if any line fails the **whole** block stops (a block that checks something and then acts never acts after a failed check). If anything is printed in red, **stop and tell the lead**; do not paste the next block.

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

## Two ways to run each step

Pasting a long block into a PowerShell window did not always work for the operator in the real trial, so **every step also comes as a small script**, one per step, in the
folder `docs\trial-r1-reader-4\` (it is in the source archive once step 2 has unpacked it, and the lead can give you the folder to copy anywhere). Each script holds the same
block, unchanged, after a short shared start (`trial-common.ps1`). The shared start, and the two step-6 scripts, begin with one entry check (`trial-guard.ps1`): it
stops, changing nothing, if the trial folder or `names.json` is a link, or if `names.json` names another folder.

- **Choose one form before step 1 and keep it to the end.** Do not mix them. A pasted block can need variables that only the pasted blocks before it leave in your window
  (step 8 does), so a run that pastes one step after running another as a script can fail.
- **Which form.** The script form was rehearsed with **PowerShell 7**: `pwsh -NoProfile -File '<script>'`. Use it where PowerShell 7 is installed and the machine's script policy
  lets the script run. Windows PowerShell 5.1 (`powershell -NoProfile -File '<script>'`) was **not** rehearsed with the scripts: it refused scripts on our machine. The machine's
  policy decides, and it must **never** be changed. If scripts cannot run, paste the blocks, for the whole run.
- **Check before step 1.** Run `docs\trial-r1-reader-4\trial-00-check.ps1` the same way. It changes nothing: it prints the PowerShell version and `scripts run here`. If it prints
  that, you may use the script form for the whole run. If the machine refuses it, paste the blocks for the whole run. If a script is refused part-way through, **stop and tell the
  lead**; do not switch forms in the middle.
- **Fill in the angle-bracket values in the step-1 script first** (`trial-01-setup.ps1`), the same values as the step-1 block asks for.
- **Each script runs in its own window and its own process.** Nothing is carried over in variables. What the steps leave on purpose stays: the files in the trial folder (the
  source, the store, the reader's state) and `names.json`, which step 1 writes and every later step reads. Every script loads `trial-common.ps1` from its own folder, so keep the
  folder's files together; the two step-6 scripts are the exception: they load `names.json` themselves, after the same entry check.
- **The two step-6 scripts run in two windows**, as the two blocks do: the server in one, the reader in the other.
- **Both forms use the default trial folder only** (`%LOCALAPPDATA%\leasewise-trial`): the step-6 windows read `names.json` from exactly there. The shared start stops with a plain
  message if `names.json` names another folder, and also if the trial folder or `names.json` is a link (nothing is changed; tell the lead).
- The line under each heading below names that step's script. If the machine's script policy refuses a script, see "Which form" above and **do not change the policy**.

## Step 1: choose the names, then make the private folder

Script form: `docs\trial-r1-reader-4\trial-01-setup.ps1`.

Edit the lines in angle brackets `<like this>` and run the block. Everything else is derived from them. Use plain letters and digits in aliases. The folder named
by `$Trial` **must not exist yet**: the next block refuses an existing folder without changing it, because step 12 deletes this folder and must only ever delete one
this runbook made. **Keep the default for `$Trial`**: the step-6 windows read `%LOCALAPPDATA%\leasewise-trial\names.json`, so another folder does not work, in either form.
If the block below says the folder already exists, an earlier trial's folder is still there: roll it back with step 12, or tell the lead. **Do not choose another name.** (The block's
own message says "Choose a new name"; this page replaces that advice, because the block itself cannot change.)

<!-- rehearse: replace -->
```powershell
$Trial        = Join-Path $env:LOCALAPPDATA 'leasewise-trial'   # the one new folder; deleting it undoes the trial
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

Then run this block. It does four things, in this order, and stops at the first problem:

1. it refuses a folder that already exists (nothing is changed);
2. it creates the folder and makes it **private**: only you, SYSTEM and Administrators can open it, nothing is inherited from the folder above, and it reads the access list
   back and compares the security identifiers (not the names) before anything else is written. This comes before the store, the identity value or any other secret;
3. it writes a small ownership marker inside (a random token and the folder's full path), which the rollback in step 12 checks before it deletes anything;
4. it saves all the names to `names.json` (UTF-8) so the two extra windows can load them without running any script.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Src        = Join-Path $Trial 'src'                     # Leasewise's source, extracted
$Store      = Join-Path $Trial 'store\inventory.db'      # Leasewise's store
$StateDir   = Join-Path $Trial 'state'                   # the reader's own state
$Journal    = Join-Path $Trial 'journal'                 # an empty folder the reader insists on
$PolicyIds  = Join-Path $Trial 'policy-ids.json'
$ConfigPath = Join-Path $Trial 'reporter.toml'           # private: it holds the identity value
$Names      = Join-Path $Trial 'names.json'
$BaseUrl    = 'http://127.0.0.1:8443'
$env:PYTHONDONTWRITEBYTECODE = '1'                       # keeps the source folder clean
$Utf8 = New-Object System.Text.UTF8Encoding($false)      # UTF-8 without a byte-order mark, for every file written below

# Who may open the private things: you (by security identifier), SYSTEM and Administrators. Names are never compared, only identifiers.
$Me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$Allowed = @($Me, 'S-1-5-18', 'S-1-5-32-544')
function Protect-Path {
    param([string]$Path, [bool]$Folder)
    $Acl = Get-Acl -LiteralPath $Path
    $Acl.SetAccessRuleProtection($true, $false)                          # nothing inherited from the folder above
    foreach ($Rule in @($Acl.Access)) { [void]$Acl.RemoveAccessRule($Rule) }
    $Flags = if ($Folder) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    foreach ($Sid in $Allowed) {
        $Who = New-Object Security.Principal.SecurityIdentifier($Sid)
        $Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($Who, 'FullControl', $Flags, 'None', 'Allow')))
    }
    Set-Acl -LiteralPath $Path -AclObject $Acl
}
function Assert-Private {
    param([string]$Path, [bool]$OwnList)                                  # $OwnList: the item has its own list and inherits nothing
    $Acl = Get-Acl -LiteralPath $Path
    if ($OwnList -and -not $Acl.AreAccessRulesProtected) { throw "$Path still inherits access from its folder. Stop here; nothing secret has been written." }
    $Seen = @()
    foreach ($Rule in $Acl.Access) {
        if ($Rule.AccessControlType -ne 'Allow') { continue }
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Allowed -notcontains $Sid) { throw "Someone else can open $Path. Stop here; nothing secret has been written." }
        if ($OwnList -and $Rule.IsInherited) { throw "$Path inherits an entry from its folder. Stop here; nothing secret has been written." }
        $Seen += $Sid
    }
    if ($Seen -notcontains $Me) { throw "You cannot open $Path. Stop here." }
}

if (Test-Path -LiteralPath $Trial) { throw "The folder $Trial already exists. Nothing was changed. Choose a new name for `$Trial and start again." }
New-Item -ItemType Directory -Path $Trial | Out-Null     # no -Force: an existing folder is an error
Protect-Path $Trial $true
Assert-Private $Trial $true

$Token = [guid]::NewGuid().ToString('N')                 # random
$Here = (Resolve-Path -LiteralPath $Trial).ProviderPath
[IO.File]::WriteAllText((Join-Path $Trial '.owner.json'), (ConvertTo-Json @{ token = $Token; path = $Here }), $Utf8)
New-Item -ItemType Directory -Path $StateDir, $Journal, (Split-Path $Store) | Out-Null

$Saved = [ordered]@{
    Trial = $Trial; Token = $Token; Py = $Py; GatewayExe = $GatewayExe; GatewayPre = @($GatewayPre); MachineAlias = $MachineAlias
    AccountAlias = $AccountAlias; GatewayAlias = $GatewayAlias; BudgetMicroEur = $BudgetMicroEur; ShareMicroEur = $ShareMicroEur
    Archive = $Archive; ExpectedSha = $ExpectedSha; Src = $Src; Store = $Store; StateDir = $StateDir; Journal = $Journal
    PolicyIds = $PolicyIds; ConfigPath = $ConfigPath; Names = $Names; BaseUrl = $BaseUrl
}
[IO.File]::WriteAllText($Names, ($Saved | ConvertTo-Json -Depth 3), $Utf8)
Test-Path -LiteralPath $Names
$ErrorActionPreference = 'Continue'
}
```

It prints `True`. If the folder existed it stops with "The folder ... already exists. Nothing was changed.": an earlier trial's folder is still there, so roll it back with step 12 or tell the lead. **Do not choose another name** (the step-6 windows would not find it; the block's own message says "Choose a new name", and this page replaces that advice because the block itself cannot change). If it stops with "Someone else can
open ...", the folder could not be made private: **stop and tell the lead**, because nothing secret may be written there.

In the two extra windows (step 6) the names are loaded with one line, `$N = Get-Content -LiteralPath <the Trial folder>\names.json -Raw -Encoding UTF8 | ConvertFrom-Json`;
no script is run, so the machine's script policy does not matter and you must not change it. If you ever close the window you are working in, open a new one and load
everything back with:
`$N = Get-Content -LiteralPath '<the Trial folder>\names.json' -Raw -Encoding UTF8 | ConvertFrom-Json; foreach ($P in $N.PSObject.Properties) { Set-Variable -Name $P.Name -Value $P.Value }`
(the helper functions of the block above are then missing; step 5 and the rollback tell you what to do).

## Step 2: check the archive, then unpack it

Script form: `docs\trial-r1-reader-4\trial-02-unpack.ps1`.

The checksum must match exactly. If it does not, **stop and tell the lead**; do not unpack.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash
if ($Actual -ne $ExpectedSha) { throw 'The archive does not match its checksum. Stop here and tell the lead.' }
New-Item -ItemType Directory -Force $Src | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $Src
Get-ChildItem $Src | Select-Object -ExpandProperty Name
$ErrorActionPreference = 'Continue'
}
```

It prints the names of the unpacked items: `README.md`, `docs`, `pyproject.toml`, `quota_balancer`, `tests` and similar. From now on run every Leasewise
command from inside that folder:

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
Set-Location $Src
$ErrorActionPreference = 'Continue'
}
```

## Step 3: check Python and the gateway program

Script form: `docs\trial-r1-reader-4\trial-03-check.ps1`.

Both checks only read. Neither changes anything.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
& $Py -c 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python is older than 3.11.' }
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'The gateway program could not print its report.' }
'{0} {1} {2}' -f $Rep.gateway_report_version, $Rep.child_cap_ready, $Rep.child_receipt_report_version
$ErrorActionPreference = 'Continue'
}
```

It prints the Python version (for example `3.12.4`), then `1 True 2`. If the second line differs (a different report version, `False` for the caps,
or an empty last number), **stop**: the gateway is not ready for this trial. Leasewise will not guess; it would only count a closed word such as
`status_shape` or `child_cap_not_ready` and send nothing.

## Step 4: set up the store

Script form: `docs\trial-r1-reader-4\trial-04-store.ps1`.

Leasewise's store is a file. It needs to know this machine, the gateway's account, and the gateway bound to that machine and account (Leasewise only
accepts a gateway's report from the machine **and** the account that own the gateway).

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
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
$InventoryFile = Join-Path $Trial 'inventory.json'     # inside the private folder: new files there inherit its private list
Set-Content -LiteralPath $InventoryFile -Value $Inventory -Encoding ascii
& $Py -m quota_balancer --db $Store init
if ($LASTEXITCODE -ne 0) { throw 'init failed' }
& $Py -m quota_balancer --db $Store config-import --file $InventoryFile
if ($LASTEXITCODE -ne 0) { throw 'config-import failed' }
& $Py -m quota_balancer --db $Store policy-ids --json | Out-File -Encoding ascii -LiteralPath $PolicyIds
if ($LASTEXITCODE -ne 0) { throw 'policy-ids failed' }
(Get-Content -LiteralPath $PolicyIds -Raw | ConvertFrom-Json).gateways | ForEach-Object { $_.alias }
$ErrorActionPreference = 'Continue'
}
```

The first three commands print nothing when they work. The last line prints your gateway alias once. (No seats, models or limits are needed: this trial
does not make any lease.)

## Step 5: make the private file, then the identity value

Script form: `docs\trial-r1-reader-4\trial-05-identity.ps1`.

Leasewise knows the reader by a secret **identity value**. It is made here, on this machine, from the computer's secure random source. **It is never
printed, never logged and never pasted into any message, to anyone, ever.** It lives in a variable, in Leasewise's store (inside the private folder of step 1), and in
one private file.

The order matters: the file that will hold the secret is created **empty**, made private and checked **first**; only then is the value made and written. If any
check fails the block stops before a secret exists. (If you opened a new window after step 1, run step 1's block 2 again only in a **new** trial folder; do not
continue in a window that lacks the helper functions.)

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
New-Item -ItemType File -Path $ConfigPath | Out-Null           # empty: nothing secret yet
Protect-Path $ConfigPath $false                                # its own list: you, SYSTEM, Administrators; nothing inherited
Assert-Private $ConfigPath $true                               # read back by identifier; stops here if anything is wrong

$Bytes = New-Object byte[] 24
$Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$Rng.GetBytes($Bytes)
$Id = ($Bytes | ForEach-Object { $_.ToString('x2') }) -join ''
& $Py -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-ref $Id --enabled true
if ($LASTEXITCODE -ne 0) { throw 'identity-set failed' }
Assert-Private $Store $false                                   # the store now holds the value: it must still be private

$GatewayReport  = @($GatewayExe) + $GatewayPre + @('gateway','report','--json')
$GatewayReceipt = @($GatewayExe) + $GatewayPre + @('gateway','receipts')
$ToToml = { param($Value) '"' + ([string]$Value).Replace('\', '\\').Replace('"', '\"') + '"' }       # a TOML basic string: only \ and " need escaping
$ListToml = { param($Items) ($Items | ForEach-Object { & $ToToml $_ }) -join ', ' }
$ConfigText = @"
version = 1
base_url = $(& $ToToml $BaseUrl)
identity = $(& $ToToml $Id)
machine = $(& $ToToml $MachineAlias)
journal_root = $(& $ToToml $Journal)
state_path = $(& $ToToml (Join-Path $StateDir 'reporter.db'))
policy_ids = $(& $ToToml $PolicyIds)
poll_seconds = 60

[gateway_receipts]
gateway = $(& $ToToml $GatewayAlias)
admission_store = $(& $ToToml (Join-Path $Trial 'admission.db'))
command = [$(& $ListToml $GatewayReceipt)]

[gateway_receipts.env]
LOCALAPPDATA = $(& $ToToml $env:LOCALAPPDATA)

[gateway_report]
gateway = $(& $ToToml $GatewayAlias)
account = $(& $ToToml $AccountAlias)
command = [$(& $ListToml $GatewayReport)]

[gateway_report.env]
LOCALAPPDATA = $(& $ToToml $env:LOCALAPPDATA)
"@
[IO.File]::WriteAllText($ConfigPath, $ConfigText, $Utf8)       # UTF-8 without a byte-order mark; the file keeps its private list
Assert-Private $ConfigPath $true                               # and it is still private after the write
Remove-Variable Id, Bytes, ConfigText
(Get-Acl -LiteralPath $ConfigPath).Access | ForEach-Object { $_.IdentityReference.Value }
$ErrorActionPreference = 'Continue'
}
```

The last line lists who can open the file: **your** user name, `NT AUTHORITY\SYSTEM` and `BUILTIN\Administrators`, and nobody else. The reader itself refuses to start if anyone
else can open the file or if the file inherits access from its folder; it allows you, SYSTEM and Administrators.

Strings in the file are written as TOML **basic strings** with `\` and `"` escaped, and the file is UTF-8 without a byte-order mark, so a folder name with an apostrophe
or a letter such as `Ü` is safe (the rehearsal used both).

Why `poll_seconds = 60`: **this is a trial setting, not the reporter's default.** The reader looks at everything once per cycle, and the default cycle is 5 seconds; the older
seat-lease receipt pass would then ask the gateway for receipts every 5 seconds. With 60, the gateway is asked for its report at most once a
minute and for receipts about once a minute (measured in the rehearsal; see the step card). This does **not** promise how old a report is when you adopt it: Leasewise
refuses to adopt one older than 120 seconds, and that refusal is the guard (see "If an adopt is refused as too old").

**About the identity value in the command above.** `identity-set` takes the value as a command argument, so for under a second, while that one command runs, the same Windows user and
administrators can see it in the process list. The same people can already read `reporter.toml`, so this adds no new reader. **Close this PowerShell window when you are done**: the
value stays in the window's memory until then (the block removes its variables, but closing the window is the sure way).

Why the `LOCALAPPDATA` lines: the reader starts the gateway commands with almost no environment (it passes only `SYSTEMROOT`, `SYSTEMDRIVE`, `PATH`,
`TEMP` and `TMP`, plus the `env` table you give it). The gateway's program finds the ledger and the user's front-token file under the user's
application-data folder, so its variable **`LOCALAPPDATA`** is named in both `env` tables. The reporter itself never opens the front-token file.

## Step 6: start it (two windows)

Script form: `docs\trial-r1-reader-4\trial-06a-server.ps1`, `docs\trial-r1-reader-4\trial-06b-reader.ps1`.

**Window 1: the Leasewise server.** It prints nothing while it works. (It reads the default trial folder.)

<!-- rehearse: window ServeArgs -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$N = Get-Content -LiteralPath "$env:LOCALAPPDATA\leasewise-trial\names.json" -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $N.Src
$ServeArgs = @('-m','quota_balancer','--db',$N.Store,'serve','--host','127.0.0.1','--port','8443')
& $N.Py @ServeArgs
}
```

**Window 2: the reader.** Start it after window 1 has been running for a few seconds. It is **not silent**: it prints one short line per cycle, about once a minute.

<!-- rehearse: window RunArgs -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$N = Get-Content -LiteralPath "$env:LOCALAPPDATA\leasewise-trial\names.json" -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $N.Src
$RunArgs = @('-m','quota_balancer','reporter','run','--config',$N.ConfigPath)
& $N.Py @RunArgs
}
```

A normal line looks like this (the rehearsal printed exactly this):

```
{"blocked": null, "journal": "ok", "new_events": 0, "sent": 1}
```

`sent` is how many things were delivered in that cycle (`1` when a report was accepted, `0` when none was due), `blocked` is `null` when Leasewise answered (it says
`unreachable` when the server in window 1 is not running, and `credential` when Leasewise refused the identity), and `journal` stays `ok`. A refusal to start prints one fixed
sentence and a closed code, for example `CONFIG_INVALID`; it never prints what the file said. See "If something goes wrong".

## Step 7: wait for the first report

Script form: `docs\trial-r1-reader-4\trial-07-wait.ps1`.

In your third window, run this. It asks Leasewise's `status` every few seconds for up to two minutes.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Deadline = (Get-Date).AddSeconds(120)
do {
    Start-Sleep -Seconds 5
    $Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
    $Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
} until ($Gateway.evidence -or (Get-Date) -gt $Deadline)
if (-not $Gateway.evidence) { throw 'No report arrived in two minutes. See "If something goes wrong".' }
'state={0} latest report number={1}' -f $Gateway.evidence.state, $Gateway.evidence.latest.seq
$ErrorActionPreference = 'Continue'
}
```

It prints `state=unadopted latest report number=1`. **Unadopted** is right: nothing is trusted until you adopt a report. Leasewise accepted the first
report. A new report follows about every minute.

## Step 8: compare the two fingerprints, then approve

Script form: `docs\trial-r1-reader-4\trial-08-approve.ps1`.

The gateway has two policy fingerprints (a price one and a child-cap one). You approve the pair you have **seen**. First compare what the gateway
prints now with what Leasewise holds from the report it received. They must be equal; if they are not, **stop**.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
$Held = $Gateway.report.policy_hashes
'price equal: {0}   cap equal: {1}' -f ($Rep.policy_hash -eq $Held.price), ($Rep.child_cap_policy_hash -eq $Held.child_cap)
if (($Rep.policy_hash -ne $Held.price) -or ($Rep.child_cap_policy_hash -ne $Held.child_cap)) { throw 'The fingerprints differ. Do not approve.' }
& $Py -m quota_balancer --db $Store gateway-approve --gateway $GatewayAlias --price-hash $Rep.policy_hash --child-cap-hash $Rep.child_cap_policy_hash
if ($LASTEXITCODE -ne 0) { throw 'gateway-approve failed' }
$ErrorActionPreference = 'Continue'
}
```

It prints `price equal: True   cap equal: True`, then `{"approved":true}`.

## Step 9: look at what blocks adoption, then adopt

Script form: `docs\trial-r1-reader-4\trial-09a-blockers.ps1`, `docs\trial-r1-reader-4\trial-09b-adopt.ps1`.

`adopt_blockers` lists every reason the newest report cannot be adopted yet. After the approval it should be empty.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
'state={0}  blockers=[{1}]  open items={2}' -f $Gateway.evidence.state, ($Gateway.evidence.adopt_blockers -join ','), @($Gateway.evidence.open_transitions).Count
$ErrorActionPreference = 'Continue'
}
```

If it prints `blockers=[]`, go on. If it names `not_fresh`, the newest report is older than 120 seconds: wait for the next one and run this block again.
If it names `open_transitions_unnamed`, Leasewise has **open items** that a person must look at. The adopt block below stops when there are any, prints them, and does not adopt:
see "Open items" at the end of this step.

Adopt the newest report. **It must be done within 120 seconds of that report**, so this block reads the newest report from `status` and adopts it straight away.
It adopts **only when there are no open items**. If there are any, it prints each one's id and kind and stops without adopting anything.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
$OpenItems = @($Gateway.evidence.open_transitions)
if ($OpenItems.Count -gt 0) {
    $OpenItems | ForEach-Object { 'open item {0}: {1}' -f $_.id, $_.kind }
    throw 'There are open items. Do not adopt. Tell the lead what is listed above.'
}
$New = $Gateway.evidence.latest
'adopting report number {0}, {1} seconds old' -f $New.seq, [math]::Round($New.age_ms / 1000)
& $Py -m quota_balancer --db $Store gateway-adopt --gateway $GatewayAlias --evidence $New.event_id
"exit code $LASTEXITCODE"
$ErrorActionPreference = 'Continue'
}
```

When it works it prints the report number and its age (a few seconds), then `{"adoption_id":1}` (the number grows with each adoption), then `exit code 0`. A refusal prints
one fixed sentence (`ADOPTION_REFUSED ... This report cannot be adopted now. Run status for the reason`; PowerShell may show it in red with extra lines about the
command: the sentence inside is the message) and `exit code 2`; the **reason** is in `status` (see "If an adopt is refused as too old").

**Open items.** An open item is something Leasewise noticed that a person must decide about (for example, money that no lease accounts for went up). The block never
resolves one for you, because that is the one place where Leasewise wants a person to look. If the block stops: **do not adopt, and tell the lead what it listed.** You adopt only
when there are no open items, or after the lead has answered for the items named (the lead will give you the exact command, which names those items on purpose).

## Step 10: check that it passed

Script form: `docs\trial-r1-reader-4\trial-10-check.ps1`.

"Passed" means all four are true at once: Leasewise accepted reports, you adopted one, `status` says **usable**, and the settled total Leasewise holds
equals the gateway's own. Run this when the gateway has been quiet for a minute (so the two totals are taken at the same moment):

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
# The month the reader used: the UTC month of its newest report. The gateway's figure for THAT month; 0 when the gateway has no row for it.
$Month = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$Gateway.evidence.latest.observed_at).UtcDateTime.ToString('yyyy-MM')
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
if ([DateTime]::UtcNow.ToString('yyyy-MM') -ne $Month) { throw "The month changed between the two readings ($Month). Wait for the next report and run this block again." }
$Row = @($Rep.periods | Where-Object { $_.period -eq $Month })
$Settled = if ($Row.Count -eq 1) { $Row[0].committed_micro_eur } else { 0 }
'state={0}  adopted report number={1}  month={2}' -f $Gateway.evidence.state, $Gateway.evidence.adopted.seq, $Month
'Leasewise settled={0}  gateway settled={1}' -f $Gateway.evidence.latest.settled_micro_eur, $Settled
if ($Gateway.evidence.state -ne 'usable') { throw 'status does not say usable' }
if ($Gateway.evidence.latest.settled_micro_eur -ne $Settled) { throw 'The settled totals differ.' }
'PASSED'
$ErrorActionPreference = 'Continue'
}
```

It prints `state=usable adopted report number=<n> month=<yyyy-mm>`, two equal totals, and `PASSED`. The gateway's figure is the one for **the month the reader used**; a gateway that
has spent nothing this month may list an older month only, or no month at all, and then its figure for this month is 0 (the rule the reader itself follows), so a quiet start of the
month passes. If the gateway spent money between the two readings the totals differ
by exactly that spending: run the block again when it is quiet. A Leasewise total **above** the gateway's is never right: stop and tell the lead.

Let it keep running for a few minutes while the gateway works normally, then run this block again: new reports keep arriving (the report number grows),
and the state stays `usable`.

## Step 11: stop

Script form: `docs\trial-r1-reader-4\trial-11-stopped.ps1`.

Press **Ctrl-C** in window 2 (the reader), then in window 1 (the server). Ctrl-C is a **request** to stop: **wait until both windows show their prompt again** before you go
on to step 12. (Ctrl-C was **not** rehearsed: the rehearsal ended the two processes through the process objects it had started, which is harsher; the reader restarted cleanly
after that.) In the server window, Ctrl-C prints a few lines that end in `KeyboardInterrupt`; this is normal, and it means the server has stopped. Then check that no Leasewise process remains:

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ('*' + [WildcardPattern]::Escape($Trial) + '*') } | Select-Object ProcessId, Name
$ErrorActionPreference = 'Continue'
}
```

It prints nothing when everything has stopped. (Your own PowerShell window may show itself: ignore that.)

## Step 12: roll back

Script form: `docs\trial-r1-reader-4\trial-12-rollback.ps1`.

Rolling back is deleting the folder, **but only the folder this runbook made**. The block compares the folder's resolved path and the token in its ownership marker with
the ones saved in step 1, and stops if either differs; it deletes nothing in that case. (`$Token` and `$Trial` come from the window you worked in; in a new window load them with
the line in step 1 first.) The gateway, agenttalk and the rest of the machine were never changed.

**Why the block also looks for links.** A delete can follow a folder link (a junction or a symbolic link) out of the folder being deleted and remove files somewhere else.
This runbook never makes a link, so a link inside the trial folder means something else put it there. Before it deletes, the block therefore walks the whole folder **without ever
going into a link** and looks at every entry. If the trial folder **itself** is a link, it stops first thing, before it opens or lists anything. (The script form does the same: the shared start stops on a link, and the
step-12 script does not enter the source folder.) If it finds **any** link inside (a
folder or a file, at any depth), it stops with "The trial folder contains a link. Nothing was deleted. Tell the lead." Only a folder with no link in it is deleted. (A few other special entries, such as a cloud-sync placeholder, count as links too: that is
the safe side.)

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $env:USERPROFILE
if ((New-Object IO.DirectoryInfo($Trial)).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { throw 'The trial folder contains a link. Nothing was deleted. Tell the lead.' }
$Marker = Get-Content -LiteralPath (Join-Path $Trial '.owner.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Here = (Resolve-Path -LiteralPath $Trial).ProviderPath
if (($Marker.token -ne $Token) -or ($Marker.path -ne $Here)) { throw 'This is not the folder this runbook made. Nothing was deleted.' }
$Links = 0                                               # a link anywhere inside: look at every entry, never go into a link (a linked trial folder was refused above)
$Pending = New-Object System.Collections.Generic.Stack[string]
$Pending.Push($Here)
while ($Pending.Count -gt 0) {
    foreach ($Entry in (New-Object IO.DirectoryInfo($Pending.Pop())).GetFileSystemInfos()) {
        if ($Entry.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Links++ }
        elseif ($Entry -is [IO.DirectoryInfo]) { $Pending.Push($Entry.FullName) }
    }
}
if ($Links -gt 0) { throw 'The trial folder contains a link. Nothing was deleted. Tell the lead.' }
Remove-Item -LiteralPath $Trial -Recurse -Force
Test-Path -LiteralPath $Trial
$ErrorActionPreference = 'Continue'
}
```

It prints `False`. **What is left behind:** nothing, apart from the archive you were given (delete it if you like) and the variables in your
PowerShell windows (close them). No scheduled task, service, firewall rule, registry entry or change to agenttalk was made. Python wrote no files
into the source folder (`PYTHONDONTWRITEBYTECODE`), and the folder is the only place Leasewise wrote to, apart from short-lived files in your
temp folder that Leasewise removes itself.

## If an adopt is refused as too old (rehearsed)

Script form: `docs\trial-r1-reader-4\trial-09a-blockers.ps1`.

A report must be adopted within 120 seconds. If you were slow, or the reader was stopped for a while, `gateway-adopt` prints its fixed refusal sentence
and exit code 2, and `status` says why:

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
'state={0}  blockers=[{1}]' -f $Gateway.evidence.state, ($Gateway.evidence.adopt_blockers -join ',')
$ErrorActionPreference = 'Continue'
}
```

You will see `not_fresh` in the blockers (and the state `stale` if a report was already adopted). **Nothing is broken and nothing needs repair.**
Make sure the reader (window 2) is running; it makes a new report at once when it starts and then about every minute. Wait for a report whose age is
under a minute, then run the Step 9 adopt block again. This refusal is the guard against adopting an old report; the reader's one-minute rhythm does not replace it.

## If something goes wrong

Script form: `docs\trial-r1-reader-4\trial-help-status.ps1`.

Everything the reader cannot do ends in **one closed word**, counted in `reporter status`. A word never carries a value from the gateway.

<!-- rehearse: run -->
```powershell
. {
$ErrorActionPreference = 'Stop'
& $Py -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty money_reader | ConvertTo-Json -Depth 4
$ErrorActionPreference = 'Continue'
}
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
| `status` shows `transition_open`, or the adopt block stops and lists open items | Leasewise saw something change that it wants a person to look at (for example money no lease accounts for went up). The items are listed in `status` under `open_transitions`, and the adopt block prints them | it is not a failure of the reader. **Do not adopt; tell the lead what is listed.** The adopt block never resolves an item by itself; you adopt only with no open items, or after the lead has answered for the items named |
| Step 12 stops with "The trial folder contains a link" | a junction or symbolic link is inside the trial folder; a delete could follow it out and remove files elsewhere | **stop; keep the folder as it is and tell the lead.** Do not delete it by hand |
| A delete fails or is refused (step 12 or any other) | something holds a file, or you may not delete it | keep the folder and tell the lead; **never try a stronger delete** (no `-Force` tricks, no `takeown`, no `icacls`, no `robocopy /MIR`) |
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
