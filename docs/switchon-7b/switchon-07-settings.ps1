# Step 7: make the private settings file, then this machine's identity value. The value is never printed; it lives only in this script's own
# process (which ends when the script ends) and in the two private files. The settings have NO readings table yet: that is step 8.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 7: the private settings file and the identity value'
New-Item -ItemType File -Path $ConfigPath | Out-Null          # empty: nothing secret yet
Protect-Path $ConfigPath $false                               # its own list: you, SYSTEM, Administrators; nothing inherited
Assert-Private $ConfigPath $true                              # read back by identifier; stops here if anything is wrong

$Bytes = New-Object byte[] 24
$Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$Rng.GetBytes($Bytes)
$Id = ($Bytes | ForEach-Object { $_.ToString('x2') }) -join ''
& $Py -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-ref $Id --enabled true
if ($LASTEXITCODE -ne 0) { throw 'identity-set failed' }
Assert-Private $Store $false                                  # the store now holds the value: it must still be private

$ToToml = { param($Value) '"' + ([string]$Value).Replace('\', '\\').Replace('"', '\"') + '"' }       # a TOML basic string: only \ and " need escaping
$ConfigText = @"
version = 1
base_url = $(& $ToToml $BaseUrl)
identity = $(& $ToToml $Id)
machine = $(& $ToToml $MachineAlias)
journal_root = $(& $ToToml $Journal)
state_path = $(& $ToToml (Join-Path $StateDir 'reporter.db'))
policy_ids = $(& $ToToml $PolicyIds)
poll_seconds = 30
"@
[IO.File]::WriteAllText($ConfigPath, $ConfigText, $Utf8)      # UTF-8 without a byte-order mark; the file keeps its private list
Assert-Private $ConfigPath $true                              # and it is still private after the write
Remove-Variable Id, Bytes, ConfigText
'private: {0} access entries, all of them you, SYSTEM or Administrators' -f (Get-Acl -LiteralPath $ConfigPath).Access.Count
