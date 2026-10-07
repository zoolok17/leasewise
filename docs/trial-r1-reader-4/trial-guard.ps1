# The one entry guard of the trial scripts. The shared start (trial-common.ps1) and the two step-6 window scripts all dot-source THIS file, so
# the check cannot differ between copies. It runs before anything is entered or run, and it stops (nothing is changed) when:
#   * the default trial folder, or its names.json, is a link (a junction or symbolic link): nothing is read, listed or entered through it;
#   * names.json names a trial folder other than the default one: only the default folder works with these steps (the step-6 windows read it).
# It leaves $DefaultTrial, $NamesFile and $N (the saved names, or nothing before step 1 has run) for the caller.
$DefaultTrial = Join-Path $env:LOCALAPPDATA 'leasewise-trial'
$NamesFile = Join-Path $DefaultTrial 'names.json'
foreach ($Item in $DefaultTrial, $NamesFile) {
    $Info = Get-Item -LiteralPath $Item -Force -ErrorAction SilentlyContinue
    if ($Info -and ($Info.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "$Item is a link. Nothing was changed. Tell the lead." }
}
$N = $null
if (Test-Path -LiteralPath $NamesFile) {
    $N = Get-Content -LiteralPath $NamesFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($N.Trial -ne $DefaultTrial) { throw "names.json names the trial folder $($N.Trial), but only $DefaultTrial works with these steps. Nothing was changed. Tell the lead." }
}
