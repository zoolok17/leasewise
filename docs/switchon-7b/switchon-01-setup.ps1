# Step 1: choose the names, then make the private folder (fill in the values in angle brackets first).
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 1: names and the private folder'

$Py            = '<C:\path\to\python.exe>'                      # Python 3.11 or newer
$Archive       = '<C:\path\to\leasewise-COMMIT.zip>'            # the archive the lead gave you
$ExpectedSha   = '<the SHA-256 the lead gave you>'
$MachineAlias  = 'laptop-b'                                     # this machine, as Leasewise names it
$ClaudeAlias   = 'claude-main'                                  # our Claude account, as Leasewise names it
$CodexAlias    = 'codex-main'                                   # our Codex account, as Leasewise names it
$ProjectAlias  = 'leasewise-team'                               # the project both accounts belong to in Leasewise

$Src        = Join-Path $LiveRoot 'src'                         # Leasewise's source, extracted
$Store      = Join-Path $LiveRoot 'store\inventory.db'          # Leasewise's store
$StateDir   = Join-Path $LiveRoot 'state'                       # the reporter's own state
$Journal    = Join-Path $LiveRoot 'journal'                     # an empty folder the reporter insists on
$PolicyIds  = Join-Path $LiveRoot 'policy-ids.json'
$ConfigPath = Join-Path $LiveRoot 'reporter.toml'               # private: it holds the identity value and the account texts
$Names      = Join-Path $LiveRoot 'names.json'
$BaseUrl    = 'http://127.0.0.1:{0}' -f $PortNumber             # this machine only

if ($Py -like '*<*' -or $Archive -like '*<*' -or $ExpectedSha -like '*<*') { throw 'Fill in the three values in angle brackets first. Nothing was changed.' }
if (Test-Path -LiteralPath $LiveRoot) { throw "The folder $LiveRoot already exists. Nothing was changed. If an earlier run made it, roll it back with step 14; otherwise tell the lead." }
New-Item -ItemType Directory -Path $LiveRoot | Out-Null  # no -Force: an existing folder is an error
Protect-Path $LiveRoot $true
Assert-Private $LiveRoot $true

$Token = [guid]::NewGuid().ToString('N')                 # random
$Here = (Resolve-Path -LiteralPath $LiveRoot).ProviderPath
[IO.File]::WriteAllText((Join-Path $LiveRoot '.owner.json'), (ConvertTo-Json @{ token = $Token; path = $Here }), $Utf8)
New-Item -ItemType Directory -Path $StateDir, $Journal, (Split-Path $Store) | Out-Null

$Saved = [ordered]@{
    LiveRoot = $LiveRoot; BusRoot = $BusRoot; Port = $PortNumber; Token = $Token; Py = $Py; Archive = $Archive; ExpectedSha = $ExpectedSha
    MachineAlias = $MachineAlias; ClaudeAlias = $ClaudeAlias; CodexAlias = $CodexAlias; ProjectAlias = $ProjectAlias
    Src = $Src; Store = $Store; StateDir = $StateDir; Journal = $Journal; PolicyIds = $PolicyIds; ConfigPath = $ConfigPath; Names = $Names; BaseUrl = $BaseUrl
}
[IO.File]::WriteAllText($Names, ($Saved | ConvertTo-Json -Depth 3), $Utf8)
Test-Path -LiteralPath $Names
