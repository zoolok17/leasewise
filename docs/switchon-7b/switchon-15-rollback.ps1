# Step 15: roll back. $Token and $LiveRoot come from names.json, saved in step 1 (and checked again by the shared start). The shared start is told to stay out of
# the source folder, and it stops first thing if any folder on the way to the live folder, names.json or the agenttalk folder is a link. The agenttalk folder is
# never touched, and this script refuses to delete anything if the two folders overlap.
. (Join-Path $PSScriptRoot 'switchon-common.ps1') -KeepOutOfSource
'== Step 15: roll back'
Set-Location -LiteralPath (Split-Path -Parent $LiveRoot)           # leave the live folder (it is about to be deleted)
Assert-Plain $LiveRoot
Assert-NoLink $LiveRoot
Assert-NoLink (Join-Path $LiveRoot '.owner.json')                  # the marker is a plain file, not a link, before it is read
$Marker = Get-Content -LiteralPath (Join-Path $LiveRoot '.owner.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
if (($Marker.token -ne $Token) -or ($Marker.path -ne $Here)) { throw 'This is not the folder this runbook made. Nothing was deleted.' }
$Links = 0                                               # a link anywhere inside: look at every entry, never go into a link
$Pending = New-Object System.Collections.Generic.Stack[string]
$Pending.Push($Here)
while ($Pending.Count -gt 0) {
    foreach ($Entry in (New-Object IO.DirectoryInfo($Pending.Pop())).GetFileSystemInfos()) {
        if ($Entry.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Links++ }
        elseif ($Entry -is [IO.DirectoryInfo]) { $Pending.Push($Entry.FullName) }
    }
}
if ($Links -gt 0) { throw 'The live folder contains a link. Nothing was deleted. Tell the lead.' }
Assert-Separate                                          # the last look before the delete: the agenttalk folder is not the live folder and not inside it
Remove-Item -LiteralPath $LiveRoot -Recurse -Force
Test-Path -LiteralPath $LiveRoot
