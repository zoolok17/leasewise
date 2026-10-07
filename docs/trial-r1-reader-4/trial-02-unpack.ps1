# Step 2: check the archive, then unpack it (the runbook's block, unchanged).
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== Step 2: check the archive, then unpack it'
$Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Archive).Hash
if ($Actual -ne $ExpectedSha) { throw 'The archive does not match its checksum. Stop here and tell the lead.' }
New-Item -ItemType Directory -Force $Src | Out-Null
Expand-Archive -LiteralPath $Archive -DestinationPath $Src
Get-ChildItem $Src | Select-Object -ExpandProperty Name
