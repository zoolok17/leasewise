# Step 10, window 1: the Leasewise server. It listens on this machine only (127.0.0.1) and prints nothing while it works.
# It first looks at who listens on the port: if anything does, it starts nothing (this also refuses a second step 10 while window 1 is open).
# It starts the server itself and writes server.json (which program it started) BEFORE it waits, so that step 11 can accept exactly that program and nothing else.
# If the record cannot be saved it stops the server it just started, so no unrecorded server is left running.
# Stop it with Ctrl-C in its window (step 14 then shows what is still running). After a restart of the machine, run this script again; nothing else needs repeating.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
$Holders = @(Get-PortListeners $PortNumber)
if ($Holders.Count -gt 0) {
    throw ('Something on this machine already listens on port {0} (process {1}, program {2}). Nothing was started. If it is window 1 from earlier, go back to that window; otherwise tell the lead.' -f $PortNumber, $Holders[0].ProcessId, $Holders[0].Name)
}
$RecordFile = Join-Path $LiveRoot 'server.json'
if ((Test-Path -LiteralPath $RecordFile) -and -not (Test-Path -LiteralPath $RecordFile -PathType Leaf)) { throw "$RecordFile is not a file. Nothing was started. Tell the lead." }
function Save-ServerRecord { param([string]$Text) [IO.File]::WriteAllText($RecordFile, $Text, $Utf8) }
$Server = $null
$Saved = $false
try {
    $Start = New-Object Diagnostics.ProcessStartInfo
    $Start.FileName = $Py
    foreach ($Word in @($PyFlags) + @(Get-ServeArgs)) { [void]$Start.ArgumentList.Add($Word) }      # one word at a time: Windows quoting is done by .NET, never by joining strings
    $Start.UseShellExecute = $false                                                                  # no redirection: the server shares this window, as before
    $Start.WorkingDirectory = $Src
    $Server = [Diagnostics.Process]::Start($Start)
    $Record = [ordered]@{ schema = 1; pid = $Server.Id; started_utc_ticks = $Server.StartTime.ToUniversalTime().Ticks; port = $PortNumber }
    try { Save-ServerRecord ($Record | ConvertTo-Json) } catch { throw 'The server was started but its record could not be saved, so it was stopped again. Nothing else was changed. Tell the lead.' }
    $Saved = $true
    while (-not $Server.WaitForExit(500)) { }                                                        # short slices, so Ctrl-C in this window still works
}
finally {
    if ($Server -and -not $Server.HasExited) {
        if (-not $Saved) { $Server.Kill($true); [void]$Server.WaitForExit(10000) }                  # no record: stop it at once, with everything it started
        elseif (-not $Server.WaitForExit(5000)) { $Server.Kill($true) }                              # Ctrl-C reaches both; this only makes sure the server we started is gone
    }
}
if ($Server.ExitCode -eq 0) { 'Window 1''s server has stopped.' }
else { "Window 1's server stopped with an error (code $($Server.ExitCode)). Tell the lead." }
