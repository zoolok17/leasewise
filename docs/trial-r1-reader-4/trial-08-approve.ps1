# Step 8: compare the two fingerprints, then approve (the runbook's block). In the pasted runbook $Gateway came from step 7's last
# status reading in the same window; this script reads status once first, with step 7's own two lines.
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 8: compare the two fingerprints, then approve'
$Status = (& $Py -m quota_balancer --db $Store status --json) | ConvertFrom-Json
$Gateway = $Status.gateways | Where-Object { $_.alias -eq $GatewayAlias }
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
$Held = $Gateway.report.policy_hashes
'price equal: {0}   cap equal: {1}' -f ($Rep.policy_hash -eq $Held.price), ($Rep.child_cap_policy_hash -eq $Held.child_cap)
if (($Rep.policy_hash -ne $Held.price) -or ($Rep.child_cap_policy_hash -ne $Held.child_cap)) { throw 'The fingerprints differ. Do not approve.' }
& $Py -m quota_balancer --db $Store gateway-approve --gateway $GatewayAlias --price-hash $Rep.policy_hash --child-cap-hash $Rep.child_cap_policy_hash
if ($LASTEXITCODE -ne 0) { throw 'gateway-approve failed' }
