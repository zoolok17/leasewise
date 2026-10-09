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
    foreach ($Derived in $Src, $Store, $StateDir, $Journal, $PolicyIds, $ConfigPath, (Join-Path $LiveRoot '.owner.json'), (Join-Path $LiveRoot 'baseline.json')) {
        if (Test-Path -LiteralPath $Derived) { Assert-NoLink $Derived }
    }
    if (-not $KeepOutOfSource -and (Test-Path -LiteralPath $Src)) { Set-Location -LiteralPath $Src }   # run every Leasewise command from inside the source folder (after step 3)
}

# What the reporter reads: <agenttalk folder>\state\*.capacity.json, the first 64 by name, at most 16 KiB each, links skipped. Returns one object (Provider, Account)
# for each file the reporter could use as a source: schema_version exactly the whole number 2, scope "account", a non-empty text account that starts with
# "claude:" or "codex:". It prints nothing; the caller counts or compares in memory.
function Get-UsageAccounts {
    $Folder = Join-Path $BusRoot 'state'
    try { $Paths = [string[]]@([IO.Directory]::EnumerateFileSystemEntries($Folder, '*.capacity.json')) } catch { throw 'The agenttalk state folder could not be read. Nothing was changed. Tell the lead.' }
    [Array]::Sort($Paths, [StringComparer]::Ordinal)
    foreach ($Path in ($Paths | Select-Object -First 64)) {
        $Info = New-Object IO.FileInfo($Path)
        if (($Info.Attributes -band ([IO.FileAttributes]::ReparsePoint -bor [IO.FileAttributes]::Directory)) -or $Info.Length -gt 16384) { continue }
        try { $Data = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8) | ConvertFrom-Json -ErrorAction Stop } catch { continue }
        if (-not (($null -ne $Data) -and ($Data.GetType().FullName -ceq 'System.Management.Automation.PSCustomObject'))) { continue }
        $Version, $Scope, $Account = $Data.schema_version, $Data.scope, $Data.account
        if (-not ((($Version -is [int]) -or ($Version -is [long])) -and $Version -eq 2 -and $Scope -is [string] -and $Scope -ceq 'account' -and $Account -is [string])) { continue }
        foreach ($Provider in 'claude', 'codex') {
            if ($Account.StartsWith($Provider + ':', [StringComparison]::Ordinal) -and $Account.Length -gt ($Provider.Length + 1)) { [pscustomobject]@{ Provider = $Provider; Account = $Account } }
        }
    }
}

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
            Address = [string]$Row.LocalAddress; ProcessId = [int]$Row.OwningProcess; Name = $(if ($Owner) { $Owner.Name } else { 'unknown' })
            Path = $(if ($Owner) { [string]$Owner.ExecutablePath }); CommandLine = $(if ($Owner) { [string]$Owner.CommandLine })
            ParentPath = $(if ($Parent) { [string]$Parent.ExecutablePath }); ParentCommandLine = $(if ($Parent) { [string]$Parent.CommandLine })
        }
    }
}

function Split-CommandLine {
    param([string]$Text)                                                 # the words of a Windows command line; a double-quoted part is one word
    @([regex]::Matches($Text, '"([^"]*)"|(\S+)') | ForEach-Object { if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value } })
}

# Step 11's start check: the only program that listens on the port is window 1's Leasewise server (listening on 127.0.0.1, started by step 1's Python with
# "quota_balancer", "serve" and "--db" followed by exactly the store). A venv's python.exe is a launcher that starts the real interpreter as a child, and
# the child owns the socket: a child counts only if its parent is step 1's Python with the same command line.
function Assert-OurServerListens {
    $Rows = @(Get-PortListeners $PortNumber)
    if ($Rows.Count -eq 0) { throw "Nothing listens on port $PortNumber yet. Start window 1 (step 10) first and wait a few seconds. Nothing was started." }
    $NotOurs = "Port $PortNumber is held by a program that is not window 1's Leasewise server. Nothing was started, and the identity value was not sent. Tell the lead."
    if ($Rows.Count -ne 1 -or $Rows[0].Address -cne '127.0.0.1') { throw $NotOurs }
    $Row = $Rows[0]
    $Words = @(Split-CommandLine $Row.CommandLine)
    $After = [Array]::IndexOf(@($Words | ForEach-Object { $_.ToLowerInvariant() }), '--db')
    $Fits = $Words -contains 'quota_balancer' -and $Words -contains 'serve' -and $After -ge 0 -and ($After + 1) -lt $Words.Count
    if (-not $Fits -or [IO.Path]::GetFullPath($Words[$After + 1]) -ine $Store) { throw $NotOurs }
    $Direct = $Row.Path -ieq $Py
    $ViaLauncher = $Row.ParentPath -ieq $Py -and $Row.ParentCommandLine -ieq $Row.CommandLine
    if (-not ($Direct -or $ViaLauncher)) { throw $NotOurs }
}

# Step 11's second start check: no reporter for this settings file is running already.
function Assert-NoReporterRunning {
    foreach ($Process in @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine })) {
        $Words = @(Split-CommandLine $Process.CommandLine)
        $After = [Array]::IndexOf(@($Words | ForEach-Object { $_.ToLowerInvariant() }), '--config')
        if ($Words -contains 'quota_balancer' -and $Words -contains 'reporter' -and $Words -contains 'run' -and $After -ge 0 -and ($After + 1) -lt $Words.Count -and $Words[$After + 1] -ieq $ConfigPath) {
            throw ('A reporter for this settings file is already running (process {0}). Nothing was started. If it is window 2 from earlier, go back to that window; otherwise tell the lead.' -f $Process.ProcessId)
        }
    }
}
