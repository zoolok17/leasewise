# Step 13: the done check. Run it after the real turns of a Claude seat and of the Codex seat. For up to ten minutes (every 20 seconds) it asks Leasewise's store
# and the reporter's status, and it passes only if ALL of these hold: every command it runs succeeded; the reporter scanned within the last three minutes; the
# settings list exactly the two expected accounts; each account has an accepted figure NEWER than the baseline of step 12; and the status shows no operator
# warning, no failed reading and no capture fault. It prints aliases and numbers only, never an account text.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 13: the done check'
$BaselineFile = Join-Path $LiveRoot 'baseline.json'
if (-not (Test-Path -LiteralPath $BaselineFile)) { throw 'No baseline was recorded. Run step 12 first. Nothing was changed.' }
$Base = Get-Content -LiteralPath $BaselineFile -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($Alias in $ClaudeAlias, $CodexAlias) { if ($null -eq $Base.$Alias) { throw 'The baseline is not the file step 12 made. Nothing was changed. Tell the lead.' } }
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
    else { foreach ($Line in $Lines) { $Parts = $Line -split ' '; $Newest[$Parts[0]] = [int64]$Parts[1] } }
    $Json = & $Py -m quota_balancer reporter status --config $ConfigPath --json
    if ($LASTEXITCODE -ne 0) { $Problems += 'The reporter status could not be read (the reporter may never have run).'; $Status = $null }
    else { $Status = $Json | ConvertFrom-Json }
    if ($Status) {
        $Readings = $Status.readings
        $Have = @($Readings.sources | ForEach-Object { '{0} -> {1}' -f $_.provider, $_.account } | Sort-Object)
        $Want = @(('claude -> ' + $ClaudeAlias), ('codex -> ' + $CodexAlias) | Sort-Object)
        if (-not $Readings.configured -or ($Have -join ';') -cne ($Want -join ';')) { $Problems += 'The settings do not list exactly the two expected accounts.' }
        if (-not $Readings.last_scan) { $Problems += 'The reporter has not scanned yet.' }
        elseif (([DateTimeOffset]::UtcNow - $(if ($Readings.last_scan -is [datetime]) { [DateTimeOffset]$Readings.last_scan } else { [DateTimeOffset]::Parse([string]$Readings.last_scan, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal) })).TotalSeconds -gt 180) { $Problems += 'The reporter has not scanned in the last three minutes (is window 2 running?).' }
        if ($Status.operator_actions.Count -ne 0 -or $Readings.rows.failed_permanent -ne 0 -or $Readings.capture_fault.count -ne 0) { $Problems += 'The reporter shows a warning.' }
    }
    foreach ($Alias in $ClaudeAlias, $CodexAlias) {
        if (-not $Newest.ContainsKey($Alias) -or $Newest[$Alias] -le [int64]$Base.$Alias) { $Problems += ('{0}: no accepted figure newer than the baseline yet.' -f $Alias) }
    }
    if ($Problems.Count -gt 0 -and (Get-Date) -lt $Deadline) { 'waiting: ' + ($Problems -join ' '); Start-Sleep -Seconds 20 }
} until ($Problems.Count -eq 0 -or (Get-Date) -ge $Deadline)
foreach ($Alias in $ClaudeAlias, $CodexAlias) {
    '{0}: newest accepted figure {1}; baseline {2}' -f $Alias, $Newest[$Alias], $Base.$Alias
}
if ($Status) { 'reporter: rows sent={0} waiting={1} failed={2}; capture faults={3}' -f $Status.readings.rows.sent, $Status.readings.rows.pending, $Status.readings.rows.failed_permanent, $Status.readings.capture_fault.count }
if ($Status) { foreach ($Line in $Status.operator_actions) { '!! ' + $Line } }
if ($Problems.Count -gt 0) { throw ('Not done: ' + ($Problems -join ' ') + ' See "If something goes wrong".') }
'PASSED'
