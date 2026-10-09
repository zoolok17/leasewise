# Step 14: after you pressed Ctrl-C in window 2, then in window 1, check that nothing is left. This script reads the list of ALL processes on this machine and
# shows only those whose command line names the live folder (process number and program name); it stops nothing. You stop a window yourself with Ctrl-C in that
# window. It prints nothing but the banner when everything has stopped.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 14: what is still running'
Get-LiveProcesses | Select-Object ProcessId, Name
