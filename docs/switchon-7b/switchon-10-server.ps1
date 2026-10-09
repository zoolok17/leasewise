# Step 10, window 1: the Leasewise server. It listens on this machine only (127.0.0.1) and prints nothing while it works.
# It first looks at who listens on the port: if anything does, it starts nothing (this also refuses a second step 10 while window 1 is open).
# Stop it with Ctrl-C in its window (step 14 then shows what is still running). After a restart of the machine, run this script again; nothing else needs repeating.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
$Holders = @(Get-PortListeners $PortNumber)
if ($Holders.Count -gt 0) {
    throw ('Something on this machine already listens on port {0} (process {1}, program {2}). Nothing was started. If it is window 1 from earlier, go back to that window; otherwise tell the lead.' -f $PortNumber, $Holders[0].ProcessId, $Holders[0].Name)
}
$ServeArgs = @('-m', 'quota_balancer', '--db', $Store, 'serve', '--host', '127.0.0.1', '--port', [string]$PortNumber)
& $Py @PyFlags @ServeArgs
