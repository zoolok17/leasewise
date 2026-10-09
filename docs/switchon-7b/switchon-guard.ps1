# The one entry guard of the switch-on scripts. The shared start (switchon-common.ps1) dot-sources THIS file, so the check cannot differ between
# copies. It runs before anything is read, listed or entered, and it stops (nothing is changed) when:
#   * the live folder or the bus folder is not a plain folder path inside C:\lw (dots, forward slashes, a trailing backslash, another drive or a folder elsewhere);
#   * the live folder, its names.json, the bus folder or the bus folder's state folder is a link (a junction or symbolic link);
#   * names.json names a live folder other than this one.
# The three LW_SWITCHON_ values are for the rehearsal in the lead's scratch folder only. The operator never sets them.
# It leaves $LiveRoot, $BusRoot, $PortNumber, $NamesFile and $N (the saved names, or nothing before step 1 has run) for the caller.
$LiveRoot = if ($env:LW_SWITCHON_ROOT) { $env:LW_SWITCHON_ROOT } else { 'C:\lw\live' }
$BusRoot = if ($env:LW_SWITCHON_BUS) { $env:LW_SWITCHON_BUS } else { 'C:\lw\team\.agenttalk' }
$PortNumber = if ($env:LW_SWITCHON_PORT) { [int]$env:LW_SWITCHON_PORT } else { 8443 }
foreach ($Path in $LiveRoot, $BusRoot) {
    if (($Path -ne [IO.Path]::GetFullPath($Path)) -or $Path.EndsWith([string][char]92) -or ($Path -notlike 'C:\lw\*')) { throw "$Path is not a plain folder inside C:\lw. Nothing was changed. Tell the lead." }
}
$NamesFile = Join-Path $LiveRoot 'names.json'
foreach ($Item in $LiveRoot, $NamesFile, $BusRoot, (Join-Path $BusRoot 'state')) {
    $Info = Get-Item -LiteralPath $Item -Force -ErrorAction SilentlyContinue
    if ($Info -and ($Info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "$Item is a link. Nothing was changed. Tell the lead." }
}
$N = $null
if (Test-Path -LiteralPath $NamesFile) {
    $N = Get-Content -LiteralPath $NamesFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($N.LiveRoot -ne $LiveRoot) { throw "names.json names the live folder $($N.LiveRoot), but this run uses $LiveRoot. Nothing was changed. Tell the lead." }
}
