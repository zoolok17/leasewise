# The rehearsal of docs/TRIAL-R1-READER-4.md (card R1-READER-4-prep). Not a test: the name does not start with `test_`, so `discover` never collects it.
#
# It follows the runbook LITERALLY: it reads the runbook, takes every PowerShell block that carries a `<!-- rehearse: ... -->` tag and runs that exact
# text, in order, in one session. Only three things differ from what the operator does:
#   * `rehearse: replace`  the block where the operator types the names of their machine is replaced by the names of this rehearsal (a pretend
#                          gateway program instead of the real one, a scratch folder instead of the real one);
#   * `rehearse: window`   the two blocks the operator runs in separate windows are started as hidden background processes (by Start-Process, so the
#                          process ids are known and the processes can be ended by exact id), with a bounded lifetime;
#   * every runnable block is wrapped by the runbook as `. { $ErrorActionPreference = 'Stop' ... }` (one unit, however it is pasted); for the two window
#                          blocks the rehearsal keeps that wrapper but takes the last line (the program call) out and starts the program itself;
#   * between the runbook's own steps the rehearsal moves the pretend gateway forward, and for the drill it stops the reader and waits.
# Nothing real is touched: no gateway, no agenttalk, no provider. Run it with Windows PowerShell 5.1 (the desktop's default) or PowerShell 7.
#
#   powershell.exe -NoProfile -File tests\trial\rehearse.ps1 -Root <fresh scratch folder> -Repo <leasewise checkout> -Python <python.exe>

param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$Runbook = '',
    [int]$MaxMinutes = 20,
    [ValidateSet('busy', 'idle-older', 'idle-empty')][string]$Start = 'busy',   # how the pretend gateway's ledger starts: spending this month, or a quiet start of a month
    [switch]$OddHome                                                             # put an apostrophe and non-ASCII letters in the application-data folder name
)

$ErrorActionPreference = 'Stop'
$ExpectStop = $false
$Started = Get-Date
$Overall = [Diagnostics.Stopwatch]::StartNew()
function Check-Time { if ($Overall.Elapsed.TotalMinutes -gt $MaxMinutes) { throw "the rehearsal ran longer than its bound of $MaxMinutes minutes" } }
if (-not $Runbook) { $Runbook = Join-Path $Repo 'docs\TRIAL-R1-READER-4.md' }
if (Test-Path $Root) { throw "The rehearsal folder must not exist yet: $Root" }
New-Item -ItemType Directory -Force $Root | Out-Null
$Log = Join-Path $Root 'rehearsal.log'
$Timings = New-Object System.Collections.ArrayList
$Background = New-Object System.Collections.ArrayList        # the process OBJECTS this script started: never a saved number looked up again

function Say([string]$Text) { Write-Host $Text; Add-Content -LiteralPath $Log -Value $Text -Encoding UTF8 }

function Names-Under([string]$Path) {
    if (-not (Test-Path $Path)) { return @() }
    return @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name | Sort-Object)
}

# --- the places the run could write, before -------------------------------------------------------------------------------------------------
# The rehearsal gives the run its OWN application-data and temp folders under the scratch root, so a stray write cannot hide among other programs'
# files. The real top-level folders are listed too, before and after.
$RealLocal = $env:LOCALAPPDATA; $RealRoaming = $env:APPDATA; $RealTemp = $env:TEMP
$Fake = Join-Path $Root 'home'
$LocalName = 'Local'
if ($OddHome) { $LocalName = "Local O'Brien-" + [char]0x00DC + 'n' + [char]0x00EF }     # an apostrophe, U-umlaut and i-diaeresis, built so this file stays ASCII
foreach ($Sub in $LocalName, 'Roaming', 'Temp') { New-Item -ItemType Directory -Force -Path (Join-Path $Fake $Sub) | Out-Null }
$env:LOCALAPPDATA = Join-Path $Fake $LocalName; $env:APPDATA = Join-Path $Fake 'Roaming'; $env:TEMP = Join-Path $Fake 'Temp'; $env:TMP = $env:TEMP
$Before = [ordered]@{
    'fake LOCALAPPDATA'  = Names-Under $env:LOCALAPPDATA
    'fake APPDATA'       = Names-Under $env:APPDATA
    'fake TEMP'          = Names-Under $env:TEMP
    'real LOCALAPPDATA'  = Names-Under $RealLocal
    'real APPDATA'       = Names-Under $RealRoaming
    'real TEMP (ours)'   = @(Names-Under $RealTemp | Where-Object { $_ -like 'quota-*' -or $_ -like 'leasewise*' -or $_ -like 'tmp*trial*' })
    'repository status'  = @(git -C $Repo status --porcelain)
}

# --- port 8443 must be free (the runbook's own prerequisite), or the server of the rehearsal could not start ------------------------------------------
$Probe = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 8443)
try { $Probe.Start(); $Probe.Stop() } catch { throw 'port 8443 on this machine is already in use: free it first' }

# --- the made-up machine ----------------------------------------------------------------------------------------------------------------------
$GatewayDir = Join-Path $Root 'pretend-gateway'
New-Item -ItemType Directory -Force $GatewayDir | Out-Null
Copy-Item (Join-Path $Repo 'tests\trial\pretend_gateway.py') $GatewayDir
$Pretend = Join-Path $GatewayDir 'pretend_gateway.py'
& $Python $Pretend init $Start
if ($LASTEXITCODE -ne 0) { throw 'the pretend gateway could not start its ledger' }
Say ("pretend gateway ledger starts as: {0}; odd home folder: {1}" -f $Start, [bool]$OddHome)
$ArchivePath = Join-Path $Root 'leasewise-rehearsal.zip'
git -C $Repo archive --format=zip -o $ArchivePath HEAD
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
$ArchiveSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash
Say ("archive of {0} sha256 {1}" -f (git -C $Repo rev-parse HEAD), $ArchiveSha)

$Preamble = @"
`$Trial        = Join-Path `$env:LOCALAPPDATA 'leasewise-trial'
`$Py           = '$Python'
`$GatewayExe   = '$Python'
`$GatewayPre   = @('$Pretend')
`$MachineAlias = 'desktop'
`$AccountAlias = 'trial-account'
`$GatewayAlias = 'trial-gateway'
`$BudgetMicroEur = 100000000
`$ShareMicroEur  = 100000000
`$Archive      = '$ArchivePath'
`$ExpectedSha  = '$ArchiveSha'
"@

# --- read the runbook ------------------------------------------------------------------------------------------------------------------------
$Blocks = New-Object System.Collections.ArrayList
$Tag = $null; $InFence = $false; $Current = New-Object System.Collections.ArrayList
foreach ($Line in (Get-Content -LiteralPath $Runbook)) {
    if (-not $InFence) {
        if ($Line -match '^<!-- rehearse: (.+?) -->\s*$') { $Tag = $Matches[1]; continue }
        if ($Line -match '^```powershell\s*$') { $InFence = $true; $Current.Clear() }
    } else {
        if ($Line -match '^```\s*$') { $InFence = $false; if ($Tag) { [void]$Blocks.Add([pscustomobject]@{ Tag = $Tag; Text = ($Current -join "`n") }) }; $Tag = $null }
        else { [void]$Current.Add($Line) }
    }
}
Say ("runbook: {0} tagged blocks" -f $Blocks.Count)

# One block is run by this script block, DOT-SOURCED (`. $RunBlockSb`) so that the variables the runbook makes stay in this script's scope, as they
# would in the operator's window. The caller sets $BlockArg and $NumberArg first.
$RunBlockSb = {
    Check-Time
    $Watch = [Diagnostics.Stopwatch]::StartNew()
    $Kind = ($BlockArg.Tag -split ' ')[0]
    Say ("--- block {0} ({1}) ---" -f $NumberArg, $BlockArg.Tag)
    switch ($Kind) {
        'replace' { $BlockText = $Preamble }
        'window' {
            $WindowInner = Unwrap-Lines $BlockArg.Text
            $LastLine = $WindowInner[-1]
            if ($LastLine -notmatch '^& (\$[\w.]+) @(\w+)$') { throw "window block must end, inside its braces, with: & `$Exe @Args  (got: $LastLine)" }
            $ExeExpression = $Matches[1]; $ArgName = $Matches[2]
            $BlockText = (@('. {', "`$ErrorActionPreference = 'Stop'") + @($WindowInner[0..($WindowInner.Count - 2)]) + @('}')) -join "`n"
        }
        default { $BlockText = $BlockArg.Text }
    }
    $ErrorActionPreference = 'Continue'                  # the operator's window is not strict either; the blocks check for themselves and throw
    $Collected = New-Object System.Collections.ArrayList
    $BlockStopped = $false
    try { . { Invoke-Expression $BlockText } | ForEach-Object { [void]$Collected.Add(($_ | Out-String).TrimEnd()) } }
    catch { $BlockStopped = $true; [void]$Collected.Add('STOPPED BY THE BLOCK: ' + $_.Exception.Message) }
    $BlockOutput = ($Collected -join "`n") + "`n"
    $ErrorActionPreference = 'Stop'
    if ($Kind -eq 'window') {
        $WindowExe = Invoke-Expression $ExeExpression; $WindowArgs = [string[]](Get-Variable $ArgName -ValueOnly)       # a real list of arguments (a bare @() would keep the list as ONE item)
        $WindowLine = ($WindowArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { [string]$_ } }) -join ' '
        Say ("window command line: " + $WindowLine)
        # the wrapped block as the operator pastes it (one line at a time), with only the last line swapped for a print of the program and arguments
        $CheckLines = @($BlockText -split "`n")
        $CheckLines = @($CheckLines[0..($CheckLines.Count - 2)]) + @("Write-Output ('EXE=' + ($ExeExpression))", "Write-Output ('ARGS=' + (`$$ArgName -join '|'))", '}', '')
        $Check = Invoke-LineDrill ("window-" + $ArgName) $CheckLines
        if ($Check.Err.Trim()) { throw "the wrapped window block printed an error when pasted line by line: $($Check.Err.Trim())" }
        if (($Check.Out -notmatch ('EXE=' + [regex]::Escape([string]$WindowExe))) -or ($Check.Out -notmatch ('ARGS=' + [regex]::Escape(($WindowArgs -join '|'))))) { throw "the wrapped window block pasted line by line does not give the program and arguments that were started" }
        Say ("drill: the wrapped window block {0} pasted line by line gives the same program and arguments, with nothing in red" -f $ArgName)
        $OutFile = Join-Path $Root ("window-{0}.out.txt" -f $ArgName); $ErrFile = Join-Path $Root ("window-{0}.err.txt" -f $ArgName)
        $Proc = Start-Process -FilePath $WindowExe -ArgumentList $WindowLine -PassThru -WindowStyle Hidden -WorkingDirectory $Src -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
        $null = $Proc.Handle                       # keep the handle, so the exit code can be read after the process ends
        [void]$Background.Add($Proc)
        Set-Variable -Name ("Proc_" + $ArgName) -Value $Proc
        $BlockOutput += "started $ArgName as process $($Proc.Id)`n"
        Start-Sleep -Seconds 4
    }
    $Watch.Stop()
    Say $BlockOutput.TrimEnd()
    if ($BlockStopped -and -not $ExpectStop) { throw "block $NumberArg stopped, and the rehearsal did not expect it" }
    [void]$Timings.Add([pscustomobject]@{ Block = $NumberArg; Tag = $BlockArg.Tag; Seconds = [math]::Round($Watch.Elapsed.TotalSeconds, 1) })
}

function Newest-Seq {
    $Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
    $G = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
    if ($G.evidence) { return [int]$G.evidence.latest.seq }
    return 0
}

function Adopted-Seq {
    $Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
    $G = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
    if ($G.evidence.adopted) { return [int]$G.evidence.adopted.seq }
    return 0
}

function Assert-Alive {
    foreach ($P in @($Background)) { if ($P.HasExited) { throw "a background process ended early (exit code $($P.ExitCode))" } }
}

function Wait-NewReport([int]$Than, [int]$Seconds) {
    $Deadline = (Get-Date).AddSeconds($Seconds)
    do { Start-Sleep -Seconds 5; Check-Time; Assert-Alive; $Now = Newest-Seq } until ($Now -gt $Than -or (Get-Date) -gt $Deadline)
    if ($Now -le $Than) { throw "no new report within $Seconds seconds" }
    return $Now
}

# (Process objects keep their handle, so the exit code can be read after the process ends.)
# Deletion safety (the liaison's rule of 2026-10-05): before a folder tree is deleted, count its links (reparse points: junctions, symbolic links) WITHOUT going
# into any, and delete nothing if there is one. The same walk as the runbook's step 12.
function Count-Links([string]$Path) {
    if ((New-Object IO.DirectoryInfo($Path)).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { return 1 }       # a linked root is never entered
    $Found = 0
    $Pending = New-Object System.Collections.Generic.Stack[string]
    $Pending.Push($Path)
    while ($Pending.Count -gt 0) {
        foreach ($Entry in (New-Object IO.DirectoryInfo($Pending.Pop())).GetFileSystemInfos()) {
            if ($Entry.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Found++ }
            elseif ($Entry -is [IO.DirectoryInfo]) { $Pending.Push($Entry.FullName) }
        }
    }
    return $Found
}
function Remove-Scaffolding([string]$Path) {
    if ((Count-Links $Path) -gt 0) { throw "the scaffolding folder contains a link: nothing was deleted ($Path)" }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

# End a process through the object this script started (never a saved number), wait for it, and drop it from the list at once.
function Stop-Proc($P) {
    if (-not $P.HasExited) { $P.Kill() }
    [void]$P.WaitForExit(10000)
    Say ("process {0} ended, exit code {1}" -f $P.Id, $P.ExitCode)
    $Background.Remove($P)
}
function Stop-Background { foreach ($P in @($Background)) { Stop-Proc $P } }

# The runbook wraps every runnable block as `. {`, the Stop line, the lines, an optional `$ErrorActionPreference = 'Continue'`, and `}`. This returns the lines inside.
function Unwrap-Lines([string]$Text) {
    $L = @($Text -split "`n")
    if ($L.Count -lt 4 -or $L[0] -ne '. {' -or $L[1] -ne "`$ErrorActionPreference = 'Stop'" -or $L[-1] -ne '}') { throw 'a runnable block must start with ". {" and the Stop line, and end with "}"' }
    $Inner = @($L[2..($L.Count - 2)])
    if ($Inner[-1] -eq "`$ErrorActionPreference = 'Continue'") { $Inner = @($Inner[0..($Inner.Count - 2)]) }
    return , $Inner
}

# The line-by-line paste: one live PowerShell process reads its input from a pipe, as if the operator pasted a block into a window and the window took each
# line as it arrived (`-Command -` runs every complete line at once and needs a blank line to end a multi-line construct, just like a pasted window).
function Invoke-LineDrill([string]$Name, [string[]]$Lines) {
    $Psi = New-Object Diagnostics.ProcessStartInfo
    $Psi.FileName = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $Psi.Arguments = '-NoProfile -NonInteractive -Command -'
    $Psi.UseShellExecute = $false; $Psi.CreateNoWindow = $true
    $Psi.RedirectStandardInput = $true; $Psi.RedirectStandardOutput = $true; $Psi.RedirectStandardError = $true
    $Psi.EnvironmentVariables['PSModulePath'] = [Environment]::GetEnvironmentVariable('PSModulePath', 'Machine')
    $P = New-Object Diagnostics.Process
    $P.StartInfo = $Psi
    [void]$P.Start()
    [void]$Background.Add($P)
    $OutTask = $P.StandardOutput.ReadToEndAsync(); $ErrTask = $P.StandardError.ReadToEndAsync()
    $P.StandardInput.WriteLine('# begin')                 # the first line may arrive with a byte-order mark in front of it: let a comment take it
    foreach ($Line in $Lines) { $P.StandardInput.WriteLine($Line); $P.StandardInput.Flush(); Start-Sleep -Milliseconds 100 }
    $P.StandardInput.Close()
    if (-not $P.WaitForExit(120000)) { Stop-Proc $P; throw "line-by-line drill '$Name' did not finish in 120 seconds" }
    $P.WaitForExit()
    $Result = [pscustomobject]@{ Out = $OutTask.Result; Err = $ErrTask.Result; ExitCode = $P.ExitCode }
    $Background.Remove($P)
    $DrillLogs = Join-Path $Root 'line-drill-logs'
    New-Item -ItemType Directory -Force -Path $DrillLogs | Out-Null
    Set-Content -LiteralPath (Join-Path $DrillLogs ($Name + '.out.txt')) -Value $Result.Out -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $DrillLogs ($Name + '.err.txt')) -Value $Result.Err -Encoding UTF8
    return $Result
}

try {
    $Number = 0
    # runbook steps 1 to 6 (names, archive, checks, store, identity, start the two windows)
    $Index = 0
    function Next-Block { $Script:Index++; return $Blocks[$Script:Index - 1] }
    foreach ($WantTag in 'replace', 'run', 'run', 'run', 'run', 'run', 'run', 'window ServeArgs', 'window RunArgs') {
        $Block = Next-Block
        if ($Block.Tag -ne $WantTag) { throw "runbook order changed: expected '$WantTag', found '$($Block.Tag)'" }
        $Number++; $BlockArg = $Block; $NumberArg = $Number; . $RunBlockSb
        if ($WantTag -eq 'replace') {
            # drill: a folder that already exists must be refused without a single change (a sentinel file proves it)
            New-Item -ItemType Directory -Path $Trial | Out-Null
            Set-Content -LiteralPath (Join-Path $Trial 'precious.txt') -Value 'do not touch' -Encoding ascii
            $Listing = @(Get-ChildItem -LiteralPath $Trial -Force | Select-Object -ExpandProperty Name)
            $ExpectStop = $true; $Number++; $BlockArg = $Blocks[$Index]; $NumberArg = $Number; . $RunBlockSb; $ExpectStop = $false
            if (-not $BlockStopped) { throw 'an existing folder was not refused' }
            $ListingAfter = @(Get-ChildItem -LiteralPath $Trial -Force | Select-Object -ExpandProperty Name)
            if (($Listing -join ',') -ne ($ListingAfter -join ',')) { throw 'the refused folder was changed' }
            if ((Get-Content -LiteralPath (Join-Path $Trial 'precious.txt') -Raw).Trim() -ne 'do not touch') { throw 'the sentinel file was changed' }
            Say "drill: the existing folder was refused; its only content, the sentinel file, is untouched ($($Listing -join ', '))"
            Remove-Scaffolding $Trial                                # the rehearsal's own scaffolding, made a moment ago by this script (no link inside, checked first)
        }
    }
    # drill: the privacy check really catches a folder that someone else can open (the runbook's own functions, on a scratch folder)
    $AclProbe = Join-Path $Root 'acl-probe'
    New-Item -ItemType Directory -Path $AclProbe | Out-Null
    Protect-Path $AclProbe $true
    Assert-Private $AclProbe $true                                  # private: passes
    $ProbeAcl = Get-Acl -LiteralPath $AclProbe
    $UsersGroup = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')
    $ProbeAcl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($UsersGroup, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    Set-Acl -LiteralPath $AclProbe -AclObject $ProbeAcl
    $Caught = $false
    try { Assert-Private $AclProbe $true } catch { $Caught = ($_.Exception.Message -like 'Someone else can open*') }
    if (-not $Caught) { throw 'the privacy check did not catch a folder that the Users group can open' }
    Say "drill: the privacy check refused a folder the Users group can open"
    # drill: step 1 and step 12 pasted ONE LINE AT A TIME into a live PowerShell process; the unwrapped block must fail it (red first), the wrapped block must pass
    $DrillRoot = Join-Path $Root 'line-drill'
    New-Item -ItemType Directory -Path $DrillRoot | Out-Null
    $Step1Block = $Blocks | Where-Object { $_.Text -match 'function Protect-Path' } | Select-Object -First 1
    $Step12Block = $Blocks | Where-Object { $_.Text -match 'Remove-Item' } | Select-Object -First 1
    function Block-Lines($Block, [bool]$Wrapped) { if ($Wrapped) { return , @($Block.Text -split "`n") } else { return , (Unwrap-Lines $Block.Text) } }
    function Prelude-Lines([string]$TrialPath) { $L = @($Preamble -split "`r?`n"); $L[0] = "`$Trial = '$TrialPath'"; return , $L }
    foreach ($Wrapped in $false, $true) {
        $Word = if ($Wrapped) { 'WRAPPED' } else { 'UNWRAPPED' }
        # (a) step 1 on a folder that already exists, holding a sentinel file
        $T = Join-Path $DrillRoot ("existing-" + $Word)
        New-Item -ItemType Directory -Path $T | Out-Null
        $Sentinel = Join-Path $T 'precious.txt'
        Set-Content -LiteralPath $Sentinel -Value 'do not touch' -Encoding ascii
        $Pre = Prelude-Lines $T; $One = Block-Lines $Step1Block $Wrapped; $Twelve = Block-Lines $Step12Block $Wrapped
        $ListBefore = (Names-Under $T) -join ','; $AclBefore = (Get-Acl -LiteralPath $T).Sddl
        $Run = Invoke-LineDrill ("existing-" + $Word) ($Pre + $One + @(''))
        $Refused = [bool](($Run.Err -replace '\s+', ' ') -match 'already exists\. Nothing was changed')
        $SentinelOk = ((Get-Content -LiteralPath $Sentinel -Raw).Trim() -eq 'do not touch')
        $ListOk = (((Names-Under $T) -join ',') -eq $ListBefore)
        $AclOk = ((Get-Acl -LiteralPath $T).Sddl -eq $AclBefore)
        $MarkerOk = -not (Test-Path -LiteralPath (Join-Path $T '.owner.json'))
        Say ("drill [{0} step 1, line by line, existing folder]: refusal printed={1} sentinel kept={2} listing unchanged={3} folder access list unchanged={4} no marker written={5}" -f $Word, $Refused, $SentinelOk, $ListOk, $AclOk, $MarkerOk)
        if (-not $Refused) { throw "line drill ($Word step 1): the refusal was not printed" }
        if ($Wrapped -and -not ($SentinelOk -and $ListOk -and $AclOk -and $MarkerOk)) { throw 'line drill (wrapped step 1): the existing folder was changed' }
        if (-not $Wrapped -and ($SentinelOk -and $ListOk -and $AclOk -and $MarkerOk)) { throw 'line drill (unwrapped step 1): the unwrapped block did not change the folder, so this drill proves nothing' }
        # (b) step 12 with a wrong token, after a real step 1
        $T = Join-Path $DrillRoot ("rollback-" + $Word)
        $Pre = Prelude-Lines $T
        $Run = Invoke-LineDrill ("rollback-" + $Word) ($Pre + $One + @('', "`$Token = 'not-the-token'") + $Twelve + @(''))
        $Refused = [bool](($Run.Err -replace '\s+', ' ') -match 'not the folder this runbook made')
        $Kept = Test-Path -LiteralPath $T
        $MarkerKept = Test-Path -LiteralPath (Join-Path $T '.owner.json')
        Say ("drill [{0} step 12, line by line, wrong token]: refusal printed={1} folder still there={2} marker still there={3}" -f $Word, $Refused, $Kept, $MarkerKept)
        if (-not $Refused) { throw "line drill ($Word step 12): the refusal was not printed" }
        if ($Wrapped -and -not ($Kept -and $MarkerKept)) { throw 'line drill (wrapped step 12): a rollback with the wrong token deleted something' }
        if (-not $Wrapped -and $Kept) { throw 'line drill (unwrapped step 12): the unwrapped block deleted nothing, so this drill proves nothing' }
        if (-not $Wrapped) { Say 'red first: the UNWRAPPED step 12 block, pasted a line at a time, printed the refusal in red and then went on and DELETED the folder' }
    }
    # drill: a link inside the trial folder. A junction (several levels down) points at a sentinel folder OUTSIDE the trial folder; both live in the scratch root.
    # Step 12 is pasted one line at a time: it must refuse, and the folder, the junction and the sentinel must all be intact.
    $LinkTrial = Join-Path $DrillRoot 'link-trial'
    $Outside = Join-Path $Root 'outside-sentinel'
    New-Item -ItemType Directory -Path $Outside | Out-Null
    Set-Content -LiteralPath (Join-Path $Outside 'keep.txt') -Value 'do not touch' -Encoding ascii
    $OutsideListing = (Names-Under $Outside) -join ','
    $Junction = Join-Path $LinkTrial 'deep\inner\link-to-outside'
    $Pre = Prelude-Lines $LinkTrial
    $One = Block-Lines $Step1Block $true
    $Twelve = Block-Lines $Step12Block $true
    $MakeLink = @('', "New-Item -ItemType Directory -Path '$(Split-Path $Junction)' -Force | Out-Null",
                  "New-Item -ItemType Junction -Path '$Junction' -Target '$Outside' | Out-Null")
    $Run = Invoke-LineDrill 'link-rollback' ($Pre + $One + $MakeLink + $Twelve + @(''))
    $LinkRefused = [bool](($Run.Err -replace '\s+', ' ') -match 'The trial folder contains a link\. Nothing was deleted\. Tell the lead\.')
    $TrialKept = Test-Path -LiteralPath $LinkTrial
    $JunctionKept = (Test-Path -LiteralPath $Junction) -and ((New-Object IO.DirectoryInfo($Junction)).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint))
    $SentinelKept = ((Get-Content -LiteralPath (Join-Path $Outside 'keep.txt') -Raw).Trim() -eq 'do not touch') -and (((Names-Under $Outside) -join ',') -eq $OutsideListing)
    Say ("drill [WRAPPED step 12, line by line, a junction inside]: refusal printed={0} trial folder kept={1} junction kept={2} sentinel outside intact={3}" -f $LinkRefused, $TrialKept, $JunctionKept, $SentinelKept)
    if (-not ($LinkRefused -and $TrialKept -and $JunctionKept -and $SentinelKept)) { throw 'link drill: the rollback did not refuse cleanly' }
    if ((Count-Links $LinkTrial) -ne 1) { throw 'link drill: the kit own link count is not 1' }
    # clean up safely: remove the junction ITSELF (not recursive, so its target is never entered), check the sentinel again, then the scaffolding
    [IO.Directory]::Delete($Junction)
    if (Test-Path -LiteralPath $Junction) { throw 'link drill: the junction is still there' }
    if (-not (Test-Path -LiteralPath (Join-Path $Outside 'keep.txt'))) { throw 'link drill: removing the junction touched the sentinel' }
    Remove-Scaffolding $LinkTrial
    Say 'drill: the junction was removed on its own, the sentinel outside is intact, the scaffolding was removed after a link-free check'
    # drill: the trial folder ITSELF is a link (fix round 1). The root is a junction to a target inside the scratch root; the target holds a file and a second junction
    # (to the outside sentinel). Nothing may be enumerated through the root: step 12 must refuse first thing (probe variables set beforehand stay untouched), and
    # Count-Links must answer exactly 1 (entering the target would add the inner junction and give 2).
    $RootTarget = Join-Path $DrillRoot 'rootlink-target'
    $RootLink = Join-Path $DrillRoot 'rootlink'
    $InnerLink = Join-Path $RootTarget 'inner-link'
    $Pre = Prelude-Lines $RootTarget
    $One = Block-Lines $Step1Block $true
    $Twelve = Block-Lines $Step12Block $true
    $Setup = @('', "New-Item -ItemType Junction -Path '$InnerLink' -Target '$Outside' | Out-Null",
               "New-Item -ItemType Junction -Path '$RootLink' -Target '$RootTarget' | Out-Null",
               "`$Trial = '$RootLink'", '$Links = -1', "`$Pending = 'untouched'")
    $Run = Invoke-LineDrill 'root-link-rollback' ($Pre + $One + $Setup + $Twelve + @('', '"PROBE Links=$Links Pending=$Pending"', ''))
    $RootRefused = [bool](($Run.Err -replace '\s+', ' ') -match 'The trial folder contains a link\. Nothing was deleted\. Tell the lead\.')
    $NotWalked = [bool]($Run.Out -match 'PROBE Links=-1 Pending=untouched')
    $TargetKept = (Test-Path -LiteralPath (Join-Path $RootTarget '.owner.json')) -and (Test-Path -LiteralPath $InnerLink)
    $SentinelKept = ((Get-Content -LiteralPath (Join-Path $Outside 'keep.txt') -Raw).Trim() -eq 'do not touch')
    $RootCount = Count-Links $RootLink
    $OldWalk = { param($Path)       # the first version of the walk (read only): it counted a linked root and still entered it
        $Found = 0
        if ((New-Object IO.DirectoryInfo($Path)).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Found++ }
        $Stack = New-Object System.Collections.Generic.Stack[string]; $Stack.Push($Path)
        while ($Stack.Count -gt 0) { foreach ($E in (New-Object IO.DirectoryInfo($Stack.Pop())).GetFileSystemInfos()) {
            if ($E.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Found++ } elseif ($E -is [IO.DirectoryInfo]) { $Stack.Push($E.FullName) } } }
        $Found }
    $OldCount = & $OldWalk $RootLink
    Say ("drill [WRAPPED step 12, line by line, the trial folder itself is a junction]: refusal printed={0} nothing walked (probe untouched)={1} target and inner junction kept={2} sentinel intact={3}" -f $RootRefused, $NotWalked, $TargetKept, $SentinelKept)
    Say ("drill [Count-Links on a linked root]: new walk counts {0} (it did not enter the target); red first: the first version of the walk counted {1} because it entered the target and found its inner junction" -f $RootCount, $OldCount)
    if (-not ($RootRefused -and $NotWalked -and $TargetKept -and $SentinelKept)) { throw 'root link drill: the rollback did not refuse cleanly before walking' }
    if ($RootCount -ne 1) { throw 'root link drill: Count-Links entered a linked root' }
    if ($OldCount -ne 2) { throw 'root link drill: the first version of the walk did not show the fault, so this drill proves nothing' }
    # clean up: only the junctions themselves, one at a time, non-recursive (a link is never entered); then the target through the link-free check
    [IO.Directory]::Delete($RootLink)
    [IO.Directory]::Delete($InnerLink)
    if ((Test-Path -LiteralPath $RootLink) -or (Test-Path -LiteralPath $InnerLink)) { throw 'root link drill: a junction is still there' }
    if (-not (Test-Path -LiteralPath (Join-Path $Outside 'keep.txt'))) { throw 'root link drill: removing a junction touched the sentinel' }
    Remove-Scaffolding $RootTarget
    Say 'drill: both junctions were removed on their own, the sentinel outside is intact, the target was removed after a link-free check'
    # step 7 (wait for the first report), step 8 (compare, approve), step 9 (blockers, helper, adopt), step 10 (passed)
    foreach ($Count in 1..5) { $Block = Next-Block; $Number++; $BlockArg = $Block; $NumberArg = $Number; . $RunBlockSb }
    # (the 5 blocks: wait, approve, blockers, adopt, passed)
    $SeqAfterFirstAdopt = Newest-Seq
    Say "rehearsal: the pretend gateway moves forward (one more receipt, more settled money); waiting for the next report"
    & $Python $Pretend advance
    $Null = Wait-NewReport $SeqAfterFirstAdopt 150
    # the runbook says to run the step-10 check again: it is the last block we ran
    $Number++; $BlockArg = $Blocks[$Index - 1]; $NumberArg = $Number; . $RunBlockSb
    # the drill: stop the reader, let the newest report age past 120 seconds, try to adopt, see the refusal, restart, adopt again
    Say "rehearsal drill: stopping the reader (process $($Proc_RunArgs.Id)) and waiting for the newest report to go stale"
    Stop-Proc $Proc_RunArgs
    Start-Sleep -Seconds 135
    Check-Time
    $StaleBlock = $Blocks | Where-Object { $_.Text -match 'blockers=\[\{1\}\]' -and $_.Text -notmatch 'open items' } | Select-Object -First 1
    $Number++; $BlockArg = $StaleBlock; $NumberArg = $Number; . $RunBlockSb                        # what the operator sees: state stale, not_fresh
    $AdoptBlock = $Blocks | Where-Object { $_.Text -match 'gateway-adopt' } | Select-Object -First 1
    $Number++
    $Refused = $false
    try { $BlockArg = $AdoptBlock; $NumberArg = $Number; . $RunBlockSb } catch { $Refused = $true }
    $Last = & { $Global:LASTEXITCODE }
    Say ("drill: the adopt of a stale report exit code {0}" -f $Last)
    if ($Last -eq 0) { throw 'a stale report was adopted' }
    # restart the reader (the runbook: make sure window 2 is running), wait for the new report, adopt again
    $RunBlock = $Blocks | Where-Object { $_.Tag -eq 'window RunArgs' } | Select-Object -First 1
    $Number++; $BlockArg = $RunBlock; $NumberArg = $Number; . $RunBlockSb
    $Null = Wait-NewReport $SeqAfterFirstAdopt 150
    $Number++; $BlockArg = $AdoptBlock; $NumberArg = $Number; . $RunBlockSb
    if ($global:LASTEXITCODE -ne 0) { throw 'the adopt after waiting for the next report did not work' }
    $PassBlock = $Blocks | Where-Object { $_.Text -match "PASSED" } | Select-Object -First 1
    $Number++; $BlockArg = $PassBlock; $NumberArg = $Number; . $RunBlockSb
    # the open-item drill: the money no lease accounts for goes up, so Leasewise opens an item; the adopt block must stop, print it and adopt nothing
    $AdoptedBefore = Adopted-Seq
    $SeqNow = Newest-Seq
    Say "rehearsal drill: the pretend gateway's unowned spending goes up; waiting for a report that shows it"
    & $Python $Pretend spend
    $Null = Wait-NewReport $SeqNow 150
    $OpenView = $Blocks | Where-Object { $_.Text -match 'open items=' } | Select-Object -First 1
    $Number++; $BlockArg = $OpenView; $NumberArg = $Number; . $RunBlockSb          # what the operator sees: transition_open, open items=1 (or more)
    $ExpectStop = $true
    $Number++; $BlockArg = $AdoptBlock; $NumberArg = $Number; . $RunBlockSb
    $ExpectStop = $false
    if (-not $BlockStopped) { throw 'the adopt block did not stop on an open item' }
    if ((Adopted-Seq) -ne $AdoptedBefore) { throw 'something was adopted while an open item existed' }
    if ($BlockOutput -notmatch 'open item \d+: \w+') { throw 'the adopt block did not print the open item' }
    Say "drill: the adopt block stopped on the open item, printed it, and adopted nothing (adopted report number stayed $AdoptedBefore)"
    # "if something goes wrong": the closed-word view of the reader
    $StatusBlock = $Blocks | Where-Object { $_.Text -match 'reporter status' } | Select-Object -First 1
    $Number++; $BlockArg = $StatusBlock; $NumberArg = $Number; . $RunBlockSb
    # step 11 (stop): end the two background processes by exact id, then run the runbook's own check that nothing remains
    Say "rehearsal: stopping the remaining windows through their process objects"
    Stop-Background
    Start-Sleep -Seconds 3
    $StopBlock = $Blocks | Where-Object { $_.Text -match 'Win32_Process' } | Select-Object -First 1
    $Number++; $BlockArg = $StopBlock; $NumberArg = $Number; . $RunBlockSb
    # before the rollback: where does the identity value live? (read it from the private file into memory only; never printed)
    $SecretLine = Get-Content -LiteralPath $ConfigPath | Where-Object { $_ -like 'identity = *' } | Select-Object -First 1
    $SecretValue = ($SecretLine -replace '^identity = "', '' -replace '"$', '')
    $Holders = @(Get-ChildItem -LiteralPath $Trial -Recurse -File -Force | Where-Object {
        [bool](Select-String -LiteralPath $_.FullName -SimpleMatch -Pattern $SecretValue -Quiet -ErrorAction SilentlyContinue) } | ForEach-Object { $_.Name })
    Say ("the identity value is inside these files of the trial folder only: " + ($Holders -join ', '))
    # step 12 (roll back)
    $RollBlock = $Blocks | Where-Object { $_.Text -match 'Remove-Item' } | Select-Object -First 1
    # drill: a rollback whose token does not match deletes nothing
    $RealToken = $Token; $Token = 'not-the-token'
    $ExpectStop = $true; $Number++; $BlockArg = $RollBlock; $NumberArg = $Number; . $RunBlockSb; $ExpectStop = $false
    if (-not $BlockStopped) { throw 'a rollback with the wrong token went on' }
    if (-not (Test-Path -LiteralPath $Trial)) { throw 'a rollback with the wrong token deleted the folder' }
    Say "drill: the rollback with a wrong token stopped and deleted nothing"
    $Token = $RealToken
    $Number++; $BlockArg = $RollBlock; $NumberArg = $Number; . $RunBlockSb
    if (Test-Path -LiteralPath $Trial) { throw 'the trial folder is still there' }
}
finally {
    Stop-Background
}

# --- how often did the reader ask the (pretend) gateway? ---------------------------------------------------------------------------------------
$CallLog = Join-Path $GatewayDir 'pretend-calls.log'
$Calls = @(Get-Content $CallLog | ForEach-Object { $Parts = $_ -split ' '; [pscustomobject]@{ At = [double]$Parts[0]; Kind = $Parts[1] } })
foreach ($Kind in 'report', 'receipts') {
    $Mine = @($Calls | Where-Object { $_.Kind -eq $Kind } | Sort-Object At)
    $Span = if ($Mine.Count -gt 1) { $Mine[-1].At - $Mine[0].At } else { 0 }
    $Busiest = 0
    foreach ($One in $Mine) { $InMinute = @($Mine | Where-Object { $_.At -ge $One.At -and $_.At -lt $One.At + 60 }).Count; if ($InMinute -gt $Busiest) { $Busiest = $InMinute } }
    Say ("gateway calls: {0,-8} {1} calls over {2:N0} s, at most {3} in any one minute" -f $Kind, $Mine.Count, $Span, $Busiest)
}

# --- the places the run could write, after ---------------------------------------------------------------------------------------------------
$After = [ordered]@{
    'fake LOCALAPPDATA'  = Names-Under $env:LOCALAPPDATA
    'fake APPDATA'       = Names-Under $env:APPDATA
    'fake TEMP'          = Names-Under $env:TEMP
    'real LOCALAPPDATA'  = Names-Under $RealLocal
    'real APPDATA'       = Names-Under $RealRoaming
    'real TEMP (ours)'   = @(Names-Under $RealTemp | Where-Object { $_ -like 'quota-*' -or $_ -like 'leasewise*' -or $_ -like 'tmp*trial*' })
    'repository status'  = @(git -C $Repo status --porcelain)
}
$Left = 0
foreach ($Place in $Before.Keys) {
    $Gone = @($Before[$Place] | Where-Object { $After[$Place] -notcontains $_ })
    $New = @($After[$Place] | Where-Object { $Before[$Place] -notcontains $_ })
    Say ("place [{0}] before={1} after={2} new=[{3}] gone=[{4}]" -f $Place, @($Before[$Place]).Count, @($After[$Place]).Count, ($New -join ','), ($Gone -join ','))
    if ($Place -like 'fake*' -or $Place -like 'repository*') { $Left += $New.Count }
}
if ($Left -ne 0) { throw 'something was left behind' }

# --- the secret must not be anywhere outside the trial folder (the log, the window outputs, everything under the rehearsal root) ---------------------
$Leaks = @(Get-ChildItem -LiteralPath $Root -Recurse -File -Force | Where-Object {
    [bool](Select-String -LiteralPath $_.FullName -SimpleMatch -Pattern $SecretValue -Quiet -ErrorAction SilentlyContinue) } | ForEach-Object { $_.FullName })
Say ("files outside the trial folder that contain the identity value: {0}" -f $Leaks.Count)
if ($Leaks.Count -ne 0) { throw 'the identity value appears outside the private file' }

$Total = [math]::Round(((Get-Date) - $Started).TotalSeconds, 0)
Say ""
Say "timings:"
$Timings | ForEach-Object { Say ("  block {0,2}  {1,-24} {2,7} s" -f $_.Block, $_.Tag, $_.Seconds) }
Check-Time
Say ("REHEARSAL PASSED in {0} seconds (bound: {1} minutes)" -f $Total, $MaxMinutes)
