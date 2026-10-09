# Step 4: set up the empty Leasewise store (private: it will hold this machine's identity value later).
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 4: set up the store'
& $Py -m quota_balancer --db $Store init
if ($LASTEXITCODE -ne 0) { throw 'init failed' }
Assert-Private (Split-Path $Store) $false
Test-Path -LiteralPath $Store
