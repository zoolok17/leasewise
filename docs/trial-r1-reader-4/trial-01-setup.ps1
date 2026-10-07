# Step 1: choose the names, then make the private folder (the runbook's two step-1 blocks; fill in the values in angle brackets first).
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 1: names and the private folder'

$Trial        = Join-Path $env:LOCALAPPDATA 'leasewise-trial'   # the one new folder; deleting it undoes the trial
$Py           = '<C:\path\to\python.exe>'                       # Python 3.11 or newer
$GatewayExe   = '<C:\path\to\agenttalk.exe>'                    # the gateway's program, an .exe
$GatewayPre   = @()
$MachineAlias = 'desktop'
$AccountAlias = '<account alias>'
$GatewayAlias = '<gateway alias>'
$BudgetMicroEur = <budget in micro-euro>
$ShareMicroEur  = <share in micro-euro>
$Archive      = '<C:\path\to\leasewise-COMMIT.zip>'
$ExpectedSha  = '<the SHA-256 the lead gave you>'

$Src        = Join-Path $Trial 'src'                     # Leasewise's source, extracted
$Store      = Join-Path $Trial 'store\inventory.db'      # Leasewise's store
$StateDir   = Join-Path $Trial 'state'                   # the reader's own state
$Journal    = Join-Path $Trial 'journal'                 # an empty folder the reader insists on
$PolicyIds  = Join-Path $Trial 'policy-ids.json'
$ConfigPath = Join-Path $Trial 'reporter.toml'           # private: it holds the identity value
$Names      = Join-Path $Trial 'names.json'
$BaseUrl    = 'http://127.0.0.1:8443'

if (Test-Path -LiteralPath $Trial) { throw "The folder $Trial already exists. Nothing was changed. Choose a new name for `$Trial and start again." }
New-Item -ItemType Directory -Path $Trial | Out-Null     # no -Force: an existing folder is an error
Protect-Path $Trial $true
Assert-Private $Trial $true

$Token = [guid]::NewGuid().ToString('N')                 # random
$Here = (Resolve-Path -LiteralPath $Trial).ProviderPath
[IO.File]::WriteAllText((Join-Path $Trial '.owner.json'), (ConvertTo-Json @{ token = $Token; path = $Here }), $Utf8)
New-Item -ItemType Directory -Path $StateDir, $Journal, (Split-Path $Store) | Out-Null

$Saved = [ordered]@{
    Trial = $Trial; Token = $Token; Py = $Py; GatewayExe = $GatewayExe; GatewayPre = @($GatewayPre); MachineAlias = $MachineAlias
    AccountAlias = $AccountAlias; GatewayAlias = $GatewayAlias; BudgetMicroEur = $BudgetMicroEur; ShareMicroEur = $ShareMicroEur
    Archive = $Archive; ExpectedSha = $ExpectedSha; Src = $Src; Store = $Store; StateDir = $StateDir; Journal = $Journal
    PolicyIds = $PolicyIds; ConfigPath = $ConfigPath; Names = $Names; BaseUrl = $BaseUrl
}
[IO.File]::WriteAllText($Names, ($Saved | ConvertTo-Json -Depth 3), $Utf8)
Test-Path -LiteralPath $Names
