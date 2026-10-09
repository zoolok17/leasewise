# Step 1: choose the names, then make the private folder (fill in the values in angle brackets first).
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 1: names and the private folder'

$Py            = '<C:\path\to\python.exe>'                      # Python 3.11 or newer
$Archive       = '<C:\path\to\leasewise-COMMIT.zip>'            # the archive the lead gave you; it must lie inside C:\lw
$ExpectedSha   = '<the SHA-256 the lead gave you>'
$MachineAlias  = 'laptop-b'                                     # this machine, as Leasewise names it
$ClaudeAlias   = 'claude-main'                                  # our Claude account, as Leasewise names it
$CodexAlias    = 'codex-main'                                   # our Codex account, as Leasewise names it
$ProjectAlias  = 'leasewise-team'                               # the project both accounts belong to in Leasewise

$Src        = Join-Path $LiveRoot 'src'                         # Leasewise's source, extracted
$Store      = Join-Path $LiveRoot 'store\inventory.db'          # Leasewise's store
$StateDir   = Join-Path $LiveRoot 'state'                       # the reporter's own state
$Journal    = Join-Path $LiveRoot 'journal'                     # an empty folder the reporter insists on
$Names      = Join-Path $LiveRoot 'names.json'

if ($Py -like '*<*' -or $Archive -like '*<*' -or $ExpectedSha -like '*<*') { throw 'Fill in the three values in angle brackets first. Nothing was changed.' }
if (($ExpectedSha -cnotmatch '^[0-9A-Fa-f]{64}$') -or (($MachineAlias, $ClaudeAlias, $CodexAlias, $ProjectAlias | Where-Object { $_ -cnotmatch '^[a-z0-9][a-z0-9-]{0,62}$' }).Count -gt 0)) { throw 'The checksum or a name is not in the allowed form. Nothing was changed.' }
Assert-PythonFile $Py
Assert-PlainFile $Archive                                       # a file inside C:\lw with no link anywhere on its way
if ([IO.Path]::GetExtension($Archive) -ne '.zip') { throw 'The archive must be a .zip file. Nothing was changed.' }
if (Test-Path -LiteralPath $LiveRoot) { throw "The folder $LiveRoot already exists. Nothing was changed. If an earlier run made it, roll it back with step 15; otherwise tell the lead." }
New-Item -ItemType Directory -Path $LiveRoot | Out-Null  # no -Force: an existing folder is an error
Protect-Path $LiveRoot $true
Assert-Private $LiveRoot $true

$Token = [guid]::NewGuid().ToString('N')                 # random
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
[IO.File]::WriteAllText((Join-Path $LiveRoot '.owner.json'), (ConvertTo-Json @{ token = $Token; path = $Here }), $Utf8)
New-Item -ItemType Directory -Path $StateDir, $Journal, (Split-Path $Store) | Out-Null

$Saved = [ordered]@{                                     # only these names are ever read back; every path is recomputed from LiveRoot and BusRoot
    LiveRoot = $LiveRoot; BusRoot = $BusRoot; Port = $PortNumber; Token = $Token; Py = $Py; Archive = $Archive; ExpectedSha = $ExpectedSha
    MachineAlias = $MachineAlias; ClaudeAlias = $ClaudeAlias; CodexAlias = $CodexAlias; ProjectAlias = $ProjectAlias
}
[IO.File]::WriteAllText($Names, ($Saved | ConvertTo-Json -Depth 3), $Utf8)
Test-Path -LiteralPath $Names
