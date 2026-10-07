# Step 5: make the private file, then the identity value (the runbook's block, unchanged). The value is never printed;
# it lives only in this script's own process, which ends when the script ends (the runbook's "close the window").
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 5: the private file, then the identity value'
New-Item -ItemType File -Path $ConfigPath | Out-Null           # empty: nothing secret yet
Protect-Path $ConfigPath $false                                # its own list: you, SYSTEM, Administrators; nothing inherited
Assert-Private $ConfigPath $true                               # read back by identifier; stops here if anything is wrong

$Bytes = New-Object byte[] 24
$Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$Rng.GetBytes($Bytes)
$Id = ($Bytes | ForEach-Object { $_.ToString('x2') }) -join ''
& $Py -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-ref $Id --enabled true
if ($LASTEXITCODE -ne 0) { throw 'identity-set failed' }
Assert-Private $Store $false                                   # the store now holds the value: it must still be private

$GatewayReport  = @($GatewayExe) + $GatewayPre + @('gateway','report','--json')
$GatewayReceipt = @($GatewayExe) + $GatewayPre + @('gateway','receipts')
$ToToml = { param($Value) '"' + ([string]$Value).Replace('\', '\\').Replace('"', '\"') + '"' }       # a TOML basic string: only \ and " need escaping
$ListToml = { param($Items) ($Items | ForEach-Object { & $ToToml $_ }) -join ', ' }
$ConfigText = @"
version = 1
base_url = $(& $ToToml $BaseUrl)
identity = $(& $ToToml $Id)
machine = $(& $ToToml $MachineAlias)
journal_root = $(& $ToToml $Journal)
state_path = $(& $ToToml (Join-Path $StateDir 'reporter.db'))
policy_ids = $(& $ToToml $PolicyIds)
poll_seconds = 60

[gateway_receipts]
gateway = $(& $ToToml $GatewayAlias)
admission_store = $(& $ToToml (Join-Path $Trial 'admission.db'))
command = [$(& $ListToml $GatewayReceipt)]

[gateway_receipts.env]
LOCALAPPDATA = $(& $ToToml $env:LOCALAPPDATA)

[gateway_report]
gateway = $(& $ToToml $GatewayAlias)
account = $(& $ToToml $AccountAlias)
command = [$(& $ListToml $GatewayReport)]

[gateway_report.env]
LOCALAPPDATA = $(& $ToToml $env:LOCALAPPDATA)
"@
[IO.File]::WriteAllText($ConfigPath, $ConfigText, $Utf8)       # UTF-8 without a byte-order mark; the file keeps its private list
Assert-Private $ConfigPath $true                               # and it is still private after the write
Remove-Variable Id, Bytes, ConfigText
(Get-Acl -LiteralPath $ConfigPath).Access | ForEach-Object { $_.IdentityReference.Value }
