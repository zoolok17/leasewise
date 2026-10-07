# Step 6, window 2: the reader (the runbook's block, unchanged). Start it a few seconds after window 1. It prints one line per cycle,
# about once a minute, like {"blocked": null, "journal": "ok", "new_events": 0, "sent": 1}. Stop it with Ctrl-C (step 11).
$ErrorActionPreference = 'Stop'
$N = Get-Content -LiteralPath "$env:LOCALAPPDATA\leasewise-trial\names.json" -Raw -Encoding UTF8 | ConvertFrom-Json
Set-Location -LiteralPath $N.Src
$env:PYTHONDONTWRITEBYTECODE = '1'
$RunArgs = @('-m','quota_balancer','reporter','run','--config',$N.ConfigPath)
& $N.Py @RunArgs
