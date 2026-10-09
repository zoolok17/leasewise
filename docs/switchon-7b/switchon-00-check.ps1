# Step 0: check that scripts run on this machine. It changes nothing: it only prints the PowerShell version and a short sentence.
# If the machine's policy refuses it, stop and tell the lead (do not change the policy). It also refuses a PowerShell older than 7.3, which passes quoted
# arguments to other programs differently.
'PowerShell ' + $PSVersionTable.PSVersion.ToString()
$Version = $PSVersionTable.PSVersion
if ([version]('{0}.{1}' -f $Version.Major, $Version.Minor) -lt [version]'7.3') { throw "This needs PowerShell 7.3 or newer; this window runs $($Version.ToString()). Nothing was changed." }
'scripts run here'
