# Step 9, second block: adopt the newest report (the runbook's block, unchanged). Only when there are no open items; within 120 s of the report.
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 9b: adopt the newest report'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
$OpenItems = @($Gateway.evidence.open_transitions)
if ($OpenItems.Count -gt 0) {
    $OpenItems | ForEach-Object { 'open item {0}: {1}' -f $_.id, $_.kind }
    throw 'There are open items. Do not adopt. Tell the lead what is listed above.'
}
$New = $Gateway.evidence.latest
'adopting report number {0}, {1} seconds old' -f $New.seq, [math]::Round($New.age_ms / 1000)
& $Py -m quota_balancer --db $Store gateway-adopt --gateway $GatewayAlias --evidence $New.event_id
"exit code $LASTEXITCODE"
