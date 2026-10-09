# Switching on usage readings on Laptop B: the operator's runbook

Mode: how-to. For: the operator, at Laptop B (the machine the Leasewise team's seats run on). Status: **rehearsed end to end on made-up data** in the
lead's scratch folder (`docs/STEP-CARD-7B-ON.md`); **not yet run for real**. Leasewise commit to install: `<COMMIT>` (the lead fills this in at the
hand-over). Approved by claude-agenttalk-lead on 2026-10-09 (answer to q-9ddb21b0c31c): first on this machine, for our own team's two accounts only.

## What this does, in plain words

agenttalk keeps a small usage file for every seat. Each file says how full an account's **5-hour** and **weekly** allowances are. Leasewise can show
those figures, but until now nothing carried them to it. This runbook switches that on:

- the Leasewise **reporter** reads our team's usage files, picks the freshest figure for each account and allowance, and sends it to a Leasewise
  that runs **on this machine only** (address `127.0.0.1`);
- it covers **two accounts**: our Claude account and our Codex account;
- it **only reads** agenttalk's files, and only the usage files in our team's own agenttalk folder (`C:\lw\team\.agenttalk\state`). It never reads a
  file of another team and never reads a folder outside `C:\lw`;
- **nothing leaves this machine.** No figure crosses to another machine in this first step;
- it does **not** touch the gateway, the money reader or limit events, and it changes nothing in agenttalk or in any seat. Seats run exactly as before.

Everything lives in **one new folder**, `C:\lw\live`. Deleting that folder (step 15) undoes it. You install nothing: never run `pip install` for this.

**Done when** the reporter's status shows accepted figures for both accounts after a real turn of each seat, and shows no warning (step 13 checks this, and only counts figures newer than a baseline you record in step 12).

**How long.** About 45 minutes of your time from the first script to step 12 (most of it is copying two texts and reading what each script prints), then a wait
for one real turn of a Claude seat and of the Codex seat (the lead can give each a small task), then step 13.

## Before you start

You need all of these. If one is missing, stop.

1. **PowerShell 7** (`pwsh`). The scripts were rehearsed with it. Windows PowerShell 5.1 was **not** rehearsed with them and refused scripts on our machines earlier. The machine's
   script policy decides what may run, and it must **never** be changed. If the check in step 0 says scripts cannot run, stop and tell the lead.
2. **Python 3.11 or newer**, with its full path (for example the team's `C:\lw\venv\Scripts\python.exe`). Leasewise needs no other package.
3. **Three things from the lead**: the folder `docs\switchon-7b` (the scripts; copy it anywhere you like, for example to `C:\lw\switchon-7b`, and keep its files together),
   `leasewise-<COMMIT>.zip` (a source archive of the exact commit; **put it inside `C:\lw`**, for example `C:\lw\leasewise-<COMMIT>.zip`: step 1 refuses an archive anywhere else or behind a link) and its SHA-256 checksum.
4. **Nothing else uses port 8443** on this machine (Leasewise listens only on `127.0.0.1`, which is this machine; it refuses any other address).
5. **The account texts**: you will copy two lines from your own screen in step 8. See step 8 for how to get them; do not collect them earlier than that.

You will use **two extra PowerShell windows** at the end: window 1 runs the Leasewise server, window 2 runs the reporter.

## How to run each step

Every step is **one small script** in the `switchon-7b` folder. Run it like this, from any PowerShell 7 window (use the real path to the folder):

```
pwsh -NoProfile -File 'C:\lw\switchon-7b\switchon-04-store.ps1'
```

- **Each script runs in its own process.** Nothing is carried over in variables. What a step leaves on purpose stays in `C:\lw\live`; `names.json` there holds the names
  step 1 chose, and every later script reads it.
- **Keep the folder's files together.** Every script loads `switchon-common.ps1` (and, through it, `switchon-guard.ps1`) from its own folder.
- **Every script starts with an entry check** (`switchon-guard.ps1`). It stops, changing nothing, if **any folder on the way** from `C:\lw` down to the live folder, `names.json`, the agenttalk
  folder, its `state` folder, the archive or any file a step uses is a link; if a path is outside `C:\lw`; if the live folder and the agenttalk folder are the same or one lies inside the other;
  or if `names.json` was made for another folder or port. Only a short list of names is read back from `names.json`; every path is worked out again each time. If you ever see "is a link" or "is not a
  plain path inside C:\lw", stop and tell the lead.
- **No link anywhere in the live folder.** At the start of every step, before anything is read and before any Python command runs, the entry check also walks the **whole** live folder
  (never following a link) and stops if any entry in it is a link: a junction, a symbolic link or any other kind. That one rule covers the source package, the store, the state folder, the reporter's
  database and the settings. It stops with a message too if the folder holds more than 50,000 entries, which it never should. The remaining limit, stated plainly: the live folder is private (only you,
  SYSTEM and Administrators can write in it), so a link could appear between the check and its use only if one of those made it.
- **If a script stops with a message, read it.** Each message says whether anything was changed. The scripts never print a secret, an account text, a user name or a seat name.
- **Never set an `LW_SWITCHON_...` environment variable.** They exist only for the lead's test run in a scratch folder, which needs a switch and all of its settings together. If one is present
  in your window the scripts stop at once with a plain message and change nothing: close that window and open a new one.
- Each block below is exactly the script's body (a test keeps them identical). You only ever **run the script**; you never paste a block.

## Where things live

| What | Where |
|---|---|
| The one new folder (private: only you, SYSTEM and Administrators can open it) | `C:\lw\live` |
| Leasewise's source, extracted from the archive | `C:\lw\live\src` |
| Leasewise's store (it holds this machine's identity value, so it is private too) | `C:\lw\live\store\inventory.db` |
| The reporter's settings (private: identity value and the account texts) | `C:\lw\live\reporter.toml` |
| The reporter's own state | `C:\lw\live\state` |
| The names every script reads | `C:\lw\live\names.json` |
| The baseline of step 12 | `C:\lw\live\baseline.json` |
| **Read only**: our team's agenttalk folder | `C:\lw\team\.agenttalk\state` |

Nothing is put in `%LOCALAPPDATA%`, your user profile or any other team's folder.

## Step 0: check that scripts run here

Script: `switchon-00-check.ps1`. It changes nothing.

<!-- script: switchon-00-check.ps1 -->
```powershell
'PowerShell ' + $PSVersionTable.PSVersion.ToString()
'scripts run here'
```

It must print `PowerShell 7...` and `scripts run here`. If the machine refuses it, stop and tell the lead.

## Step 1: choose the names, then make the private folder

Script: `switchon-01-setup.ps1`. **Edit the three lines in angle brackets first**: the full path of Python, the full path of the archive, and the checksum the lead gave you. The other names
(`laptop-b`, `claude-main`, `codex-main`, `leasewise-team`) are Leasewise's own names for this machine, the two accounts and the project; keep them unless the lead says otherwise.

<!-- script: switchon-01-setup.ps1 -->
```powershell
'== Step 1: names and the private folder'

$Py            = '<C:\path\to\python.exe>'                      # Python 3.11 or newer
$Archive       = '<C:\path\to\leasewise-COMMIT.zip>'            # the archive the lead gave you; it must lie inside C:\lw
$ExpectedSha   = '<the SHA-256 the lead gave you>'
$MachineAlias  = 'laptop-b'                                     # this machine, as Leasewise names it
$ClaudeAlias   = 'claude-main'                                  # our Claude account, as Leasewise names it
$CodexAlias    = 'codex-main'                                   # our Codex account, as Leasewise names it
$ProjectAlias  = 'leasewise-team'                               # the project both accounts belong to in Leasewise

$Src        = Join-Path $LiveRoot 'src'                         # Leasewise's source, extracted
$Store      = Join-Path $LiveRoot 'store\inventory.db'          # Leasewise's store
$StateDir   = Join-Path $LiveRoot 'state'                       # the reporter's own state
$Journal    = Join-Path $LiveRoot 'journal'                     # an empty folder the reporter insists on
$Names      = Join-Path $LiveRoot 'names.json'

if ($Py -like '*<*' -or $Archive -like '*<*' -or $ExpectedSha -like '*<*') { throw 'Fill in the three values in angle brackets first. Nothing was changed.' }
if (($ExpectedSha -cnotmatch '^[0-9A-Fa-f]{64}$') -or (($MachineAlias, $ClaudeAlias, $CodexAlias, $ProjectAlias | Where-Object { $_ -cnotmatch '^[a-z0-9][a-z0-9-]{0,62}$' }).Count -gt 0)) { throw 'The checksum or a name is not in the allowed form. Nothing was changed.' }
Assert-PythonFile $Py
Assert-PlainFile $Archive                                       # a file inside C:\lw with no link anywhere on its way
if ([IO.Path]::GetExtension($Archive) -ne '.zip') { throw 'The archive must be a .zip file. Nothing was changed.' }
if (Test-Path -LiteralPath $LiveRoot) { throw "The folder $LiveRoot already exists. Nothing was changed. If an earlier run made it, roll it back with step 15; otherwise tell the lead." }
New-Item -ItemType Directory -Path $LiveRoot | Out-Null  # no -Force: an existing folder is an error
Protect-Path $LiveRoot $true
Assert-Private $LiveRoot $true

$Token = [guid]::NewGuid().ToString('N')                 # random
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
[IO.File]::WriteAllText((Join-Path $LiveRoot '.owner.json'), (ConvertTo-Json @{ token = $Token; path = $Here }), $Utf8)
New-Item -ItemType Directory -Path $StateDir, $Journal, (Split-Path $Store) | Out-Null

$Saved = [ordered]@{                                     # only these names are ever read back; every path is recomputed from LiveRoot and BusRoot
    LiveRoot = $LiveRoot; BusRoot = $BusRoot; Port = $PortNumber; Token = $Token; Py = $Py; Archive = $Archive; ExpectedSha = $ExpectedSha
    MachineAlias = $MachineAlias; ClaudeAlias = $ClaudeAlias; CodexAlias = $CodexAlias; ProjectAlias = $ProjectAlias
}
[IO.File]::WriteAllText($Names, ($Saved | ConvertTo-Json -Depth 3), $Utf8)
Test-Path -LiteralPath $Names
```

It checks the three values (Python must be one plain file; the archive must be a `.zip` file inside `C:\lw` with no link on its way), refuses a folder that already exists (nothing is changed), creates
`C:\lw\live`, makes it **private** and reads the access list back by security identifier before anything is written, writes an ownership marker (which the rollback checks before it deletes anything)
and saves a short list of names. It must print `True`.
If it says the folder already exists, an earlier run made it: roll that back with step 15, or tell the lead. **Do not choose another folder.**

## Step 2: check the archive, then unpack it

Script: `switchon-02-unpack.ps1`. It compares the archive's SHA-256 with the checksum and unpacks only if they are equal.

<!-- script: switchon-02-unpack.ps1 -->
```powershell
'== Step 2: check the archive, then unpack it'
Assert-PlainFile $Archive                                # again: a file inside C:\lw, no link anywhere on its way
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash
if ($Actual -ne $ExpectedSha) { throw 'The archive does not match its checksum. Nothing was unpacked. Stop here and tell the lead.' }
New-Item -ItemType Directory -Force $Src | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $Src
Get-ChildItem $Src | Select-Object -ExpandProperty Name
```

It must list the source folders (`docs`, `quota_balancer`, `tests` and a few files). A checksum mismatch stops it with nothing unpacked: tell the lead.

## Step 3: check Python and the usage files

Script: `switchon-03-check.ps1`. It only reads. It prints Python's version and **how many** usage files agenttalk has (a number; nothing in them is shown).

<!-- script: switchon-03-check.ps1 -->
```powershell
'== Step 3: check Python and the usage files'
& $Py -c 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python is older than 3.11.' }
$StateFolder = Join-Path $BusRoot 'state'
if (-not (Test-Path -LiteralPath $StateFolder -PathType Container)) { throw 'The agenttalk state folder was not found. Nothing was changed. Tell the lead.' }
$Count = @(Get-ChildItem -LiteralPath $StateFolder -Filter '*.capacity.json' -File).Count
'{0} usage file(s) found' -f $Count
if ($Count -eq 0) { throw 'No usage file was found yet. Run a turn in a seat first, or tell the lead. Nothing was changed.' }
```

It must print a version of 3.11 or newer and at least one usage file. If there is none yet, run a turn in a seat first.

## Step 4: set up the store

Script: `switchon-04-store.ps1`. It creates Leasewise's empty store (private).

<!-- script: switchon-04-store.ps1 -->
```powershell
'== Step 4: set up the store'
& $Py -m quota_balancer --db $Store init
if ($LASTEXITCODE -ne 0) { throw 'init failed' }
Assert-Private (Split-Path $Store) $false
Test-Path -LiteralPath $Store
```

It must print `True`.

## Step 5 (operator step): the two accounts and this machine as an allowed reporter

Leasewise accepts a machine's readings for an account only if that machine has a seat in a project that has the account. This step tells Leasewise about the two accounts (`claude-main` and `codex-main`,
both subscriptions with no money budget), the machine (`laptop-b`) and one placeholder seat called `readings` that exists **only** to make the machine an allowed reporter: it is in "shadow" mode and **is never used to request a lease**, so it grants
and refuses nothing. Read the block, then run the script `switchon-05-accounts.ps1`.

<!-- script: switchon-05-accounts.ps1 -->
```powershell
'== Step 5: the two accounts and the allowed reporter'
$Inventory = @"
{"version":2,
 "accounts":[{"alias":"$ClaudeAlias","vendor":"anthropic","kind":"subscription","budget_micro_eur":null},
             {"alias":"$CodexAlias","vendor":"openai","kind":"subscription","budget_micro_eur":null}],
 "machines":[{"alias":"$MachineAlias"}],
 "gateways":[],
 "projects":[{"alias":"$ProjectAlias"}],
 "teams":[{"project":"$ProjectAlias","alias":"team"}],
 "project_accounts":[{"project":"$ProjectAlias","account":"$ClaudeAlias"},{"project":"$ProjectAlias","account":"$CodexAlias"}],
 "seats":[{"team":{"project":"$ProjectAlias","team":"team"},"machine":"$MachineAlias","alias":"readings","role":"main","enforcement":"shadow"}],
 "models":[], "profiles":[], "limits":[]}
"@
$InventoryFile = Join-Path $LiveRoot 'inventory.json'    # inside the private folder: new files there inherit its private list
Set-Content -LiteralPath $InventoryFile -Value $Inventory -Encoding ascii
& $Py -m quota_balancer --db $Store config-import --file $InventoryFile
if ($LASTEXITCODE -ne 0) { throw 'config-import failed. Nothing else was changed. Tell the lead.' }
& $Py -m quota_balancer --db $Store policy-ids --json | Out-File -Encoding ascii -LiteralPath $PolicyIds
if ($LASTEXITCODE -ne 0) { throw 'policy-ids failed' }
(Get-Content -LiteralPath $PolicyIds -Raw | ConvertFrom-Json).accounts | ForEach-Object { $_.alias }
```

It must print the two account names. It replaces Leasewise's inventory, which is empty at this point; it touches nothing else.

### Check after step 5 (step 6)

Script: `switchon-06-check-accounts.ps1`. It opens the store read-only and asks the question Leasewise itself asks when a reading arrives.

<!-- script: switchon-06-check-accounts.ps1 -->
```powershell
'== Step 6: this machine is an allowed reporter for both accounts'
$Question = @'
import sqlite3, sys
store, machine = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("file:" + store + "?mode=ro", uri=True)
bad = 0
for alias in sys.argv[3:]:
    row = conn.execute("SELECT 1 FROM seats s JOIN machines m ON m.id = s.machine_id JOIN teams t ON t.id = s.team_id "
                       "JOIN project_accounts pa ON pa.project_id = t.project_id JOIN accounts a ON a.id = pa.account_id "
                       "WHERE m.alias = ? AND a.alias = ? LIMIT 1", (machine, alias)).fetchone()
    print(alias + ": " + ("allowed reporter" if row else "NOT an allowed reporter"))
    bad += row is None
sys.exit(1 if bad else 0)
'@
& $Py -c $Question $Store $MachineAlias $ClaudeAlias $CodexAlias
if ($LASTEXITCODE -ne 0) { throw 'This machine is not an allowed reporter for both accounts. Do not go on. Tell the lead.' }
'both accounts: allowed'
```

It must print `claude-main: allowed reporter`, `codex-main: allowed reporter` and `both accounts: allowed`. If not, **do not go on**; tell the lead.

## Step 7: the private settings file and the identity value

Script: `switchon-07-settings.ps1`. Before anything secret exists it checks that the live folder, the store's folder and the store are private (and stops, with "nothing secret has been written", if not). It then makes
the settings file private, makes a random identity value, enrolls it for this machine in Leasewise (through standard input, so the value is never on a command line) and writes it into the settings, checking privacy
again after each write (a failure there says the secret **has already been written**). It never prints the value. The settings have no account table yet.

<!-- script: switchon-07-settings.ps1 -->
```powershell
'== Step 7: the private settings file and the identity value'
Assert-Private $LiveRoot $true                                # before anything is written: the live folder, ...
Assert-Private (Split-Path $Store) $false                     # ... the folder of the store, ...
Assert-Private $Store $false                                  # ... and the store itself must already be private
New-Item -ItemType File -Path $ConfigPath | Out-Null          # empty: nothing secret yet
Protect-Path $ConfigPath $false                               # its own list: you, SYSTEM, Administrators; nothing inherited
Assert-Private $ConfigPath $true                              # read back by identifier; stops here if anything is wrong

$Bytes = New-Object byte[] 24
$Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$Rng.GetBytes($Bytes)
$Id = ($Bytes | ForEach-Object { $_.ToString('x2') }) -join ''
$Id | & $Py -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-stdin --enabled true
if ($LASTEXITCODE -ne 0) { throw 'identity-set failed' }
Assert-PrivateAfter $Store $false                             # the store now holds the value: it must still be private

$ToToml = { param($Value) '"' + ([string]$Value).Replace('\', '\\').Replace('"', '\"') + '"' }       # a TOML basic string: only \ and " need escaping
$ConfigText = @"
version = 1
base_url = $(& $ToToml $BaseUrl)
identity = $(& $ToToml $Id)
machine = $(& $ToToml $MachineAlias)
journal_root = $(& $ToToml $Journal)
state_path = $(& $ToToml (Join-Path $StateDir 'reporter.db'))
policy_ids = $(& $ToToml $PolicyIds)
poll_seconds = 30
"@
[IO.File]::WriteAllText($ConfigPath, $ConfigText, $Utf8)      # UTF-8 without a byte-order mark; the file keeps its private list
Assert-PrivateAfter $ConfigPath $true                         # and it is still private after the write
Remove-Variable Id, Bytes, ConfigText
'private: {0} access entries, all of them you, SYSTEM or Administrators' -f (Get-Acl -LiteralPath $ConfigPath).Access.Count
```

It must print `private: 3 access entries, all of them you, SYSTEM or Administrators` (the number may differ, but the sentence must appear).

## Step 8 (operator step): the account table

This is the step that needs your hand. The reporter finds an account by **the account text agenttalk writes**, and you tell it which Leasewise account each text means.

**Get the two texts.** On your own screen, run `agenttalk capacity` (the same way you run agenttalk anywhere else). It prints one line per account, written `<provider>:<your Windows user name>:home-<eight letters or digits>`.
Find the **Claude** line (it starts with `claude:`) and the **Codex** line (it starts with `codex:`). The texts contain your Windows user name, so: **copy them only from your own screen, only into this script's two
questions, and never into the repository, a message to the lead, a report or a chat.**

Run the script `switchon-08-table.ps1`. It first checks that the settings file is private (a refusal there changes nothing), then asks for the two texts. **Nothing you type or paste is shown** (the question is masked):
paste each, press Enter. It checks their **shape** and says which one is wrong without showing it, and writes the table into the private settings file only if both are right.

<!-- script: switchon-08-table.ps1 -->
```powershell
'== Step 8: the account table'
Assert-Private $LiveRoot $true                           # before anything is asked or written
Assert-Private $ConfigPath $true
if ((Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8) -match '\[\[readings\.sources\]\]') { throw 'The account table is already in the settings file. Nothing was changed. Tell the lead.' }
if ($env:LW_SWITCHON_INPUT -ceq 'env') {                 # the lead's test run only (the guard allows it in rehearsal mode and nowhere else)
    $ClaudeText = $env:LW_SWITCHON_CLAUDE_TEXT
    $CodexText = $env:LW_SWITCHON_CODEX_TEXT
}
else {
    $ClaudeText = Read-Host -MaskInput 'Claude account text, as agenttalk capacity prints it (it is not shown as you type)'
    $CodexText = Read-Host -MaskInput 'Codex account text, as agenttalk capacity prints it (it is not shown as you type)'
}
$Shape = '^{0}:.+:home-[0-9a-f]{{8}}$'
if ($ClaudeText -cnotmatch ($Shape -f 'claude')) { throw 'The Claude text is not in the form claude:<user>:home-<8 characters 0-9 a-f>. Nothing was changed. Copy it again.' }
if ($CodexText -cnotmatch ($Shape -f 'codex')) { throw 'The Codex text is not in the form codex:<user>:home-<8 characters 0-9 a-f>. Nothing was changed. Copy it again.' }
if ($ClaudeText -eq $CodexText) { throw 'The two texts are the same. Nothing was changed. Copy them again.' }
$ToToml = { param($Value) '"' + ([string]$Value).Replace('\', '\\').Replace('"', '\"') + '"' }       # a TOML basic string: only \ and " need escaping
$Rows = @"

[[readings.sources]]
bus_root = $(& $ToToml $BusRoot)
provider = "claude"
agenttalk_account = $(& $ToToml $ClaudeText)
account = $(& $ToToml $ClaudeAlias)

[[readings.sources]]
bus_root = $(& $ToToml $BusRoot)
provider = "codex"
agenttalk_account = $(& $ToToml $CodexText)
account = $(& $ToToml $CodexAlias)
"@
[IO.File]::AppendAllText($ConfigPath, $Rows, $Utf8)           # the file keeps its private list
Assert-PrivateAfter $ConfigPath $true
Remove-Variable ClaudeText, CodexText, Rows
'two rows written'
```

It must print `two rows written`. If it says a text is not in the right form, copy that line again; nothing was changed.

### Check after step 8 (step 9)

Script: `switchon-09-check-table.ps1`. The reporter's `status` reads the settings strictly (a wrong table is refused with a fixed sentence that shows no value) and lists each row by **provider and Leasewise name only**.

<!-- script: switchon-09-check-table.ps1 -->
```powershell
'== Step 9: check the account table'
$Json = & $Py -m quota_balancer reporter status --config $ConfigPath --json
if ($LASTEXITCODE -ne 0) { throw 'The settings are refused (see the sentence above). Nothing secret was shown. Tell the lead.' }
$Readings = ($Json | ConvertFrom-Json).readings
if (-not $Readings.configured) { throw 'The settings hold no account table.' }
$Readings.sources | ForEach-Object { '{0} -> {1}' -f $_.provider, $_.account }
$Want = @(('claude -> ' + $ClaudeAlias), ('codex -> ' + $CodexAlias))
$Have = @($Readings.sources | ForEach-Object { '{0} -> {1}' -f $_.provider, $_.account })
foreach ($Line in $Want) { if ($Have -notcontains $Line) { throw "The table does not list: $Line" } }
if ($Have.Count -ne 2) { throw 'The table must list exactly the two accounts.' }
'table: ok'
```

It must print `claude -> claude-main`, `codex -> codex-main` and `table: ok`.

## Step 10 (window 1): the Leasewise server

Open a **new PowerShell 7 window** and run `switchon-10-server.ps1`. It prints nothing while it works. Leave the window open; you stop it with Ctrl-C in that window (step 14 then shows what is still running).

<!-- script: switchon-10-server.ps1 -->
```powershell
$ServeArgs = @('-m', 'quota_balancer', '--db', $Store, 'serve', '--host', '127.0.0.1', '--port', [string]$PortNumber)
& $Py @ServeArgs
```

## Step 11 (window 2): the reporter

A few seconds later, open another **new PowerShell 7 window** and run `switchon-11-reporter.ps1`. It prints one line per cycle, like
`{"blocked": null, "journal": "ok", "new_events": 0, "sent": 2}`. Leave it open.

<!-- script: switchon-11-reporter.ps1 -->
```powershell
$RunArgs = @('-m', 'quota_balancer', 'reporter', 'run', '--config', $ConfigPath)
& $Py @RunArgs
```

`"blocked": "unreachable"` means the server (window 1) is not running. A reporter that stops at once with `CONFIG_...` is telling you a setting is wrong (see "If something goes wrong").

**After a restart of the machine**, nothing needs setting up again: run step 10, then step 11, in two new windows. The reporter keeps its own state and sends nothing twice.

## Step 12: record the baseline

Run this once the reporter (step 11) has been running for a while, and **before** you ask the lead for the real turns. Script: `switchon-12-baseline.ps1`. It writes `baseline.json` with three things: a
schema number, **this machine's clock** at that moment (UTC, in milliseconds), and for each account the newest figure Leasewise has accepted so far (or none). The done check counts only figures **observed after both**.
Waiting does not prove that the reporter's delivery queue is empty: a figure that was observed before this step can still arrive after it, and because the check also compares the figure's own time with the
clock time recorded here, such a late arrival cannot make it pass.

<!-- script: switchon-12-baseline.ps1 -->
```powershell
'== Step 12: record the baseline'
$BaselineFile = Join-Path $LiveRoot 'baseline.json'
if ((Test-Path -LiteralPath $BaselineFile) -and -not $Again) { throw 'A baseline was already recorded. Nothing was changed. Run step 12 with -Again only if step 13 told you the clock moved back.' }
$Cut = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$Question = @'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
for alias in sys.argv[2:]:
    newest = conn.execute("SELECT MAX(w.observed_at) FROM windows w JOIN accounts a ON a.id = w.account_id WHERE a.alias = ?", (alias,)).fetchone()[0]
    print(alias + " " + str(int(newest or 0)))
'@
$Lines = @(& $Py -c $Question $Store $ClaudeAlias $CodexAlias)
if ($LASTEXITCODE -ne 0 -or $Lines.Count -ne 2) { throw 'The store could not be read. Nothing was changed. Tell the lead.' }
$Delivered = [ordered]@{}
foreach ($Line in $Lines) {
    if ($Line -cnotmatch '^([a-z0-9][a-z0-9-]*) ([0-9]+)$') { throw 'The store gave an answer this step does not understand. Nothing was changed. Tell the lead.' }
    $Delivered[$Matches[1]] = if ([int64]$Matches[2] -eq 0) { $null } else { [int64]$Matches[2] }
}
if ((@($Delivered.Keys) -join ',') -cne ($ClaudeAlias + ',' + $CodexAlias)) { throw 'The store did not answer for exactly the two accounts. Nothing was changed. Tell the lead.' }
$Record = [ordered]@{ schema = 1; cut_ms = $Cut; delivered = $Delivered }
[IO.File]::WriteAllText($BaselineFile, ($Record | ConvertTo-Json -Depth 4), $Utf8)
foreach ($Alias in $Delivered.Keys) {
    if ($null -eq $Delivered[$Alias]) { '{0}: no accepted figure yet' -f $Alias }
    else { '{0}: newest accepted figure so far was observed at {1}' -f $Alias, [DateTimeOffset]::FromUnixTimeMilliseconds($Delivered[$Alias]).UtcDateTime.ToString('yyyy-MM-dd HH:mm:ss') }
}
'baseline recorded at {0} UTC. Now ask the lead for one real turn of a Claude seat and one of the Codex seat, then run step 13.' -f [DateTimeOffset]::FromUnixTimeMilliseconds($Cut).UtcDateTime.ToString('yyyy-MM-dd HH:mm:ss')
```

It must print one line per account and `baseline recorded at ...`. It refuses to record a second baseline. The one case where you run it again is the clock case: if step 13 says "The clock moved back
since step 12", run `switchon-12-baseline.ps1 -Again` (the `-Again` switch replaces the baseline), then ask the lead for the two turns again. Use `-Again` for nothing else.

## Wait for a real turn, then step 13: the done check

Ask the lead for one small task for a Claude seat and one for the Codex seat (a real turn is what makes agenttalk write a fresh figure). Then, in any window, run `switchon-13-done.ps1`. For up to ten minutes (every 20 seconds)
it asks Leasewise's store and the reporter's status, and prints aliases and numbers only.

<!-- script: switchon-13-done.ps1 -->
```powershell
'== Step 13: the done check'
$BaselineFile = Join-Path $LiveRoot 'baseline.json'
if (-not (Test-Path -LiteralPath $BaselineFile -PathType Leaf)) { throw 'No baseline was recorded. Run step 12 first. Nothing was changed.' }
function Test-Whole { param($Value) ($Value -is [int]) -or ($Value -is [long]) }
function Test-Object { param($Item) ($null -ne $Item) -and ($Item.GetType().FullName -ceq 'System.Management.Automation.PSCustomObject') }       # PowerShell calls a list a custom object too, so the exact type is looked at
function Test-HasAll { param($Item, [string[]]$Names) (Test-Object $Item) -and (@($Names | Where-Object { $Item.PSObject.Properties.Name -cnotcontains $_ }).Count -eq 0) }
function Test-HasOnly { param($Item, [string[]]$Names) (Test-HasAll $Item $Names) -and (@($Item.PSObject.Properties).Count -eq $Names.Count) }
$NotBaseline = 'The baseline is not the file step 12 made. Nothing was changed. Tell the lead.'
try { $Base = Get-Content -LiteralPath $BaselineFile -Raw -Encoding UTF8 | ConvertFrom-Json -NoEnumerate -ErrorAction Stop } catch { throw $NotBaseline }
if (-not (Test-HasOnly $Base 'schema', 'cut_ms', 'delivered') -or -not (Test-Whole $Base.schema) -or $Base.schema -ne 1 -or -not (Test-Whole $Base.cut_ms) -or $Base.cut_ms -le 0 -or -not (Test-HasOnly $Base.delivered $ClaudeAlias, $CodexAlias)) { throw $NotBaseline }
$Delivered = @{}
foreach ($Alias in $ClaudeAlias, $CodexAlias) {
    $Value = $Base.delivered.$Alias
    if ($null -ne $Value -and (-not (Test-Whole $Value) -or $Value -lt 0)) { throw $NotBaseline }
    $Delivered[$Alias] = if ($null -eq $Value) { [int64]0 } else { [int64]$Value }
}
$Cut = [int64]$Base.cut_ms
if ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $Cut - 30000) { throw 'The clock moved back since step 12. Nothing was changed. Run step 12 again with -Again, then ask the lead for the two turns again.' }
$Question = @'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
for alias in sys.argv[2:]:
    newest = conn.execute("SELECT MAX(w.observed_at) FROM windows w JOIN accounts a ON a.id = w.account_id WHERE a.alias = ?", (alias,)).fetchone()[0]
    print(alias + " " + str(int(newest or 0)))
'@
$WaitMinutes = 10                                        # how long to keep trying (the check passes the moment everything holds)
$Deadline = (Get-Date).AddMinutes($WaitMinutes)
do {
    $Problems = @()
    $Newest = @{}
    $Lines = @(& $Py -c $Question $Store $ClaudeAlias $CodexAlias)
    if ($LASTEXITCODE -ne 0) { $Problems += 'The store could not be read.' }
    else {
        foreach ($Line in $Lines) { if ($Line -cmatch '^([a-z0-9][a-z0-9-]*) ([0-9]+)$') { $Newest[$Matches[1]] = [int64]$Matches[2] } }
        if ($Lines.Count -ne 2 -or $Newest.Count -ne 2) { $Problems += 'The store gave an answer this step does not understand.' }
    }
    $Status = $null
    $Json = & $Py -m quota_balancer reporter status --config $ConfigPath --json
    if ($LASTEXITCODE -ne 0) { $Problems += 'The reporter status could not be read (the reporter may never have run).' }
    else {
        try { $Parsed = ($Json | Out-String) | ConvertFrom-Json -NoEnumerate -ErrorAction Stop } catch { $Parsed = $null }
        $R = if (Test-HasAll $Parsed 'readings') { $Parsed.readings } else { $null }
        if (-not (Test-Object $Parsed)) { $Problems += 'The reporter status is not a JSON object.' }
        elseif (-not (Test-HasAll $Parsed 'operator_actions') -or $Parsed.operator_actions -isnot [array] -or -not (Test-HasAll $R 'configured', 'sources', 'last_scan', 'rows', 'capture_fault') `
                -or -not (Test-HasAll $R.rows 'sent', 'pending', 'failed_permanent') -or -not (Test-HasAll $R.capture_fault 'count') `
                -or -not (Test-Whole $R.rows.sent) -or -not (Test-Whole $R.rows.pending) -or -not (Test-Whole $R.rows.failed_permanent) -or -not (Test-Whole $R.capture_fault.count)) {
            $Problems += 'The reporter status is missing a part this check needs (readings, operator_actions, rows or capture_fault).'
        }
        else { $Status = $Parsed }
    }
    if ($Status) {
        $Have = @(@($R.sources) | ForEach-Object { if (Test-Object $_) { '{0} -> {1}' -f $_.provider, $_.account } else { '?' } } | Sort-Object)
        $Want = @(('claude -> ' + $ClaudeAlias), ('codex -> ' + $CodexAlias) | Sort-Object)
        if ($R.configured -ne $true -or ($Have -join ';') -cne ($Want -join ';')) { $Problems += 'The settings do not list exactly the two expected accounts.' }
        $When = $null
        if ($R.last_scan -is [datetime]) { $When = [DateTimeOffset]$R.last_scan }
        elseif ($R.last_scan -is [string]) { $Read = [DateTimeOffset]::MinValue; if ([DateTimeOffset]::TryParse($R.last_scan, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$Read)) { $When = $Read } }
        if ($null -eq $R.last_scan) { $Problems += 'The reporter has not scanned yet.' }
        elseif ($null -eq $When) { $Problems += 'The reporter''s last scan time could not be read.' }
        else {
            $Age = ([DateTimeOffset]::UtcNow - $When).TotalSeconds
            if ($Age -gt 180) { $Problems += 'The reporter has not scanned in the last three minutes (is window 2 running?).' }
            elseif ($Age -lt -30) { $Problems += 'The reporter''s last scan lies more than 30 seconds ahead of this machine''s clock.' }
        }
        if (@($Status.operator_actions).Count -ne 0 -or $R.rows.failed_permanent -ne 0 -or $R.capture_fault.count -ne 0) { $Problems += 'The reporter shows a warning.' }
    }
    foreach ($Alias in $ClaudeAlias, $CodexAlias) {
        if (-not $Newest.ContainsKey($Alias) -or $Newest[$Alias] -le $Cut -or $Newest[$Alias] -le $Delivered[$Alias]) { $Problems += ('{0}: no accepted figure observed after the baseline yet.' -f $Alias) }
    }
    if ($Problems.Count -gt 0 -and (Get-Date) -lt $Deadline) { 'waiting: ' + ($Problems -join ' '); Start-Sleep -Seconds 20 }
} until ($Problems.Count -eq 0 -or (Get-Date) -ge $Deadline)
foreach ($Alias in $ClaudeAlias, $CodexAlias) {
    '{0}: newest accepted figure {1}; at the baseline {2}; baseline time {3}' -f $Alias, $Newest[$Alias], $Delivered[$Alias], $Cut
}
if ($Status) { 'reporter: rows sent={0} waiting={1} failed={2}; capture faults={3}' -f $R.rows.sent, $R.rows.pending, $R.rows.failed_permanent, $R.capture_fault.count }
if ($Status) { foreach ($Line in $Status.operator_actions) { '!! ' + $Line } }
if ($Problems.Count -gt 0) { throw ('Not done: ' + ($Problems -join ' ') + ' Nothing was changed. See "If something goes wrong".') }
'PASSED'
```

It must end with `PASSED`. The check fails closed: it passes only if **all** of this holds, and otherwise it says which part failed and that nothing was changed. It needs: the baseline file is the one step 12 wrote; **each account has an accepted figure observed after the baseline's clock time and after that account's newest figure at that time**; the reporter's status
command succeeded and answered with a proper JSON object whose readings part is configured, lists exactly the two accounts and has a last-scan time that can be read; that last scan lies between three minutes
ago and 30 seconds ahead of this machine's clock (the 30 seconds are the stated clock tolerance); every other command succeeded and its answer could be read; and the status shows no operator warning, no
failed reading and no capture fault. "Scanned within three minutes" means the reporter ran recently; it does **not** prove the reporter is still running now. If it stops, it names what is missing; read "If something goes wrong".
Then follow "Tell the lead".

## Step 14: see what is still running, and stop it

Stop the windows yourself: press Ctrl-C in window 2, then in window 1. Then run `switchon-14-stopped.ps1`. It **only lists** what is still running from the live folder; it stops nothing.

<!-- script: switchon-14-stopped.ps1 -->
```powershell
'== Step 14: what is still running'
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ('*' + [WildcardPattern]::Escape($LiveRoot) + '*') } | Select-Object ProcessId, Name
```

It prints nothing but its banner when everything has stopped (it may show its own PowerShell: ignore that). If a process is listed, go back to its window and press Ctrl-C there.

## Step 15: roll back (undo everything)

Stop both windows first (step 14). The script `switchon-15-rollback.ps1` deletes `C:\lw\live`, and nothing else, after these checks: the folder holds the ownership marker this runbook wrote (and the marker is a plain file, not a link),
**no link anywhere on the way to it or inside it** (it looks at every entry and never follows a link), it is the folder `names.json` names, and the agenttalk folder is neither the same folder nor inside it. If any check fails it
deletes nothing and tells you. It never touches the agenttalk folder.

<!-- script: switchon-15-rollback.ps1 -->
```powershell
'== Step 15: roll back'
Set-Location -LiteralPath (Split-Path -Parent $LiveRoot)           # leave the live folder (it is about to be deleted)
Assert-Plain $LiveRoot
Assert-NoLink $LiveRoot
Assert-NoLink (Join-Path $LiveRoot '.owner.json')                  # the marker is a plain file, not a link, before it is read
$Marker = Get-Content -LiteralPath (Join-Path $LiveRoot '.owner.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
if (($Marker.token -ne $Token) -or ($Marker.path -ne $Here)) { throw 'This is not the folder this runbook made. Nothing was deleted.' }
$Links = 0                                               # a link anywhere inside: look at every entry, never go into a link
$Pending = New-Object System.Collections.Generic.Stack[string]
$Pending.Push($Here)
while ($Pending.Count -gt 0) {
    foreach ($Entry in (New-Object IO.DirectoryInfo($Pending.Pop())).GetFileSystemInfos()) {
        if ($Entry.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Links++ }
        elseif ($Entry -is [IO.DirectoryInfo]) { $Pending.Push($Entry.FullName) }
    }
}
if ($Links -gt 0) { throw 'The live folder contains a link. Nothing was deleted. Tell the lead.' }
Assert-Separate                                          # the last look before the delete: the agenttalk folder is not the live folder and not inside it
Remove-Item -LiteralPath $LiveRoot -Recurse -Force
Test-Path -LiteralPath $LiveRoot
```

It must print `False` at the end (the folder is gone). What rollback does **not** undo: nothing outside `C:\lw\live` was ever changed. The reporter's state file was version 7; a reporter built before
card 7b cannot open it, which does not matter once the folder is deleted. If you keep the folder and want to go back to an older program, keep a copy of the state file first.

## If something goes wrong

Run `switchon-help-status.ps1`. It prints the reporter's readings status: closed words, counts and Leasewise names only (never an account text, a seat name, a file name or a path). **Never paste `reporter.toml` anywhere.**

<!-- script: switchon-help-status.ps1 -->
```powershell
'== The readings status'
& $Py -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty readings | ConvertTo-Json -Depth 5
& $Py -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty operator_actions
```

The reporter's status raises three lines of its own for readings. What each means here:

- **`FORBIDDEN: this machine is not an authorized reporter for the account`.** Leasewise refused a reading with 403. On this machine it means step 5 did not take effect (the placeholder seat or the project's
  accounts). Run step 6 again; if it passes and this line stays, tell the lead. Each refused reading fails for good, and later readings are still sent.
- **`N seat file(s) name an agenttalk account that is not in readings.sources`.** A usage file in our agenttalk folder belongs to an account text that is not in your table. Two causes: the table has a typo
  (it was checked for shape only), or a seat runs under another Claude or Codex folder, which has a different text. Run `agenttalk capacity` and compare with the table by eye. If the account is meant to
  be reported, ask the lead; if not, ignore the line.
- **`A seat file has not followed the documented capacity format for N scans in a row`.** A usage file breaks agenttalk's documented format, often because agenttalk was updated. Tell the lead which
  agenttalk version runs; nothing else is needed.

Other words in the status: `retrying` with `MALFORMED_RESPONSE` means Leasewise answered a success without a usable body and the reporter is sending the same figure again (harmless, repeated after the usual
wait). `refused: ...` as a failure code means Leasewise rejected a reading for good; send the status to the lead.

If the settings are refused at start, the reporter prints one fixed sentence and a code: `CONFIG_INVALID` (a setting is wrong: tell the lead the sentence), `CONFIG_UNRESOLVED` (an account name is not in
Leasewise: step 5 did not finish), `CONFIG_PERMISSIONS` (the settings file is open to someone else: stop; tell the lead).

## Tell the lead when you are done

Send the lead **only**: the output of step 13 (it ends with `PASSED`) and the output of `switchon-help-status.ps1`. Both show Leasewise names (`claude-main`, `codex-main`), numbers and closed words. They never
show an account text. **Never send** `reporter.toml`, a usage file, the output of `agenttalk capacity`, your user name or a path from your user profile.

## Technical detail (for the lead and the reviewer)

- The scripts only read agenttalk's files: the reporter opens `<bus>\state\*.capacity.json` read-only with the strict reader of card 7b (`quota_balancer/reporter/capacityreader.py`): at most 64 files, 16 KiB each, links skipped.
- Settings: one `[[readings.sources]]` row per account (`bus_root`, `provider`, `agenttalk_account`, `account`); no `limit_events`, gateway or money-reader table, so none of those lanes runs. `poll_seconds = 30`.
- Ports: Leasewise on `127.0.0.1:8443` (the identity header authenticates this machine; Leasewise refuses other addresses).
- Authorization: `http_api._authorized_reporter_for_account` accepts a machine that has a seat in a team whose project has the account (step 5's placeholder seat). Step 6 asks the same question of the store.
- The done check needs: a store query and a status command that both succeed, a reporter scan in the last 180 seconds, exactly the two expected sources, an accepted observation per account later than both the clock time and the delivered maximum recorded in `baseline.json` (schema 1, checked field by field), a scan time between 180 seconds ago and 30 seconds ahead, and no operator line, failed reading or capture fault.
- Flow after the done check: each window keeps its latest unsent figure, Leasewise gets at most one accepted figure per window every two minutes (a new reset time goes at once), and the gaps sentence of
  `reporter status` says what readings cannot do.
- The scripts' safety checks are copied from the money-reader trial (`docs/TRIAL-R1-READER-4.md`): the guard is new for this card (`C:\lw`, every ancestor, the whole live folder, the rehearsal mode) and the privacy helpers were rewritten in fix round 1 (`Assert-Private` before a write, `Assert-PrivateAfter` after one). `tests/test_switchon_scripts.py` keeps every block identical to its script and pins the shared start and the guard.
