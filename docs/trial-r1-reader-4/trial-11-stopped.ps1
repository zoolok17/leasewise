# Step 11: after Ctrl-C in window 2, then window 1, check that no Leasewise process remains (the runbook's block, unchanged).
# It prints nothing when everything has stopped (this script's own pwsh may show itself: ignore that).
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 11: no Leasewise process left'
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ('*' + [WildcardPattern]::Escape($Trial) + '*') } | Select-Object ProcessId, Name
