# The rehearsal of docs/TRIAL-R1-READER-4.md (card R1-READER-4-prep). Not a test: the name does not start with `test_`, so `discover` never collects it.
#
# It follows the runbook LITERALLY: it reads the runbook, takes every PowerShell block that carries a `<!-- rehearse: ... -->` tag and runs that exact
# text, in order, in one session. Only three things differ from what the operator does:
#   * `rehearse: replace`  the block where the operator types the names of their machine is replaced by the names of this rehearsal (a pretend
#                          gateway program instead of the real one, a scratch folder instead of the real one);
#   * `rehearse: window`   the two blocks the operator runs in separate windows are started as hidden background processes (by Start-Process, so the
#                          process ids are known and the processes can be ended by exact id), with a bounded lifetime;
#   * between the runbook's own steps the rehearsal moves the pretend gateway forward, and for the drill it stops the reader and waits.
# Nothing real is touched: no gateway, no agenttalk, no provider. Run it with Windows PowerShell 5.1 (the desktop's default) or PowerShell 7.
#
#   powershell.exe -NoProfile -File tests\trial\rehearse.ps1 -Root <fresh scratch folder> -Repo <leasewise checkout> -Python <python.exe>

param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$Runbook = '',
    [int]$MaxMinutes = 20
)

$ErrorActionPreference = 'Stop'
$ExpectStop = $false
$Started = Get-Date
if (-not $Runbook) { $Runbook = Join-Path $Repo 'docs\TRIAL-R1-READER-4.md' }
if (Test-Path $Root) { throw "The rehearsal folder must not exist yet: $Root" }
New-Item -ItemType Directory -Force $Root | Out-Null
$Log = Join-Path $Root 'rehearsal.log'
$Timings = New-Object System.Collections.ArrayList
$Background = New-Object System.Collections.ArrayList        # the process ids this script started, to end by exact id

function Say([string]$Text) { Write-Host $Text; Add-Content -Path $Log -Value $Text -Encoding ascii }

function Names-Under([string]$Path) {
    if (-not (Test-Path $Path)) { return @() }
    return @(Get-ChildItem -LiteralPath $Path -Force -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name | Sort-Object)
}

# --- the places the run could write, before -------------------------------------------------------------------------------------------------
# The rehearsal gives the run its OWN application-data and temp folders under the scratch root, so a stray write cannot hide among other programs'
# files. The real top-level folders are listed too, before and after.
$RealLocal = $env:LOCALAPPDATA; $RealRoaming = $env:APPDATA; $RealTemp = $env:TEMP
$Fake = Join-Path $Root 'home'
foreach ($Sub in 'Local', 'Roaming', 'Temp') { New-Item -ItemType Directory -Force (Join-Path $Fake $Sub) | Out-Null }
$env:LOCALAPPDATA = Join-Path $Fake 'Local'; $env:APPDATA = Join-Path $Fake 'Roaming'; $env:TEMP = Join-Path $Fake 'Temp'; $env:TMP = $env:TEMP
$Before = [ordered]@{
    'fake LOCALAPPDATA'  = Names-Under $env:LOCALAPPDATA
    'fake APPDATA'       = Names-Under $env:APPDATA
    'fake TEMP'          = Names-Under $env:TEMP
    'real LOCALAPPDATA'  = Names-Under $RealLocal
    'real APPDATA'       = Names-Under $RealRoaming
    'real TEMP (ours)'   = @(Names-Under $RealTemp | Where-Object { $_ -like 'quota-*' -or $_ -like 'leasewise*' -or $_ -like 'tmp*trial*' })
    'repository status'  = @(git -C $Repo status --porcelain)
}

# --- the made-up machine ----------------------------------------------------------------------------------------------------------------------
$GatewayDir = Join-Path $Root 'pretend-gateway'
New-Item -ItemType Directory -Force $GatewayDir | Out-Null
Copy-Item (Join-Path $Repo 'tests\trial\pretend_gateway.py') $GatewayDir
$Pretend = Join-Path $GatewayDir 'pretend_gateway.py'
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
    $Watch = [Diagnostics.Stopwatch]::StartNew()
    $Kind = ($BlockArg.Tag -split ' ')[0]
    Say ("--- block {0} ({1}) ---" -f $NumberArg, $BlockArg.Tag)
    switch ($Kind) {
        'replace' { $BlockText = $Preamble }
        'window' {
            $BlockLines = $BlockArg.Text -split "`n"
            $LastLine = $BlockLines[-1]
            if ($LastLine -notmatch '^& (\$\w+) @(\w+)$') { throw "window block must end with: & `$Exe @Args  (got: $LastLine)" }
            $ExeName = $Matches[1].TrimStart('$'); $ArgName = $Matches[2]
            $BlockText = ($BlockLines[0..($BlockLines.Count - 2)] -join "`n")
        }
        default { $BlockText = $BlockArg.Text }
    }
    $ErrorActionPreference = 'Continue'                  # the operator's window is not strict either; the blocks check for themselves and throw
    $Collected = New-Object System.Collections.ArrayList
    $BlockStopped = $false
    try { . { Invoke-Expression $BlockText } 2>&1 | ForEach-Object { [void]$Collected.Add(($_ | Out-String).TrimEnd()) } }
    catch { $BlockStopped = $true; [void]$Collected.Add('STOPPED BY THE BLOCK: ' + $_.Exception.Message) }
    $BlockOutput = ($Collected -join "`n") + "`n"
    $ErrorActionPreference = 'Stop'
    if ($Kind -eq 'window') {
        $WindowExe = (Get-Variable $ExeName -ValueOnly); $WindowArgs = @(Get-Variable $ArgName -ValueOnly)
        $WindowLine = ($WindowArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { [string]$_ } }) -join ' '
        $OutFile = Join-Path $Root ("window-{0}.out.txt" -f $ArgName); $ErrFile = Join-Path $Root ("window-{0}.err.txt" -f $ArgName)
        $Proc = Start-Process -FilePath $WindowExe -ArgumentList $WindowLine -PassThru -WindowStyle Hidden -WorkingDirectory $Src -RedirectStandardOutput $OutFile -RedirectStandardError $ErrFile
        [void]$Background.Add($Proc.Id)
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

function Wait-NewReport([int]$Than, [int]$Seconds) {
    $Deadline = (Get-Date).AddSeconds($Seconds)
    do { Start-Sleep -Seconds 5; $Now = Newest-Seq } until ($Now -gt $Than -or (Get-Date) -gt $Deadline)
    if ($Now -le $Than) { throw "no new report within $Seconds seconds" }
    return $Now
}

function Stop-Background {
    foreach ($Id in @($Background)) {
        $P = Get-Process -Id $Id -ErrorAction SilentlyContinue
        if ($P) { Stop-Process -Id $Id -Force -ErrorAction SilentlyContinue }
    }
    $Background.Clear()
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
    }
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
    Stop-Process -Id $Proc_RunArgs.Id -Force
    Start-Sleep -Seconds 135
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
    Say "rehearsal: stopping the two windows by exact process id"
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
    $Number++; $BlockArg = $RollBlock; $NumberArg = $Number; . $RunBlockSb
    if (Test-Path $Trial) { throw 'the trial folder is still there' }
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
Say ("REHEARSAL PASSED in {0} seconds" -f $Total)
