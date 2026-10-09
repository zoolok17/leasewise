# Step 9: check the account table. `reporter status` reads the settings strictly (a wrong table is refused with a fixed sentence that shows no
# value) and lists each row by provider and Leasewise alias only. It never prints an account text.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
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
