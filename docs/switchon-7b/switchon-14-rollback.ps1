# Step 14: roll back. $Token and $LiveRoot come from names.json, saved in step 1. The shared start is told to stay out of the source folder,
# and it stops first thing if the live folder, names.json or the bus folder is a link. The bus folder is never touched: it is not inside the live folder.
. (Join-Path $PSScriptRoot 'switchon-common.ps1') -KeepOutOfSource
'== Step 14: roll back'
Set-Location -LiteralPath (Split-Path -Parent $LiveRoot)           # leave the live folder (it is about to be deleted)
if ((New-Object IO.DirectoryInfo($LiveRoot)).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { throw 'The live folder contains a link. Nothing was deleted. Tell the lead.' }
$Marker = Get-Content -LiteralPath (Join-Path $LiveRoot '.owner.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
if (($Marker.token -ne $Token) -or ($Marker.path -ne $Here)) { throw 'This is not the folder this runbook made. Nothing was deleted.' }
$Links = 0                                               # a link anywhere inside: look at every entry, never go into a link (a linked live folder was refused above)
$Pending = New-Object System.Collections.Generic.Stack[string]
$Pending.Push($Here)
while ($Pending.Count -gt 0) {
    foreach ($Entry in (New-Object IO.DirectoryInfo($Pending.Pop())).GetFileSystemInfos()) {
        if ($Entry.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) { $Links++ }
        elseif ($Entry -is [IO.DirectoryInfo]) { $Pending.Push($Entry.FullName) }
    }
}
if ($Links -gt 0) { throw 'The live folder contains a link. Nothing was deleted. Tell the lead.' }
Remove-Item -LiteralPath $LiveRoot -Recurse -Force
Test-Path -LiteralPath $LiveRoot
