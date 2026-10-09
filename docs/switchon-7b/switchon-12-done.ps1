# Step 12: the done check. Run it after a real turn of a Claude seat and of the Codex seat. It asks the reporter for its status and Leasewise's
# store which accounts have an accepted figure, for up to ten minutes. It prints aliases and numbers only, never an account text.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 12: the done check'
$Question = @'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
for alias in sys.argv[2:]:
    n = conn.execute("SELECT COUNT(*) FROM windows w JOIN accounts a ON a.id = w.account_id WHERE a.alias = ?", (alias,)).fetchone()[0]
    print(alias + " " + str(n))
'@
$Deadline = (Get-Date).AddMinutes(10)
do {
    $Counts = @{}
    foreach ($Line in (& $Py -c $Question $Store $ClaudeAlias $CodexAlias)) { $Parts = $Line -split ' '; $Counts[$Parts[0]] = [int]$Parts[1] }
    $Status = (& $Py -m quota_balancer reporter status --config $ConfigPath --json) | ConvertFrom-Json
    $Ready = ($Counts[$ClaudeAlias] -gt 0) -and ($Counts[$CodexAlias] -gt 0)
    if (-not $Ready -and (Get-Date) -lt $Deadline) { 'waiting: {0} window(s) for {1}, {2} for {3}' -f $Counts[$ClaudeAlias], $ClaudeAlias, $Counts[$CodexAlias], $CodexAlias; Start-Sleep -Seconds 20 }
} until ($Ready -or (Get-Date) -ge $Deadline)
$Readings = $Status.readings
'{0}: {1} window(s) accepted by Leasewise' -f $ClaudeAlias, $Counts[$ClaudeAlias]
'{0}: {1} window(s) accepted by Leasewise' -f $CodexAlias, $Counts[$CodexAlias]
'reporter: rows sent={0} waiting={1} failed={2}; capture faults={3}' -f $Readings.rows.sent, $Readings.rows.pending, $Readings.rows.failed_permanent, $Readings.capture_fault.count
foreach ($Line in $Status.operator_actions) { '!! ' + $Line }
if (-not $Ready) { throw 'An account has no accepted figure yet. Run a turn in that seat and run this step again, or see "If something goes wrong".' }
if ($Status.operator_actions.Count -ne 0 -or $Readings.rows.failed_permanent -ne 0 -or $Readings.capture_fault.count -ne 0) { throw 'The reporter shows a warning (above). Not done. See "If something goes wrong".' }
'PASSED'
