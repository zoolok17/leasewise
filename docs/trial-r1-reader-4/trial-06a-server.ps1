# Step 6, window 1: the Leasewise server (the runbook's block, unchanged). It prints nothing while it works. Stop it with Ctrl-C (step 11).
$ErrorActionPreference = 'Stop'
# The same entry guard as the shared start: a link in the way, or a different trial folder, stops here.
. (Join-Path $PSScriptRoot 'trial-guard.ps1')
$N = Get-Content -LiteralPath "$env:LOCALAPPDATA\leasewise-trial\names.json" -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $N.Src
$env:PYTHONDONTWRITEBYTECODE = '1'
$ServeArgs = @('-m','quota_balancer','--db',$N.Store,'serve','--host','127.0.0.1','--port','8443')
& $N.Py @ServeArgs
