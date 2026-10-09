# Step 11, window 2: the reporter. Start it a few seconds after window 1. It prints one line per cycle, like
# {"blocked": null, "journal": "ok", "new_events": 0, "sent": 2}. Stop it with Ctrl-C in its window (step 14 then shows what is still running).
# Before it starts anything it checks that the only program listening on the port is window 1's Leasewise server (otherwise the identity value would be sent to a
# stranger) and that no reporter for these settings is running already. After a restart of the machine, run step 10 and then this script again; the reporter's
# state is kept and nothing is repeated.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
Assert-OurServerListens
Assert-NoReporterRunning
$RunArgs = @('-m', 'quota_balancer', 'reporter', 'run', '--config', $ConfigPath)
& $Py @PyFlags @RunArgs
