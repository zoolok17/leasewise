# Step 10, window 1: the Leasewise server. It listens on this machine only (127.0.0.1) and prints nothing while it works.
# Stop it with Ctrl-C in its window (step 14 then shows what is still running). After a restart of the machine, run this script again; nothing else needs repeating.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
$ServeArgs = @('-m', 'quota_balancer', '--db', $Store, 'serve', '--host', '127.0.0.1', '--port', [string]$PortNumber)
& $Py @ServeArgs
