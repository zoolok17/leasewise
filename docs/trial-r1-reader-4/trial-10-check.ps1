# Step 10: check that it passed (the runbook's block, unchanged). Run it when the gateway has been quiet for a minute.
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 10: check that it passed'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
# The month the reader used: the UTC month of its newest report. The gateway's figure for THAT month; 0 when the gateway has no row for it.
$Month = [DateTimeOffset]::FromUnixTimeMilliseconds([int64]$Gateway.evidence.latest.observed_at).UtcDateTime.ToString('yyyy-MM')
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
if ([DateTime]::UtcNow.ToString('yyyy-MM') -ne $Month) { throw "The month changed between the two readings ($Month). Wait for the next report and run this block again." }
$Row = @($Rep.periods | Where-Object { $_.period -eq $Month })
$Settled = if ($Row.Count -eq 1) { $Row[0].committed_micro_eur } else { 0 }
'state={0}  adopted report number={1}  month={2}' -f $Gateway.evidence.state, $Gateway.evidence.adopted.seq, $Month
'Leasewise settled={0}  gateway settled={1}' -f $Gateway.evidence.latest.settled_micro_eur, $Settled
if ($Gateway.evidence.state -ne 'usable') { throw 'status does not say usable' }
if ($Gateway.evidence.latest.settled_micro_eur -ne $Settled) { throw 'The settled totals differ.' }
'PASSED'
