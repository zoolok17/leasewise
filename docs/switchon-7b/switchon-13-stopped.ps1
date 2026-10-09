# Step 13: after Ctrl-C in window 2, then window 1, check that no Leasewise process remains.
# It prints nothing when everything has stopped (this script's own pwsh may show itself: ignore that).
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 13: no Leasewise process left'
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ('*' + [WildcardPattern]::Escape($LiveRoot) + '*') } | Select-Object ProcessId, Name
