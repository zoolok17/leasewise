# Step 13: the done check. Run it after the real turns of a Claude seat and of the Codex seat. For up to ten minutes (every 20 seconds) it asks Leasewise's store
# and the reporter's status, and it FAILS CLOSED: it passes only if ALL of these hold, and otherwise it says which one failed and that nothing was changed.
#   * the baseline of step 12 exists and has exactly the fields step 12 writes;
#   * each of the two accounts has an accepted figure observed after the baseline's clock time AND after that account's newest figure at that time;
#   * the reporter's status command succeeds and answers with a JSON object that has the readings part configured, exactly the two expected sources
#     (provider and account) and a last-scan time that can be read;
#   * the last scan lies between 180 seconds ago and 30 seconds ahead of this machine's clock (the 30 seconds are the clock tolerance);
#   * every other command succeeded and its answer could be read; and there is no operator line, no failed reading and no capture fault.
# If this machine's clock is more than 30 seconds earlier than the baseline's time, it stops: the clock moved back since step 12. That is checked at the start and
# again, with a fresh reading of the clock, at the moment it decides; the scan age is measured against that same fresh reading.
# It prints aliases and numbers only, never an account text.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 13: the done check'
$BaselineFile = Join-Path $LiveRoot 'baseline.json'
if (-not (Test-Path -LiteralPath $BaselineFile -PathType Leaf)) { throw 'No baseline was recorded. Run step 12 first. Nothing was changed.' }
function Get-Now { [DateTimeOffset]::UtcNow }                     # every reading of the clock goes through here
$ClockBack = 'The clock moved back since step 12. Nothing was changed. Run step 12 again with -Again, then ask the lead for the two turns again.'
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
