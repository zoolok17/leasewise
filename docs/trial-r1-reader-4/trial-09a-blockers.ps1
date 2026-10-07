# Step 9, first block: look at what blocks adoption (the runbook's block, unchanged). Also the block for "If an adopt is refused as too old".
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 9a: what blocks adoption'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
'state={0}  blockers=[{1}]  open items={2}' -f $Gateway.evidence.state, ($Gateway.evidence.adopt_blockers -join ','), @($Gateway.evidence.open_transitions).Count
