# Step 4: set up the store (the runbook's block, unchanged).
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 4: set up the store'
$Inventory = @"
{"version":2,
 "accounts":[{"alias":"$AccountAlias","vendor":"ovh","kind":"metered","budget_micro_eur":$BudgetMicroEur}],
 "machines":[{"alias":"$MachineAlias"}],
 "gateways":[{"alias":"$GatewayAlias","account":"$AccountAlias","machine":"$MachineAlias","share_micro_eur":$ShareMicroEur,"period":"utc_month"}],
 "projects":[{"alias":"trial"}],
 "teams":[{"project":"trial","alias":"team"}],
 "project_accounts":[{"project":"trial","account":"$AccountAlias"}],
 "seats":[], "models":[], "profiles":[], "limits":[]}
"@
$InventoryFile = Join-Path $Trial 'inventory.json'     # inside the private folder: new files there inherit its private list
Set-Content -LiteralPath $InventoryFile -Value $Inventory -Encoding ascii
& $Py -m quota_balancer --db $Store init
if ($LASTEXITCODE -ne 0) { throw 'init failed' }
& $Py -m quota_balancer --db $Store config-import --file $InventoryFile
if ($LASTEXITCODE -ne 0) { throw 'config-import failed' }
& $Py -m quota_balancer --db $Store policy-ids --json | Out-File -Encoding ascii -LiteralPath $PolicyIds
if ($LASTEXITCODE -ne 0) { throw 'policy-ids failed' }
(Get-Content -LiteralPath $PolicyIds -Raw | ConvertFrom-Json).gateways | ForEach-Object { $_.alias }
