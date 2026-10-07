# Step 7: wait for the first report (the runbook's block, unchanged). It asks Leasewise's status every few seconds for up to two minutes.
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 7: wait for the first report'
$Deadline = (Get-Date).AddSeconds(120)
do {
    Start-Sleep -Seconds 5
    $Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
    $Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
} until ($Gateway.evidence -or (Get-Date) -gt $Deadline)
if (-not $Gateway.evidence) { throw 'No report arrived in two minutes. See "If something goes wrong".' }
'state={0} latest report number={1}' -f $Gateway.evidence.state, $Gateway.evidence.latest.seq
