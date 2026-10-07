# "If something goes wrong": the reader's closed words (the runbook's block, unchanged). Never paste reporter.toml or a report anywhere.
. (Join-Path $PSScriptRoot 'trial-common.ps1')
'== The reader''s closed words'
& $Py -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty money_reader | ConvertTo-Json -Depth 4
