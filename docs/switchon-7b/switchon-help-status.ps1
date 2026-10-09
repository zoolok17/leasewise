# "If something goes wrong" and "Tell the lead": the reporter's readings status, with aliases and closed words only. It never shows an account
# text, a seat name, a file name or a path. Paste its output to the lead; never paste reporter.toml.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== The readings status'
& $Py @PyFlags -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty readings | ConvertTo-Json -Depth 5
& $Py @PyFlags -m quota_balancer reporter status --config $ConfigPath --json | ConvertFrom-Json | Select-Object -ExpandProperty operator_actions
