param([switch]$Again)    # only for the clock case in the runbook: it replaces the baseline that is already there
# Step 12: record the baseline. Run it once the reporter (step 11) has run for a minute, and BEFORE you ask the lead for the two real turns.
# It writes one small file with three things: a schema number, this machine's clock (UTC, in milliseconds) at this moment, and for each account the newest
# figure Leasewise has already accepted (or none). The done check (step 13) passes only for figures observed after BOTH, so neither old figures nor figures
# that were observed before this step but delivered after it can make it pass. It only reads the store and writes that file; it prints aliases and times only.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
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
