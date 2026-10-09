# Step 5 (operator step): the two accounts and this machine as an allowed reporter for both. Leasewise lets a machine report an account's
# readings only when one of the machine's seats belongs to a project that has the account. The one seat below ("readings", shadow) exists
# only for that: it grants and denies nothing. The block replaces Leasewise's inventory, which is empty at this point.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 5: the two accounts and the allowed reporter'
$Question = @'
import sqlite3, sys
conn = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
print(conn.execute("SELECT (SELECT COUNT(*) FROM accounts) + (SELECT COUNT(*) FROM machines)").fetchone()[0])
'@
$Have = & $Py @PyFlags -c $Question $Store                 # read-only: how many accounts and machines the store already has
if ($LASTEXITCODE -ne 0 -or "$Have" -cnotmatch '^[0-9]+$') { throw 'The store could not be read. Nothing was changed. Tell the lead.' }
if ([int]"$Have" -ne 0) { throw 'Step 5 was already done. Nothing was changed. Go on with step 6.' }
$Inventory = @"
{"version":2,
 "accounts":[{"alias":"$ClaudeAlias","vendor":"anthropic","kind":"subscription","budget_micro_eur":null},
             {"alias":"$CodexAlias","vendor":"openai","kind":"subscription","budget_micro_eur":null}],
 "machines":[{"alias":"$MachineAlias"}],
 "gateways":[],
 "projects":[{"alias":"$ProjectAlias"}],
 "teams":[{"project":"$ProjectAlias","alias":"team"}],
 "project_accounts":[{"project":"$ProjectAlias","account":"$ClaudeAlias"},{"project":"$ProjectAlias","account":"$CodexAlias"}],
 "seats":[{"team":{"project":"$ProjectAlias","team":"team"},"machine":"$MachineAlias","alias":"readings","role":"main","enforcement":"shadow"}],
 "models":[], "profiles":[], "limits":[]}
"@
$InventoryFile = Join-Path $LiveRoot 'inventory.json'    # inside the private folder: new files there inherit its private list
Set-Content -LiteralPath $InventoryFile -Value $Inventory -Encoding ascii
& $Py @PyFlags -m quota_balancer --db $Store config-import --file $InventoryFile
if ($LASTEXITCODE -ne 0) { throw 'config-import failed. Nothing else was changed. Tell the lead.' }
& $Py @PyFlags -m quota_balancer --db $Store policy-ids --json | Out-File -Encoding ascii -LiteralPath $PolicyIds
if ($LASTEXITCODE -ne 0) { throw 'policy-ids failed' }
(Get-Content -LiteralPath $PolicyIds -Raw | ConvertFrom-Json).accounts | ForEach-Object { $_.alias }
