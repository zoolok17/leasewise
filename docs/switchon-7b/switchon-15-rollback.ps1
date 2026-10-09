# Step 15: roll back. $Token and $LiveRoot come from names.json, saved in step 1 (and checked again by the shared start). The shared start is told to stay out of
# the source folder, and it stops first thing if any folder on the way to the live folder, names.json or the agenttalk folder is a link, or if anything inside the live
# folder is. The agenttalk folder is never touched, and this script refuses to delete anything if the two folders overlap or if window 1 or 2 is still running.
# If names.json is missing (step 1 stopped early), it deletes only a folder that holds nothing but the ownership marker. The marker and names.json are deleted last,
# so a delete that fails part way can be repeated with every check.
. (Join-Path $PSScriptRoot 'switchon-common.ps1') -KeepOutOfSource
'== Step 15: roll back'
Set-Location -LiteralPath (Split-Path -Parent $LiveRoot)           # leave the live folder (it is about to be deleted)
Assert-Plain $LiveRoot
Assert-NoLink $LiveRoot
if (-not (Test-Path -LiteralPath $LiveRoot -PathType Container)) { 'There is nothing to roll back: the folder does not exist.'; return }
$Running = @(Get-LiveProcesses)                          # the same list as step 14, without this script's own process
if ($Running.Count -gt 0) { throw ('A program that uses the live folder is still running (process {0}, program {1}). If it is window 1 or 2, press Ctrl-C there; otherwise close that program. Then run step 15 again. Nothing was deleted.' -f $Running[0].ProcessId, $Running[0].Name) }
$MarkerFile = Join-Path $LiveRoot '.owner.json'
Assert-NoLink $MarkerFile                                          # the marker is a plain file, not a link, before it is read
if (-not (Test-Path -LiteralPath $MarkerFile -PathType Leaf)) { throw 'The folder holds no ownership marker, so it is not a folder this runbook made. Nothing was deleted. Tell the lead.' }
$Marker = Get-Content -LiteralPath $MarkerFile -Raw -Encoding UTF8 | ConvertFrom-Json
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
if ($N) {
    if (($Marker.token -cne $Token) -or ($Marker.path -cne $Here)) { throw 'This is not the folder this runbook made. Nothing was deleted.' }
}
else {                                                   # names.json is missing: only a folder that holds nothing but the marker may go
    $Inside = @((New-Object IO.DirectoryInfo($Here)).GetFileSystemInfos())
    if (($Marker.path -cne $Here) -or ([string]$Marker.token -cnotmatch '^[0-9a-f]{32}$') -or $Inside.Count -ne 1 -or $Inside[0].Name -cne '.owner.json') {
        throw 'names.json is missing and the folder holds more than the ownership marker, so it cannot be told to be ours. Nothing was deleted. Tell the lead.'
    }
}
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
try {
    foreach ($Child in @(Get-ChildItem -LiteralPath $LiveRoot -Force | Where-Object { $_.Name -cne '.owner.json' -and $_.Name -cne 'names.json' })) {
        Remove-Item -LiteralPath $Child.FullName -Recurse -Force
    }
    if (Test-Path -LiteralPath $NamesFile) { Remove-Item -LiteralPath $NamesFile -Force }
    Remove-Item -LiteralPath $MarkerFile -Force
    [IO.Directory]::Delete($LiveRoot)                    # not recursive: the folder is empty by now
}
catch { throw ('A file could not be deleted (is a program still using it?). The ownership marker and names.json are kept while anything else is left, so you can run step 15 again after closing it. Tell the lead if it fails again. ' + $_.Exception.Message) }
Test-Path -LiteralPath $LiveRoot
