# Step 12: record the baseline. Run it once the reporter (step 11) has run for a minute, and BEFORE you ask the lead for the two real turns.
# For each account it records the newest figure Leasewise has accepted so far (or "none yet"). The done check (step 13) then passes only for figures
# that are newer than this, so old figures cannot make it pass. It only reads the store and writes one small file; it prints aliases and times only.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 12: record the baseline'
$BaselineFile = Join-Path $LiveRoot 'baseline.json'
if (Test-Path -LiteralPath $BaselineFile) { throw 'A baseline was already recorded. Nothing was changed. If the real turns failed and you must start over, tell the lead.' }
$Question = @'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
for alias in sys.argv[2:]:
    newest = conn.execute("SELECT MAX(w.observed_at) FROM windows w JOIN accounts a ON a.id = w.account_id WHERE a.alias = ?", (alias,)).fetchone()[0]
    print(alias + " " + str(int(newest or 0)))
'@
$Lines = @(& $Py -c $Question $Store $ClaudeAlias $CodexAlias)
if ($LASTEXITCODE -ne 0 -or $Lines.Count -ne 2) { throw 'The store could not be read. Nothing was changed. Tell the lead.' }
$Base = [ordered]@{}
foreach ($Line in $Lines) { $Parts = $Line -split ' '; $Base[$Parts[0]] = [int64]$Parts[1] }
if ((@($Base.Keys) -join ',') -cne ($ClaudeAlias + ',' + $CodexAlias)) { throw 'The store did not answer for exactly the two accounts. Nothing was changed. Tell the lead.' }
[IO.File]::WriteAllText($BaselineFile, ($Base | ConvertTo-Json), $Utf8)
foreach ($Alias in $Base.Keys) {
    if ($Base[$Alias] -eq 0) { '{0}: no accepted figure yet' -f $Alias }
    else { '{0}: newest accepted figure so far was observed at {1}' -f $Alias, [DateTimeOffset]::FromUnixTimeMilliseconds($Base[$Alias]).UtcDateTime.ToString('yyyy-MM-dd HH:mm:ss') }
}
'baseline recorded. Now ask the lead for one real turn of a Claude seat and one of the Codex seat, then run step 13.'
