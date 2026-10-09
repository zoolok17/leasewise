# Step 14: after you pressed Ctrl-C in window 2, then in window 1, check that nothing is left. This script LISTS what is still running from the live folder;
# it stops nothing. You stop a window yourself with Ctrl-C in that window. It prints nothing but the banner when everything has stopped
# (this script's own pwsh may show itself: ignore that).
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 14: what is still running'
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ('*' + [WildcardPattern]::Escape($LiveRoot) + '*') } | Select-Object ProcessId, Name
