# Step 1: choose the names, then make the private folder. The script asks three plain questions (none of them is secret, so what you type is shown).
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 1: names and the private folder'

if (Test-Path -LiteralPath 'Env:LW_SWITCHON_PYTHON') {   # the lead's test run only (the guard allows it in rehearsal mode and nowhere else)
    $Py = $env:LW_SWITCHON_PYTHON
    $Archive = $env:LW_SWITCHON_ARCHIVE
    $ExpectedSha = $env:LW_SWITCHON_SHA
}
else {
    $Py = (Read-Host 'The full path of python.exe (Python 3.11 or newer), for example C:\Python313\python.exe').Trim().Trim('"')
    $Archive = (Read-Host 'The full path of the archive the lead gave you (it must lie inside C:\lw), for example C:\lw\leasewise-1234abcd.zip').Trim().Trim('"')
    $ExpectedSha = (Read-Host 'The SHA-256 checksum the lead gave you (64 characters)').Trim()
}
$MachineAlias  = 'laptop-b'                                     # this machine, as Leasewise names it
$ClaudeAlias   = 'claude-main'                                  # our Claude account, as Leasewise names it
$CodexAlias    = 'codex-main'                                   # our Codex account, as Leasewise names it
$ProjectAlias  = 'leasewise-team'                               # the project both accounts belong to in Leasewise

$Src        = Join-Path $LiveRoot 'src'                         # Leasewise's source, extracted
$Store      = Join-Path $LiveRoot 'store\inventory.db'          # Leasewise's store
$StateDir   = Join-Path $LiveRoot 'state'                       # the reporter's own state
$Journal    = Join-Path $LiveRoot 'journal'                     # an empty folder the reporter insists on
$Names      = Join-Path $LiveRoot 'names.json'

if (($ExpectedSha -cnotmatch '^[0-9A-Fa-f]{64}$') -or (($MachineAlias, $ClaudeAlias, $CodexAlias, $ProjectAlias | Where-Object { $_ -cnotmatch '^[a-z0-9][a-z0-9-]{0,62}$' }).Count -gt 0)) { throw 'The checksum is not 64 characters of 0-9 and a-f. Nothing was changed. Run step 1 again.' }
Assert-PythonFile $Py
Assert-PlainFile $Archive                                       # a file inside C:\lw with no link anywhere on its way
if ([IO.Path]::GetExtension($Archive) -ne '.zip') { throw 'The archive must be a .zip file. Nothing was changed.' }
if (Test-Path -LiteralPath $LiveRoot) { throw "The folder $LiveRoot already exists. Nothing was changed. If an earlier run made it, roll it back with step 15; otherwise tell the lead." }
New-Item -ItemType Directory -Path $LiveRoot | Out-Null  # no -Force: an existing folder is an error

# Only the ownership marker comes first (a random token and the folder path, nothing personal): step 15 can remove the folder from it whatever happens next.
# names.json holds the path of Python, which can contain your user name, so it is written only after the folder has been made private and checked.
$Token = [guid]::NewGuid().ToString('N')                 # random
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
[IO.File]::WriteAllText((Join-Path $LiveRoot '.owner.json'), (ConvertTo-Json @{ token = $Token; path = $Here }), $Utf8)
try { Protect-Path $LiveRoot $true; Assert-Private $LiveRoot $true }
catch { throw "$LiveRoot was made and holds only the ownership marker (a random token and the folder path), but it could not be made private. Nothing secret and none of your answers were written. Run step 15 to remove it, then tell the lead." }
$Saved = [ordered]@{                                     # only these names are ever read back; every path is recomputed from LiveRoot and BusRoot
    LiveRoot = $LiveRoot; BusRoot = $BusRoot; Port = $PortNumber; Token = $Token; Py = $Py; Archive = $Archive; ExpectedSha = $ExpectedSha
    MachineAlias = $MachineAlias; ClaudeAlias = $ClaudeAlias; CodexAlias = $CodexAlias; ProjectAlias = $ProjectAlias
}
[IO.File]::WriteAllText($Names, ($Saved | ConvertTo-Json -Depth 3), $Utf8)
New-Item -ItemType Directory -Path $StateDir, $Journal, (Split-Path $Store) | Out-Null
Test-Path -LiteralPath $Names
