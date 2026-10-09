# Step 0: check that scripts run on this machine. It changes nothing: it only prints the PowerShell version and a short sentence.
# If the machine's policy refuses it, stop and tell the lead (do not change the policy).
'PowerShell ' + $PSVersionTable.PSVersion.ToString()
'scripts run here'
