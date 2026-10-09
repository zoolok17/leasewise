# Switching on usage readings on Laptop B: the operator's runbook

Mode: how-to. For: the operator, at Laptop B (the machine the Leasewise team's seats run on). Status: **rehearsed end to end on made-up data** in the
lead's scratch folder (`docs/STEP-CARD-7B-ON.md`); **not yet run for real**. Leasewise commit to install: `<COMMIT>` (in the copy the lead hands over, this is a
40-character commit id; if you see angle brackets there, stop and ask the lead). Approved by claude-agenttalk-lead on 2026-10-09 (answer to q-9ddb21b0c31c): first on this machine, for our own team's two accounts only.

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

**How long.** About 45 minutes of your time from the first script to step 12 (most of it is typing the parts of two account texts and reading what each script prints), then a wait
for one real turn of a Claude seat and of the Codex seat (the lead can give each a small task), then step 13.

## Before you start

You need all of these. If one is missing, stop.

1. **PowerShell 7** (`pwsh`). The scripts were rehearsed with it. Windows PowerShell 5.1 was **not** rehearsed with them and refused scripts on our machines earlier. The machine's
   script policy decides what may run, and it must **never** be changed. If the check in step 0 says scripts cannot run, stop and tell the lead.
2. **Python 3.11 or newer**, with its full path (for example the team's `C:\lw\venv\Scripts\python.exe`). Leasewise needs no other package.
3. **agenttalk 0.99.0 or newer** on this machine, with **every seat restarted on it and one turn done since**. Older agenttalk writes usage files without the account text this feature needs. Step 3 checks that
   usable files exist for both providers and stops if not. If a later agenttalk writes another format, step 3 stops too: then tell the lead; do not try to get around it.
4. **Three things from the lead**: the folder `docs\switchon-7b` (the scripts; copy it anywhere you like, for example to `C:\lw\switchon-7b`, and keep its files together),
   the archive `leasewise-<COMMIT>.zip` (a source archive of the exact commit; in your copy `<COMMIT>` is the commit id, and if you still see angle brackets, stop and ask the lead; **put the archive inside `C:\lw`**: step 1 refuses an archive anywhere else or behind a link) and its SHA-256 checksum.
5. **Nothing else uses port 8443** on this machine (Leasewise listens only on `127.0.0.1`, which is this machine; it refuses any other address). Step 10 checks this and refuses to start if anything listens
   there; step 11 checks that the only listener is window 1's server before it sends anything.
6. **The account parts**: you will type four short parts from your own screen in step 8 (a name and 8 characters, for each of the two accounts). See step 8 for how to get them; do not collect them earlier than that.

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
- **Python runs without outside influence.** Every Python call in every script uses the same three switches (`-E -s -B`): `PYTHON...` environment variables such as `PYTHONPATH` are ignored, your own installed
  Python packages are not used, and no bytecode files are written.
- **"Private" means private to your Windows account.** Only your Windows account, SYSTEM and Administrators can open `C:\lw\live`. But **any program running under your Windows account can read it**, including
  another team's seats on this machine if they run under the same account. When the lead checked on 9 October 2026, only this team's seats were running here; if another team's seats ever run here under your account, this limit applies to them. What that exposes: the identity value (it only lets a program send readings to this
  machine's own Leasewise) and the account texts. The scripts cannot change this.
- **Limits that remain, stated plainly.** (1) The live folder is private, so a link could appear between the check and its use only if you, SYSTEM or an Administrator made it. (2) The port checks of steps 10 and 11
  happen once, when you start: a program that took the port over *after* step 11's check would have to run under your own Windows account (Windows refuses a second program from another account on a port that is
  in use; Microsoft's page "Using SO_REUSEADDR and SO_EXCLUSIVEADDRUSE" describes this), and a program under your own account can read the settings file anyway. The Leasewise server itself is unchanged. (3) Hard links
  are not detected; the scripts only write files they create.
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

Script: `switchon-01-setup.ps1`. **You do not edit it.** It asks three plain questions and shows what you type, because none of it is secret: the full path of Python (for example `C:\Python313\python.exe`), the full
path of the archive (inside `C:\lw`), and the checksum the lead gave you. Quote marks around a path are removed. The other names (`laptop-b`, `claude-main`, `codex-main`, `leasewise-team`) are Leasewise's own names for this
machine, the two accounts and the project; they stay fixed in the script.

<!-- script: switchon-01-setup.ps1 -->
```powershell
'== Step 1: names and the private folder'

if (Test-Path -LiteralPath 'Env:LW_SWITCHON_PYTHON') {   # the lead's test run only (the guard allows it in rehearsal mode and nowhere else)
    $Py = $env:LW_SWITCHON_PYTHON
    $Archive = $env:LW_SWITCHON_ARCHIVE
    $ExpectedSha = $env:LW_SWITCHON_SHA
}
else {
    $Py = (Read-Host 'The full path of python.exe (Python 3.11 or newer), for example C:\Python313\python.exe').Trim().Trim('"')
    $Archive = (Read-Host 'The full path of the archive the lead gave you (it must lie inside C:\lw), for example C:\lw\leasewise-1234abcd.zip').Trim().Trim('"')
    $ExpectedSha = (Read-Host 'The SHA-256 checksum the lead gave you (64 characters)').Trim()
}
$MachineAlias  = 'laptop-b'                                     # this machine, as Leasewise names it
$ClaudeAlias   = 'claude-main'                                  # our Claude account, as Leasewise names it
$CodexAlias    = 'codex-main'                                   # our Codex account, as Leasewise names it
$ProjectAlias  = 'leasewise-team'                               # the project both accounts belong to in Leasewise

$Src        = Join-Path $LiveRoot 'src'                         # Leasewise's source, extracted
$Store      = Join-Path $LiveRoot 'store\inventory.db'          # Leasewise's store
$StateDir   = Join-Path $LiveRoot 'state'                       # the reporter's own state
$Journal    = Join-Path $LiveRoot 'journal'                     # an empty folder the reporter insists on
$Names      = Join-Path $LiveRoot 'names.json'

if (($ExpectedSha -cnotmatch '^[0-9A-Fa-f]{64}$') -or (($MachineAlias, $ClaudeAlias, $CodexAlias, $ProjectAlias | Where-Object { $_ -cnotmatch '^[a-z0-9][a-z0-9-]{0,62}$' }).Count -gt 0)) { throw 'The checksum is not 64 characters of 0-9 and a-f. Nothing was changed. Run step 1 again.' }
Assert-PythonFile $Py
Assert-PlainFile $Archive                                       # a file inside C:\lw with no link anywhere on its way
if ([IO.Path]::GetExtension($Archive) -ne '.zip') { throw 'The archive must be a .zip file. Nothing was changed.' }
if (Test-Path -LiteralPath $LiveRoot) { throw "The folder $LiveRoot already exists. Nothing was changed. If an earlier run made it, roll it back with step 15; otherwise tell the lead." }
New-Item -ItemType Directory -Path $LiveRoot | Out-Null  # no -Force: an existing folder is an error

# The marker and names.json come first, at once: neither holds a secret, and step 15 can remove the folder from them whatever happens next.
$Token = [guid]::NewGuid().ToString('N')                 # random
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
[IO.File]::WriteAllText((Join-Path $LiveRoot '.owner.json'), (ConvertTo-Json @{ token = $Token; path = $Here }), $Utf8)
$Saved = [ordered]@{                                     # only these names are ever read back; every path is recomputed from LiveRoot and BusRoot
    LiveRoot = $LiveRoot; BusRoot = $BusRoot; Port = $PortNumber; Token = $Token; Py = $Py; Archive = $Archive; ExpectedSha = $ExpectedSha
    MachineAlias = $MachineAlias; ClaudeAlias = $ClaudeAlias; CodexAlias = $CodexAlias; ProjectAlias = $ProjectAlias
}
[IO.File]::WriteAllText($Names, ($Saved | ConvertTo-Json -Depth 3), $Utf8)
try { Protect-Path $LiveRoot $true; Assert-Private $LiveRoot $true }
catch { throw "$LiveRoot was made but could not be made private. Nothing secret was written. Run step 15 to remove it, then tell the lead." }
New-Item -ItemType Directory -Path $StateDir, $Journal, (Split-Path $Store) | Out-Null
Test-Path -LiteralPath $Names
```

It checks the three values (Python must be one plain file; the archive must be a `.zip` file inside `C:\lw` with no link on its way), refuses a folder that already exists (nothing is changed), creates
`C:\lw\live`, **at once** writes the ownership marker and the short list of names (neither holds a secret; step 15 needs them), and only then makes the folder **private** and reads the access list back by security
identifier. If that fails it says "`C:\lw\live` was made but could not be made private. Nothing secret was written. Run step 15 to remove it, then tell the lead." It must print `True`.
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

Script: `switchon-03-check.ps1`. It only reads. It prints Python's version, **how many** usage files agenttalk has, and for each provider **how many usable ones** (numbers only; nothing in them is shown). A usable file is one the reporter's own scan
accepts (format number exactly 2, scope "account" and every other documented field) and whose account text starts with `claude:` or `codex:`. If the folder cannot be read, or holds more entries or usage files than the reporter looks at (4096
entries, 64 files), step 3 and step 8 say so and stop.

<!-- script: switchon-03-check.ps1 -->
```powershell
'== Step 3: check Python and the usage files'
& $Py @PyFlags -c 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python is older than 3.11.' }
$StateFolder = Join-Path $BusRoot 'state'
if (-not (Test-Path -LiteralPath $StateFolder -PathType Container)) { throw 'The agenttalk state folder was not found. Nothing was changed. Tell the lead.' }
$Count = @(Get-ChildItem -LiteralPath $StateFolder -Filter '*.capacity.json' -File).Count
'{0} usage file(s) found' -f $Count
if ($Count -eq 0) { throw 'No usage file was found yet. Run a turn in a seat first, or tell the lead. Nothing was changed.' }
$Usable = Invoke-UsageScan 'count' $null                     # asked of the reporter's own scan (agenttalk 0.99.0 or newer); only counted, never shown
foreach ($Provider in 'claude', 'codex') {
    $Number = $Usable.$Provider
    '{0}: {1} usable usage file(s)' -f $Provider, $Number
    if ($Number -eq 0) { throw "No usable $Provider usage file was found. Every seat must run agenttalk 0.99.0 or newer, be restarted on it, and have done one turn since. If they do, a later agenttalk may write another format: tell the lead. Nothing was changed." }
}
```

It must print a version of 3.11 or newer, at least one usage file and at least one usable file for **each** of Claude and Codex. If a provider has none, the seats are on an older agenttalk or have not done a turn
since the restart: update and restart them, run a turn in a seat of each provider, and run step 3 again. If a later agenttalk writes another format, step 3 stops and you tell the lead.

## Step 4: set up the store

Script: `switchon-04-store.ps1`. It creates Leasewise's empty store (private).

<!-- script: switchon-04-store.ps1 -->
```powershell
'== Step 4: set up the store'
& $Py @PyFlags -m quota_balancer --db $Store init
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
$Question = @'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print(conn.execute("SELECT (SELECT COUNT(*) FROM accounts) + (SELECT COUNT(*) FROM machines)").fetchone()[0])
'@
$Have = & $Py @PyFlags -c $Question $Store                 # read-only: how many accounts and machines the store already has
if ($LASTEXITCODE -ne 0 -or "$Have" -cnotmatch '^[0-9]+$') { throw 'The store could not be read. Nothing was changed. Tell the lead.' }
if ([int]"$Have" -ne 0) { throw 'Step 5 was already done. Nothing was changed. Go on with step 6.' }
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
& $Py @PyFlags -m quota_balancer --db $Store config-import --file $InventoryFile
if ($LASTEXITCODE -ne 0) { throw 'config-import failed. Nothing else was changed. Tell the lead.' }
& $Py @PyFlags -m quota_balancer --db $Store policy-ids --json | Out-File -Encoding ascii -LiteralPath $PolicyIds
if ($LASTEXITCODE -ne 0) { throw 'policy-ids failed' }
(Get-Content -LiteralPath $PolicyIds -Raw | ConvertFrom-Json).accounts | ForEach-Object { $_.alias }
```

It must print the two account names. It replaces Leasewise's inventory, which is empty at this point; it touches nothing else. Before it writes anything it checks, read-only, that the store has no account or
machine yet; if it has, it says "Step 5 was already done. Nothing was changed. Go on with step 6." and you do.

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
& $Py @PyFlags -c $Question $Store $MachineAlias $ClaudeAlias $CodexAlias
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
$Id | & $Py @PyFlags -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-stdin --enabled true
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

**Get the four parts.** On your own screen, run `agenttalk capacity` (the same way you run agenttalk anywhere else). For each account it prints a line like

```
  claude account of OS user <name> (home-xxxxxxxx), 2 seats: ...
```

You need two parts of that line, for the Claude line and again for the Codex line: **the name after "OS user"** and **the 8 characters of `home-xxxxxxxx`** (with or without the `home-` in front). The script puts them together the
way agenttalk writes them in its usage files, `<provider>:<name>:home-<8 characters>`. The name is your Windows user name, so: **type the parts only from your own screen, only into this script's four
questions, and never into the repository, a message to the lead, a report or a chat.**

Run the script `switchon-08-table.ps1`. It first checks that the settings file is private (a refusal there changes nothing), then asks four questions. **Nothing you type or paste is shown** (the questions are masked):
type each part, press Enter. It checks the shape of each part and says which one is wrong without showing it. Because masked typing hides typos, it then asks **the reporter's own file scan** (the very code the reporter runs, so it looks at exactly the files the reporter reads and accepts) how many usage files carry exactly that
account text; the texts reach it on standard input, never on a command line, and only a count comes back, like `Claude: matches 1 usage file(s)`. If a text matches no file it says so and writes nothing. The table is written into the private settings
file only if both are right.

<!-- script: switchon-08-table.ps1 -->
```powershell
'== Step 8: the account table'
Assert-Private $LiveRoot $true                           # before anything is asked or written
Assert-Private $ConfigPath $true
if ((Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8) -match '\[\[readings\.sources\]\]') { throw 'The account table is already in the settings file. Nothing was changed. Tell the lead.' }
if ($env:LW_SWITCHON_INPUT -ceq 'env') {                 # the lead's test run only (the guard allows it in rehearsal mode and nowhere else)
    $ClaudeName, $ClaudeHome = $env:LW_SWITCHON_CLAUDE_NAME, $env:LW_SWITCHON_CLAUDE_HOME
    $CodexName, $CodexHome = $env:LW_SWITCHON_CODEX_NAME, $env:LW_SWITCHON_CODEX_HOME
}
else {
    $ClaudeName = Read-Host -MaskInput 'Claude: the name after "OS user" (it is not shown as you type)'
    $ClaudeHome = Read-Host -MaskInput 'Claude: the 8 characters of home-xxxxxxxx (it is not shown as you type)'
    $CodexName = Read-Host -MaskInput 'Codex: the name after "OS user" (it is not shown as you type)'
    $CodexHome = Read-Host -MaskInput 'Codex: the 8 characters of home-xxxxxxxx (it is not shown as you type)'
}
function Join-AccountText {
    param([string]$Provider, [string]$Label, [string]$Name, [string]$Home8)
    if ($Name.Length -lt 1 -or $Name.Length -gt 256 -or $Name -cmatch '[\p{Cc}:]' -or $Name -cne $Name.Trim()) {
        throw "The $Label name is not in the allowed form (1 to 256 characters, no colon, no control character, no space at either end). Nothing was changed. Run step 8 again."
    }
    if ($Home8 -cnotmatch '^(home-)?([0-9a-f]{8})$') { throw "The $Label home part must be exactly 8 characters 0-9 and a-f, with or without home- in front. Nothing was changed. Run step 8 again." }
    '{0}:{1}:home-{2}' -f $Provider, $Name, $Matches[2]
}
$ClaudeText = Join-AccountText 'claude' 'Claude' $ClaudeName $ClaudeHome
$CodexText = Join-AccountText 'codex' 'Codex' $CodexName $CodexHome
$Counts = Invoke-UsageScan 'match' @{ claude = $ClaudeText; codex = $CodexText }          # the reporter's own scan; the texts go in on standard input, only counts come back
foreach ($Pair in @(@('Claude', 'claude'), @('Codex', 'codex'))) {
    $Found = $Counts.($Pair[1])
    if ($Found -eq 0) { throw ('The {0} text you typed does not match any of our usage files. Nothing was changed. Compare with your agenttalk capacity screen and run step 8 again.' -f $Pair[0]) }
    '{0}: matches {1} usage file(s)' -f $Pair[0], $Found
}
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
Remove-Variable ClaudeName, ClaudeHome, CodexName, CodexHome, ClaudeText, CodexText, Rows
'two rows written'
```

It must print the two `matches` lines and `two rows written`. If it says a part is not in the right form, or a text does not match any of our usage files, nothing was changed: compare with your agenttalk capacity screen
and run step 8 again.

### Check after step 8 (step 9)

Script: `switchon-09-check-table.ps1`. The reporter's `status` reads the settings strictly (a wrong table is refused with a fixed sentence that shows no value) and lists each row by **provider and Leasewise name only**.

<!-- script: switchon-09-check-table.ps1 -->
```powershell
'== Step 9: check the account table'
$Json = & $Py @PyFlags -m quota_balancer reporter status --config $ConfigPath --json
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

Open a **new PowerShell 7 window** and run `switchon-10-server.ps1`. Before it starts anything it looks at who listens on port 8443. If anything does, it stops with the process number and program name and starts nothing
("Something on this machine already listens on port 8443 ..."): if that is window 1 from earlier, go back to that window; otherwise tell the lead. This also refuses a second step 10. If it cannot read the list, it stops too.
The server prints nothing while it works. Leave the window open; you stop it with Ctrl-C in that window (step 14 then shows what is still running).

<!-- script: switchon-10-server.ps1 -->
```powershell
$Holders = @(Get-PortListeners $PortNumber)
if ($Holders.Count -gt 0) {
    throw ('Something on this machine already listens on port {0} (process {1}, program {2}). Nothing was started. If it is window 1 from earlier, go back to that window; otherwise tell the lead.' -f $PortNumber, $Holders[0].ProcessId, $Holders[0].Name)
}
$ServeArgs = Get-ServeArgs                               # the exact words step 11 expects
& $Py @PyFlags @ServeArgs
```

## Step 11 (window 2): the reporter

A few seconds later, open another **new PowerShell 7 window** and run `switchon-11-reporter.ps1`. Before it starts the reporter it checks that exactly one program listens on port 8443, on `127.0.0.1`, and that it is window 1's Leasewise server **started with exactly the command step 10 launches**: your Python,
then `-E -s -B -m quota_balancer --db`, the store, `serve --host 127.0.0.1 --port 8443`, in that order, with no word before, between or after (the store is compared as a Windows path, ignoring capitals; every other word
exactly). A program that merely contains these words (for example a different script given them as arguments) is refused. The program must be your Python itself, or, when your Python is a virtual environment's launcher
(a `pyvenv.cfg` in the folder above it, inside `C:\lw`), the one base interpreter that file names, started by that launcher with the identical command line; nothing else is accepted, and a program whose command line or path
cannot be read is refused. Otherwise it says "Port 8443 is held by a program that is not window 1's Leasewise server. Nothing was started, and the identity value was not sent. Tell the lead." It also refuses to start when
a reporter for the same settings is already running. What this cannot tell apart: a program under your own Windows account that copies this exact command line, for example from another folder; such a program could read the
settings file anyway. The reporter prints one line per cycle, like
`{"blocked": null, "journal": "ok", "new_events": 0, "sent": 2}`. Leave it open.

<!-- script: switchon-11-reporter.ps1 -->
```powershell
Assert-OurServerListens
Assert-NoReporterRunning
$RunArgs = @('-m', 'quota_balancer', 'reporter', 'run', '--config', $ConfigPath)
& $Py @PyFlags @RunArgs
```

`"blocked": "unreachable"` means the server (window 1) is not running. A reporter that stops at once with `CONFIG_...` is telling you a setting is wrong (see "If something goes wrong").

**If window 1 or window 2 prints an error or closes by itself, stop.** Press Ctrl-C in the other window, start nothing again, and tell the lead which window it was and its last line (leave out any path that contains your
user name). Never run step 10 or step 11 again while their windows are open; the scripts refuse that too.

**After a restart of the machine**, nothing needs setting up again: run step 10, then step 11, in two new windows. The reporter keeps its own state and sends nothing twice.

## Step 12: record the baseline

Wait **at least two minutes** after you started window 2, and until it has printed a line with `"sent"` greater than 0 (each line is one cycle of about 30 seconds, so such a line may appear once between lines that show 0). Then run this, **before** you ask the lead for the real turns. If every line still shows `"sent": 0`
after five minutes, that is normal when no seat has had a turn in the last ten minutes (a figure counts for ten minutes only): go on anyway; the script then says "no accepted figure yet" for the account, and the real turns
are what step 13 waits for. Script: `switchon-12-baseline.ps1`. It writes `baseline.json` with three things: a
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
$Lines = @(& $Py @PyFlags -c $Question $Store $ClaudeAlias $CodexAlias)
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
function Get-Now { [DateTimeOffset]::UtcNow }                     # every reading of the clock goes through here
$ClockBack = 'The clock moved back since step 12. Nothing was changed. Run step 12 again with -Again, then ask the lead for the two turns again.'
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
if ((Get-Now).ToUnixTimeMilliseconds() -lt $Cut - 30000) { throw $ClockBack }
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
    $Lines = @(& $Py @PyFlags -c $Question $Store $ClaudeAlias $CodexAlias)
    if ($LASTEXITCODE -ne 0) { $Problems += 'The store could not be read.' }
    else {
        foreach ($Line in $Lines) { if ($Line -cmatch '^([a-z0-9][a-z0-9-]*) ([0-9]+)$') { $Newest[$Matches[1]] = [int64]$Matches[2] } }
        if ($Lines.Count -ne 2 -or $Newest.Count -ne 2) { $Problems += 'The store gave an answer this step does not understand.' }
    }
    $Status = $null
    $Json = & $Py @PyFlags -m quota_balancer reporter status --config $ConfigPath --json
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
    $Now = Get-Now                                       # a fresh reading, taken when the decision is made
    if ($Now.ToUnixTimeMilliseconds() -lt $Cut - 30000) { throw $ClockBack }
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
            $Age = ($Now - $When).TotalSeconds
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
failed reading and no capture fault. The clock is read again, fresh, at the moment it decides: if it has moved back by more than 30 seconds since step 12 it says so instead of passing, and the scan age is measured against that same reading. "Scanned within three minutes" means the reporter ran recently; it does **not** prove the reporter is still running now. If it stops, it names what is missing; read "If something goes wrong".
Then follow "Tell the lead".

## Step 14: see what is still running, and stop it

Stop the windows yourself: press Ctrl-C in window 2, then in window 1. Then run `switchon-14-stopped.ps1`. It reads the list of **all** processes on this machine and **only lists** those whose command line names the live folder, with process number and program name; it stops nothing.

<!-- script: switchon-14-stopped.ps1 -->
```powershell
'== Step 14: what is still running'
Get-LiveProcesses | Select-Object ProcessId, Name
```

It prints nothing but its banner when everything has stopped (it may show its own PowerShell: ignore that). If a process is listed, go back to its window and press Ctrl-C there.

## Step 15: roll back (undo everything)

Stop both windows first (step 14). The script `switchon-15-rollback.ps1` deletes `C:\lw\live`, and nothing else, after these checks: it runs step 14's check itself and refuses while window 1 or 2 is still running; the folder holds
the ownership marker this runbook wrote (and the marker is a plain file, not a link); **no link anywhere on the way to it or inside it** (it looks at every entry and never follows a link); it is the folder `names.json`
names; and the agenttalk folder is neither the same folder nor inside it. If `names.json` is missing (step 1 stopped early), it deletes only a folder that holds nothing but the marker. If any check fails it deletes nothing
and tells you. It deletes everything else first, then `names.json`, then the marker, then the empty folder, so if a delete fails part way (a program still holds a file) the marker and `names.json` are still there and you can
run it again. It never touches the agenttalk folder.

<!-- script: switchon-15-rollback.ps1 -->
```powershell
'== Step 15: roll back'
Set-Location -LiteralPath (Split-Path -Parent $LiveRoot)           # leave the live folder (it is about to be deleted)
Assert-Plain $LiveRoot
Assert-NoLink $LiveRoot
if (-not (Test-Path -LiteralPath $LiveRoot -PathType Container)) { 'There is nothing to roll back: the folder does not exist.'; return }
$Running = @(Get-LiveProcesses)                          # the same list as step 14, without this script's own process
if ($Running.Count -gt 0) { throw ('Window 1 or 2 is still running (process {0}). Press Ctrl-C in it, then run step 15 again. Nothing was deleted.' -f $Running[0].ProcessId) }
$MarkerFile = Join-Path $LiveRoot '.owner.json'
Assert-NoLink $MarkerFile                                          # the marker is a plain file, not a link, before it is read
if (-not (Test-Path -LiteralPath $MarkerFile -PathType Leaf)) { throw 'The folder holds no ownership marker, so it is not a folder this runbook made. Nothing was deleted. Tell the lead.' }
$Marker = Get-Content -LiteralPath $MarkerFile -Raw -Encoding UTF8 | ConvertFrom-Json
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
if ($N) {
    if (($Marker.token -cne $Token) -or ($Marker.path -cne $Here)) { throw 'This is not the folder this runbook made. Nothing was deleted.' }
}
else {                                                   # names.json is missing: only a folder that holds nothing but the marker may go
    $Inside = @((New-Object IO.DirectoryInfo($Here)).GetFileSystemInfos())
    if (($Marker.path -cne $Here) -or ([string]$Marker.token -cnotmatch '^[0-9a-f]{32}$') -or $Inside.Count -ne 1 -or $Inside[0].Name -cne '.owner.json') {
        throw 'names.json is missing and the folder holds more than the ownership marker, so it cannot be told to be ours. Nothing was deleted. Tell the lead.'
    }
}
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
try {
    foreach ($Child in @(Get-ChildItem -LiteralPath $LiveRoot -Force | Where-Object { $_.Name -cne '.owner.json' -and $_.Name -cne 'names.json' })) {
        Remove-Item -LiteralPath $Child.FullName -Recurse -Force
    }
    if (Test-Path -LiteralPath $NamesFile) { Remove-Item -LiteralPath $NamesFile -Force }
    Remove-Item -LiteralPath $MarkerFile -Force
    [IO.Directory]::Delete($LiveRoot)                    # not recursive: the folder is empty by now
}
catch { throw ('A file could not be deleted (is a program still using it?). The ownership marker and names.json are kept while anything else is left, so you can run step 15 again after closing it. Tell the lead if it fails again. ' + $_.Exception.Message) }
Test-Path -LiteralPath $LiveRoot
```

It must print `False` at the end (the folder is gone). What rollback does **not** undo: nothing outside `C:\lw\live` was ever changed. The reporter's state file was version 7; a reporter built before
card 7b cannot open it, which does not matter once the folder is deleted. If you keep the folder and want to go back to an older program, keep a copy of the state file first.

## If something goes wrong

Run `switchon-help-status.ps1`. It prints the reporter's readings status: closed words, counts and Leasewise names only (never an account text, a seat name, a file name or a path). **Never paste `reporter.toml` anywhere.**

<!-- script: switchon-help-status.ps1 -->
```powershell
'== The readings status'
& $Py @PyFlags -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty readings | ConvertTo-Json -Depth 5
& $Py @PyFlags -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty operator_actions
```

The reporter's status raises three lines of its own for readings. What each means here:

- **`FORBIDDEN: this machine is not an authorized reporter for the account`.** Leasewise refused a reading with 403. On this machine it means step 5 did not take effect (the placeholder seat or the project's
  accounts). Run step 6 again; if it passes and this line stays, tell the lead. Each refused reading fails for good, and later readings are still sent.
- **`N seat file(s) name an agenttalk account that is not in readings.sources`.** A usage file in our agenttalk folder belongs to an account text that is not in your table. Step 8 proved that both texts match a usage file when it wrote them, so this line means a seat runs under another
  Windows user or another home folder than the two you typed. Run `agenttalk capacity` and compare the `home-` part and the name of each account line with what you typed. If the account is meant to be
  reported, ask the lead; if not, ignore the line.
- **`A seat file has not followed the documented capacity format for N scans in a row`.** A usage file breaks agenttalk's documented format, often because agenttalk was updated to a version that writes another format.
  Tell the lead which agenttalk version runs; nothing else is needed.

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
- Ports: Leasewise on `127.0.0.1:8443` (the identity header authenticates this machine; Leasewise refuses other addresses). Step 10 lists the machine's listening TCP endpoints (IPv4 and IPv6, any address) and starts only
  if none is on the port; step 11 requires exactly one, on `127.0.0.1`, owned by window 1's server (command line and program checked; a venv launcher's child accepted only with the launcher as parent and the same command line).
- Authorization: `http_api._authorized_reporter_for_account` accepts a machine that has a seat in a team whose project has the account (step 5's placeholder seat). Step 6 asks the same question of the store.
- The done check needs: a store query and a status command that both succeed, a reporter scan in the last 180 seconds, exactly the two expected sources, an accepted observation per account later than both the clock time and the delivered maximum recorded in `baseline.json` (schema 1, checked field by field), a scan time between 180 seconds ago and 30 seconds ahead, and no operator line, failed reading or capture fault.
- Flow after the done check: each window keeps its latest unsent figure, Leasewise gets at most one accepted figure per window every two minutes (a new reset time goes at once), and the gaps sentence of
  `reporter status` says what readings cannot do.
- The scripts' safety checks are copied from the money-reader trial (`docs/TRIAL-R1-READER-4.md`): the guard is new for this card (`C:\lw`, every ancestor, the whole live folder, the rehearsal mode) and the privacy helpers were rewritten in fix round 1 (`Assert-Private` before a write, `Assert-PrivateAfter` after one). `tests/test_switchon_scripts.py` keeps every block identical to its script and pins the shared start and the guard.
