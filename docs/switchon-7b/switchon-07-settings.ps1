# Step 7: make the private settings file, then this machine's identity value. The value is never printed and never put on a command line (it goes to the
# enrollment through standard input); it lives only in this script's own process (which ends when the script ends) and in the two private files.
# Everything that will hold the value is checked to be private BEFORE the value exists; the checks after the writes are kept as well.
# The settings have NO readings table yet: that is step 8.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 7: the private settings file and the identity value'
Assert-Private $LiveRoot $true                                # before anything is written: the live folder, ...
Assert-Private (Split-Path $Store) $false                     # ... the folder of the store, ...
Assert-Private $Store $false                                  # ... and the store itself must already be private
New-Item -ItemType File -Path $ConfigPath | Out-Null          # empty: nothing secret yet
Protect-Path $ConfigPath $false                               # its own list: you, SYSTEM, Administrators; nothing inherited
Assert-Private $ConfigPath $true                              # read back by identifier; stops here if anything is wrong

$Bytes = New-Object byte[] 24
$Rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$Rng.GetBytes($Bytes)
$Id = ($Bytes | ForEach-Object { $_.ToString('x2') }) -join ''
$Id | & $Py @PyFlags -m quota_balancer --db $Store identity-set --machine $MachineAlias --identity-stdin --enabled true
if ($LASTEXITCODE -ne 0) { throw 'identity-set failed' }
Assert-PrivateAfter $Store $false                             # the store now holds the value: it must still be private

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
Assert-PrivateAfter $ConfigPath $true                         # and it is still private after the write
Remove-Variable Id, Bytes, ConfigText
'private: {0} access entries, all of them you, SYSTEM or Administrators' -f (Get-Acl -LiteralPath $ConfigPath).Access.Count
