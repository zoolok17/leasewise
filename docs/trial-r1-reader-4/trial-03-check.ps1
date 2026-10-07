# Step 3: check Python and the gateway program (the runbook's block, unchanged). Both checks only read.
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 3: check Python and the gateway program'
& $Py -c 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python is older than 3.11.' }
$Rep = (& $GatewayExe @GatewayPre gateway report --json) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'The gateway program could not print its report.' }
'{0} {1} {2}' -f $Rep.gateway_report_version, $Rep.child_cap_ready, $Rep.child_receipt_report_version
