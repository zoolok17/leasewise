# The one entry guard of the switch-on scripts. The shared start (switchon-common.ps1) dot-sources THIS file, so the check cannot differ between
# copies. It runs before anything is read, listed or entered, and it stops (nothing is changed) when:
#   * the live folder or the agenttalk folder is not a plain path inside C:\lw (dots, forward slashes, a trailing backslash, another drive);
#   * ANY existing folder on the way down from C:\lw to the live folder, its names.json, the agenttalk folder or its state folder is a link
#     (a junction or symbolic link): a link in an ancestor is as bad as a link at the end;
#   * the live folder and the agenttalk folder are the same, or one lies inside the other;
#   * names.json names a live folder, an agenttalk folder or a port other than the ones this run was checked with.
# Rehearsal (the lead's test run in a scratch folder) happens only when LW_SWITCHON_REHEARSAL is exactly "yes", and then ALL of
# LW_SWITCHON_ROOT, LW_SWITCHON_BUS, LW_SWITCHON_PORT and LW_SWITCHON_INPUT are required and both folders must lie inside C:\lw\scratch.
# For the operator any rehearsal variable stops the step: they are never silently accepted.
# It leaves $LiveRoot, $BusRoot, $PortNumber, $NamesFile, $N (the checked saved names, or nothing before step 1 has run) and the functions below.
$RehearsalNames = 'LW_SWITCHON_REHEARSAL', 'LW_SWITCHON_ROOT', 'LW_SWITCHON_BUS', 'LW_SWITCHON_PORT', 'LW_SWITCHON_INPUT', 'LW_SWITCHON_CLAUDE_TEXT', 'LW_SWITCHON_CODEX_TEXT'
$RehearsalSet = @($RehearsalNames | Where-Object { Test-Path -LiteralPath ('Env:' + $_) })
function Assert-Plain {
    param([string]$Path)
    if (($Path -ne [IO.Path]::GetFullPath($Path)) -or $Path.EndsWith([string][char]92) -or ($Path -notlike 'C:\lw\*')) { throw "$Path is not a plain path inside C:\lw. Nothing was changed. Tell the lead." }
}
function Assert-NoLink {
    param([string]$Path)                                                 # every existing folder from C:\lw down to the path itself
    $Walk = 'C:' + [char]92
    foreach ($Part in $Path.Substring(3).Split([char]92)) {
        $Walk = Join-Path $Walk $Part
        $Info = Get-Item -LiteralPath $Walk -Force -ErrorAction SilentlyContinue
        if ($Info -and ($Info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "$Walk is a link. Nothing was changed. Tell the lead." }
    }
}
function Assert-PlainFile {
    param([string]$Path)                                                 # a file inside C:\lw with no link anywhere on its way
    Assert-Plain $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Path is not a file. Nothing was changed. Tell the lead." }
    Assert-NoLink $Path
}
function Assert-PythonFile {
    param([string]$Path)                                                 # Python may live outside C:\lw; it must be one plain file
    if (($Path -ne [IO.Path]::GetFullPath($Path)) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'The Python path is not the full path of an existing file. Nothing was changed.' }
    if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'The Python path is a link. Nothing was changed. Tell the lead.' }
}
function Assert-Separate {
    $Live = $LiveRoot.ToLowerInvariant() + [char]92
    $Bus = $BusRoot.ToLowerInvariant() + [char]92
    if ($Live.StartsWith($Bus) -or $Bus.StartsWith($Live)) { throw 'The live folder and the agenttalk folder are the same, or one lies inside the other. Nothing was changed. Tell the lead.' }
}
if ($env:LW_SWITCHON_REHEARSAL -ceq 'yes') {
    foreach ($Name in 'LW_SWITCHON_ROOT', 'LW_SWITCHON_BUS', 'LW_SWITCHON_PORT', 'LW_SWITCHON_INPUT') {
        if (-not (Test-Path -LiteralPath ('Env:' + $Name))) { throw "Rehearsal mode needs all of its settings and $Name is missing. Nothing was changed." }
    }
    $Texts = @('LW_SWITCHON_CLAUDE_TEXT', 'LW_SWITCHON_CODEX_TEXT' | Where-Object { Test-Path -LiteralPath ('Env:' + $_) })
    if ($env:LW_SWITCHON_INPUT -ceq 'env') { if ($Texts.Count -ne 2) { throw 'Rehearsal input "env" needs both test texts. Nothing was changed.' } }
    elseif ($env:LW_SWITCHON_INPUT -ceq 'prompt') { if ($Texts.Count -ne 0) { throw 'Rehearsal input "prompt" must not set the test texts. Nothing was changed.' } }
    else { throw 'LW_SWITCHON_INPUT must be "env" or "prompt". Nothing was changed.' }
    if ($env:LW_SWITCHON_PORT -notmatch '^[0-9]{4,5}$' -or [int]$env:LW_SWITCHON_PORT -lt 1024 -or [int]$env:LW_SWITCHON_PORT -gt 65535 -or [int]$env:LW_SWITCHON_PORT -eq 8443) { throw 'The rehearsal port must be 1024 to 65535 and not 8443. Nothing was changed.' }
    $LiveRoot = $env:LW_SWITCHON_ROOT
    $BusRoot = $env:LW_SWITCHON_BUS
    $PortNumber = [int]$env:LW_SWITCHON_PORT
    foreach ($Path in $LiveRoot, $BusRoot) { if ($Path -notlike 'C:\lw\scratch\*') { throw "$Path is not inside C:\lw\scratch. Nothing was changed." } }
}
elseif ($RehearsalSet.Count -gt 0) {
    throw 'A rehearsal setting (LW_SWITCHON_...) is present in this window. Those are for the lead''s test run only. Close this window and open a new one. Nothing was changed.'
}
else {
    $LiveRoot = 'C:\lw\live'
    $BusRoot = 'C:\lw\team\.agenttalk'
    $PortNumber = 8443
}
foreach ($Path in $LiveRoot, $BusRoot) { Assert-Plain $Path }
Assert-Separate
$NamesFile = Join-Path $LiveRoot 'names.json'
foreach ($Item in $LiveRoot, $NamesFile, $BusRoot, (Join-Path $BusRoot 'state')) { Assert-NoLink $Item }
$N = $null
if (Test-Path -LiteralPath $NamesFile) {
    $Saved = Get-Content -LiteralPath $NamesFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (($Saved.LiveRoot -cne $LiveRoot) -or ($Saved.BusRoot -cne $BusRoot) -or ([string]$Saved.Port -cne [string]$PortNumber)) {
        throw 'names.json was made for another live folder, agenttalk folder or port than this run uses. Nothing was changed. Tell the lead.'
    }
    $Alias = '^[a-z0-9][a-z0-9-]{0,62}$'
    if (([string]$Saved.Token -cnotmatch '^[0-9a-f]{32}$') -or ([string]$Saved.ExpectedSha -cnotmatch '^[0-9A-Fa-f]{64}$')) { throw 'names.json is not the file step 1 made. Nothing was changed. Tell the lead.' }
    foreach ($Field in 'MachineAlias', 'ClaudeAlias', 'CodexAlias', 'ProjectAlias') { if ([string]$Saved.$Field -cnotmatch $Alias) { throw 'names.json is not the file step 1 made. Nothing was changed. Tell the lead.' } }
    Assert-PythonFile ([string]$Saved.Py)
    # Only these fields are taken from the file; every path below is recomputed from the checked roots, never read back.
    $N = [pscustomobject]@{
        LiveRoot = $LiveRoot; BusRoot = $BusRoot; Port = $PortNumber; Token = [string]$Saved.Token; Py = [string]$Saved.Py; Archive = [string]$Saved.Archive
        ExpectedSha = [string]$Saved.ExpectedSha; MachineAlias = [string]$Saved.MachineAlias; ClaudeAlias = [string]$Saved.ClaudeAlias
        CodexAlias = [string]$Saved.CodexAlias; ProjectAlias = [string]$Saved.ProjectAlias
    }
}
