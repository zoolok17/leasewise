# Step 3: check Python and the agenttalk usage files. Both checks only read, and the usage files are only counted: nothing in them is shown.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 3: check Python and the usage files'
& $Py @PyFlags -c 'import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 11) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Python is older than 3.11.' }
$StateFolder = Join-Path $BusRoot 'state'
if (-not (Test-Path -LiteralPath $StateFolder -PathType Container)) { throw 'The agenttalk state folder was not found. Nothing was changed. Tell the lead.' }
$Count = @(Get-ChildItem -LiteralPath $StateFolder -Filter '*.capacity.json' -File).Count
'{0} usage file(s) found' -f $Count
if ($Count -eq 0) { throw 'No usage file was found yet. Run a turn in a seat first, or tell the lead. Nothing was changed.' }
$Usable = @(Get-UsageAccounts)                           # the format the reporter accepts (agenttalk 0.99.0 or newer); only counted, never shown
foreach ($Provider in 'claude', 'codex') {
    $Number = @($Usable | Where-Object { $_.Provider -ceq $Provider }).Count
    '{0}: {1} usable usage file(s)' -f $Provider, $Number
    if ($Number -eq 0) { throw "No usable $Provider usage file was found. Every seat must run agenttalk 0.99.0 or newer, be restarted on it, and have done one turn since. If they do, a later agenttalk may write another format: tell the lead. Nothing was changed." }
}
