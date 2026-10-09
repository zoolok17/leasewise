# Step 2: check the archive, then unpack it.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 2: check the archive, then unpack it'
Assert-PlainFile $Archive                                # again: a file inside C:\lw, no link anywhere on its way
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash
if ($Actual -ne $ExpectedSha) { throw 'The archive does not match its checksum. Nothing was unpacked. Stop here and tell the lead.' }
New-Item -ItemType Directory -Force $Src | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $Src
Get-ChildItem $Src | Select-Object -ExpandProperty Name
