param([switch]$KeepOutOfSource)    # the rollback sets this: it must not enter the source folder (which lives inside the folder it deletes)
# Shared start for every switch-on step script (card 7b-on; the model is trial-common.ps1 of the money-reader trial).
# It does what the open window would otherwise have to: stop at the first error, the UTF-8 writer, the privacy helpers, the entry guard, the saved names
# (only an allow-list of them; every path is recomputed from the checked roots) and Leasewise's source folder as the working folder.
$ErrorActionPreference = 'Stop'
$PyFlags = @('-E', '-s', '-B')                           # every Python call: ignore PYTHON... variables, no user site packages, no bytecode files (not -I: the scripts run from the source folder)
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
function Test-PrivateProblem {
    param([string]$Path, [bool]$OwnList)                                  # $OwnList: the item has its own list and inherits nothing; returns a sentence or nothing
    $Acl = Get-Acl -LiteralPath $Path
    if ($OwnList -and -not $Acl.AreAccessRulesProtected) { return "$Path still inherits access from its folder." }
    $Seen = @()
    foreach ($Rule in $Acl.Access) {
        if ($Rule.AccessControlType -ne 'Allow') { continue }
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Allowed -notcontains $Sid) { return "Someone else can open $Path." }
        if ($OwnList -and $Rule.IsInherited) { return "$Path inherits an entry from its folder." }
        $Seen += $Sid
    }
    if ($Seen -notcontains $Me) { return "You cannot open $Path." }
}
function Assert-Private {
    param([string]$Path, [bool]$OwnList)                                  # the check BEFORE anything secret is written
    $Problem = Test-PrivateProblem $Path $OwnList
    if ($Problem) { throw "$Problem Stop here; nothing secret has been written, and nothing was changed." }
}
function Assert-PrivateAfter {
    param([string]$Path, [bool]$OwnList)                                  # the check AFTER a secret was written
    $Problem = Test-PrivateProblem $Path $OwnList
    if ($Problem) { throw "$Problem A secret HAS ALREADY BEEN WRITTEN there. Stop here and tell the lead before you do anything else." }
}

# The entry guard (a link anywhere on the way, a folder outside C:\lw, the two roots overlapping, or saved names for another run: stop before anything is read,
# listed or entered), then the checked saved names. Every derived path is recomputed from the checked roots and checked for links; none is read back from names.json.
. (Join-Path $PSScriptRoot 'switchon-guard.ps1')
if ($N) {
    foreach ($P in $N.PSObject.Properties) { Set-Variable -Name $P.Name -Value $P.Value }
    $Src = Join-Path $LiveRoot 'src'
    $Store = Join-Path $LiveRoot 'store\inventory.db'
    $StateDir = Join-Path $LiveRoot 'state'
    $Journal = Join-Path $LiveRoot 'journal'
    $PolicyIds = Join-Path $LiveRoot 'policy-ids.json'
    $ConfigPath = Join-Path $LiveRoot 'reporter.toml'
    $Names = $NamesFile
    $BaseUrl = 'http://127.0.0.1:{0}' -f $PortNumber
    foreach ($Derived in $Src, $Store, $StateDir, $Journal, $PolicyIds, $ConfigPath, (Join-Path $LiveRoot '.owner.json'), (Join-Path $LiveRoot 'baseline.json'), (Join-Path $LiveRoot 'server.json')) {
        if (Test-Path -LiteralPath $Derived) { Assert-NoLink $Derived }
    }
    if (-not $KeepOutOfSource -and (Test-Path -LiteralPath $Src)) { Set-Location -LiteralPath $Src }   # run every Leasewise command from inside the source folder (after step 3)
}

# What the reporter reads, asked of the reporter's own scan: one small Python helper calls quota_balancer.reporter.capacityreader.scan on the agenttalk folder
# (the same code the reporter runs: bounded listing, links and folders dropped, names checked, the first 64 files, at most 16 KiB each, every field checked) and
# counts what it accepts. Mode "count": per provider, the accepted files whose account starts with "claude:" or "codex:". Mode "match": the two assembled texts
# arrive on STANDARD INPUT (never on a command line) and the answer is, per provider, how many accepted files have exactly that account. The answer holds numbers
# only; on any problem the helper prints one fixed sentence and no account text, and the step stops with a plain message.
$UsageHelper = @'
import json, sys, time
def stop(word):
    print("USAGE_SCAN_FAILED " + word)
    sys.exit(2)
try:
    from quota_balancer.reporter import capacityreader
    found = capacityreader.scan(sys.argv[1], int(time.time() * 1000))
    if found.folder_unreadable:
        stop("unreadable")
    if found.too_many_entries:
        stop("too_many")
    wanted = json.loads(sys.stdin.read()) if sys.argv[2] == "match" else None
    accounts = [item.account for item in found.files if isinstance(item.account, str)]
    answer = {}
    for provider in ("claude", "codex"):
        if wanted is None:
            answer[provider] = sum(1 for text in accounts if text.startswith(provider + ":") and len(text) > len(provider) + 1)
        else:
            answer[provider] = sum(1 for text in accounts if text == wanted[provider])
    print(json.dumps(answer))
except SystemExit:
    raise
except BaseException:
    stop("error")
'@
function Invoke-UsageScan {
    param([string]$Mode, $Texts)
    $Stdin = if ($Mode -ceq 'match') { $Texts | ConvertTo-Json -Compress -EscapeHandling EscapeNonAscii } else { '{}' }
    $Answer = @($Stdin | & $Py @PyFlags -c $UsageHelper $BusRoot $Mode)
    if ($LASTEXITCODE -ne 0) {
        $Said = $Answer -join ' '
        if ($Said -like '*unreadable*') { throw 'The agenttalk state folder could not be read. Nothing was changed. Tell the lead.' }
        if ($Said -like '*too_many*') { throw 'The agenttalk state folder holds more entries or usage files than the reporter looks at (4096 entries, 64 files). Nothing was changed. Tell the lead.' }
        throw 'The usage files could not be checked. Nothing was changed. Tell the lead.'
    }
    try { $Counts = ($Answer -join '') | ConvertFrom-Json -ErrorAction Stop } catch { $Counts = $null }
    $Fits = $Counts -and ((@($Counts.PSObject.Properties.Name) -join ',') -ceq 'claude,codex') -and (($Counts.claude -is [int]) -or ($Counts.claude -is [long])) -and (($Counts.codex -is [int]) -or ($Counts.codex -is [long]))
    if (-not $Fits) { throw 'The usage files could not be checked. Nothing was changed. Tell the lead.' }
    $Counts
}

# Strict reading of the small JSON files the scripts write (the baseline, server.json): an object is looked at by its exact type (PowerShell calls a list a custom
# object too), with exactly the expected fields and whole numbers.
function Test-Whole { param($Value) ($Value -is [int]) -or ($Value -is [long]) }
function Test-Object { param($Item) ($null -ne $Item) -and ($Item.GetType().FullName -ceq 'System.Management.Automation.PSCustomObject') }
function Test-HasAll { param($Item, [string[]]$Names) (Test-Object $Item) -and (@($Names | Where-Object { $Item.PSObject.Properties.Name -cnotcontains $_ }).Count -eq 0) }
function Test-HasOnly { param($Item, [string[]]$Names) (Test-HasAll $Item $Names) -and (@($Item.PSObject.Properties).Count -eq $Names.Count) }

# Every process on this machine whose command line names the live folder, leaving out this script's own process (ids and program names are all that is shown).
function Get-LiveProcesses {
    $Needle = $LiveRoot + [string][char]92
    @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.IndexOf($Needle, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
}

# Every program that listens on a TCP port (IPv4 and IPv6, any local address): where, which process, its program, its command line and its parent's.
# A list that cannot be read stops the step (fail closed).
function Get-PortListeners {
    param([int]$Port)
    try { $Rows = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop) }
    catch {
        if ($_.FullyQualifiedErrorId -cne 'CmdletizationQuery_NotFound,Get-NetTCPConnection') { throw "The list of programs that listen on port $Port could not be read. Nothing was started. Tell the lead." }      # only "nothing listens there" means none
        $Rows = @()
    }
    $All = @(Get-CimInstance Win32_Process)
    foreach ($Row in $Rows) {
        $Owner = $All | Where-Object { $_.ProcessId -eq $Row.OwningProcess } | Select-Object -First 1
        $Parent = if ($Owner) { $All | Where-Object { $_.ProcessId -eq $Owner.ParentProcessId } | Select-Object -First 1 }
        [pscustomobject]@{
            Address = [string]$Row.LocalAddress; ProcessId = [int]$Row.OwningProcess; ParentId = $(if ($Owner) { [int]$Owner.ParentProcessId }); Name = $(if ($Owner) { $Owner.Name } else { 'unknown' })
            Path = $(if ($Owner) { [string]$Owner.ExecutablePath }); CommandLine = $(if ($Owner) { [string]$Owner.CommandLine })
            ParentPath = $(if ($Parent) { [string]$Parent.ExecutablePath }); ParentCommandLine = $(if ($Parent) { [string]$Parent.CommandLine })
        }
    }
}

# The words step 10 passes to Python after $PyFlags: step 10 launches them and step 11 expects exactly them, from this one function.
function Get-ServeArgs {
    @('-m', 'quota_balancer', '--db', $Store, 'serve', '--host', '127.0.0.1', '--port', [string]$PortNumber)
}

function Split-CommandLine {
    param([string]$Text)                                                 # the words of a Windows command line; a double-quoted part is one word
    @([regex]::Matches($Text, '"([^"]*)"|(\S+)') | ForEach-Object { if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value } })
}

# Step 11's start check: the only program that listens on the port is the process step 10 started and recorded in server.json (never the text of a command line, which
# cannot prove what a process runs). server.json is read strictly (exactly schema 1, pid, started_utc_ticks, port; whole numbers; this run's port). The recorded process
# must still run, with the recorded start time (a reused process number does not match), and its program must be step 1's Python. Exactly one program listens on the port,
# on 127.0.0.1, and it is either that process or a direct child of it started after it (a venv's python.exe starts the real interpreter as a child, and the child owns the
# socket). Anything else is refused; a process or port list that cannot be read stops the step. What this cannot tell apart: a program under the operator's own Windows
# account that stops the server and takes the port after this check, or edits server.json.
function Assert-OurServerListens {
    $NotRecorded = 'Window 1 was not started by step 10 of this runbook. Start step 10 first. Nothing was started.'
    $RecordFile = Join-Path $LiveRoot 'server.json'
    if (-not (Test-Path -LiteralPath $RecordFile -PathType Leaf)) { throw $NotRecorded }
    try { $Record = Get-Content -LiteralPath $RecordFile -Raw -Encoding UTF8 | ConvertFrom-Json -NoEnumerate -ErrorAction Stop } catch { throw $NotRecorded }
    if (-not (Test-HasOnly $Record 'schema', 'pid', 'started_utc_ticks', 'port') -or -not (Test-Whole $Record.schema) -or $Record.schema -ne 1 -or -not (Test-Whole $Record.pid) -or $Record.pid -le 0 `
            -or -not (Test-Whole $Record.started_utc_ticks) -or $Record.started_utc_ticks -le 0 -or -not (Test-Whole $Record.port) -or $Record.port -ne $PortNumber) { throw $NotRecorded }
    $Rows = @(Get-PortListeners $PortNumber)
    if ($Rows.Count -eq 0) { throw "Nothing listens on port $PortNumber yet. Start window 1 (step 10) first and wait a few seconds. Nothing was started." }
    $NotOurs = "Port $PortNumber is held by a program that is not window 1's Leasewise server. Nothing was started, and the identity value was not sent. Tell the lead."
    try { $Server = [Diagnostics.Process]::GetProcessById([int]$Record.pid); $Started = $Server.StartTime.ToUniversalTime().Ticks } catch { throw $NotOurs }       # it has ended, or cannot be read
    if ($Started -ne [int64]$Record.started_utc_ticks) { throw $NotOurs }
    $Program = [string](Get-CimInstance Win32_Process -Filter ('ProcessId = {0}' -f [int]$Record.pid)).ExecutablePath
    if (-not $Program -or -not [IO.Path]::IsPathRooted($Program) -or [IO.Path]::GetFullPath($Program) -ine $Py) { throw $NotOurs }
    if ($Rows.Count -ne 1 -or $Rows[0].Address -cne '127.0.0.1') { throw $NotOurs }
    if ($Rows[0].ProcessId -eq [int]$Record.pid) { return }                                             # (a) the recorded process itself
    if ($Rows[0].ParentId -ne [int]$Record.pid) { throw $NotOurs }                                      # (b) its direct child, started after it
    try { $Child = [Diagnostics.Process]::GetProcessById($Rows[0].ProcessId); $ChildStarted = $Child.StartTime.ToUniversalTime().Ticks } catch { throw $NotOurs }
    if ($ChildStarted -lt $Started) { throw $NotOurs }
}

# Step 11's second start check: no reporter for this settings file is running already. This one only REFUSES, so reading command-line words is enough here (a
# second reporter is also stopped by the reporter's own exclusive state lock).
function Assert-NoReporterRunning {
    foreach ($Process in @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine })) {
        $Words = @(Split-CommandLine $Process.CommandLine)
        $After = [Array]::IndexOf(@($Words | ForEach-Object { $_.ToLowerInvariant() }), '--config')
        if ($Words -contains 'quota_balancer' -and $Words -contains 'reporter' -and $Words -contains 'run' -and $After -ge 0 -and ($After + 1) -lt $Words.Count -and $Words[$After + 1] -ieq $ConfigPath) {
            throw ('A reporter for this settings file is already running (process {0}). Nothing was started. If it is window 2 from earlier, go back to that window; otherwise tell the lead.' -f $Process.ProcessId)
        }
    }
}
