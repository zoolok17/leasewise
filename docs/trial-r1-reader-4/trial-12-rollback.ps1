# Step 12: roll back (the runbook's block, unchanged; $Token and $Trial come from names.json, saved in step 1).
# The shared start is told to stay out of the source folder, and it stops first thing if the trial folder or names.json is a link.
. (Join-Path $PSScriptRoot 'trial-common.ps1') -KeepOutOfSource
'== Step 12: roll back'
Set-Location -LiteralPath $env:USERPROFILE
if ((New-Object IO.DirectoryInfo($Trial)).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { throw 'The trial folder contains a link. Nothing was deleted. Tell the lead.' }
$Marker = Get-Content -LiteralPath (Join-Path $Trial '.owner.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Here = (Resolve-Path -LiteralPath $Trial).ProviderPath
if (($Marker.token -ne $Token) -or ($Marker.path -ne $Here)) { throw 'This is not the folder this runbook made. Nothing was deleted.' }
$Links = 0                                               # a link anywhere inside: look at every entry, never go into a link (a linked trial folder was refused above)
$Pending = New-Object System.Collections.Generic.Stack[string]
$Pending.Push($Here)
while ($Pending.Count -gt 0) {
    foreach ($Entry in (New-Object IO.DirectoryInfo($Pending.Pop())).GetFileSystemInfos()) {
        if ($Entry.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Links++ }
        elseif ($Entry -is [IO.DirectoryInfo]) { $Pending.Push($Entry.FullName) }
    }
}
if ($Links -gt 0) { throw 'The trial folder contains a link. Nothing was deleted. Tell the lead.' }
Remove-Item -LiteralPath $Trial -Recurse -Force
Test-Path -LiteralPath $Trial
