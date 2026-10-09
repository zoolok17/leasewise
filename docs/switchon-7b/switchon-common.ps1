param([switch]$KeepOutOfSource)    # the rollback sets this: it must not enter the source folder (which lives inside the folder it deletes)
# Shared start for every switch-on step script (card 7b-on; the model is trial-common.ps1 of the money-reader trial).
# It does what the open window would otherwise have to: stop at the first error, the UTF-8 writer, the privacy helpers, the entry guard, the saved names
# (only an allow-list of them; every path is recomputed from the checked roots) and Leasewise's source folder as the working folder.
$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'                       # keeps the source folder clean
$Utf8 = New-Object System.Text.UTF8Encoding($false)      # UTF-8 without a byte-order mark, for every file written below

# Who may open the private things: you (by security identifier), SYSTEM and Administrators. Names are never compared, only identifiers.
$Me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$Allowed = @($Me, 'S-1-5-18', 'S-1-5-32-544')
function Protect-Path {
    param([string]$Path, [bool]$Folder)
    $Acl = Get-Acl -LiteralPath $Path
    $Acl.SetAccessRuleProtection($true, $false)                          # nothing inherited from the folder above
    foreach ($Rule in @($Acl.Access)) { [void]$Acl.RemoveAccessRule($Rule) }
    $Flags = if ($Folder) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    foreach ($Sid in $Allowed) {
        $Who = New-Object Security.Principal.SecurityIdentifier($Sid)
        $Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($Who, 'FullControl', $Flags, 'None', 'Allow')))
    }
    Set-Acl -LiteralPath $Path -AclObject $Acl
}
function Test-PrivateProblem {
    param([string]$Path, [bool]$OwnList)                                  # $OwnList: the item has its own list and inherits nothing; returns a sentence or nothing
    $Acl = Get-Acl -LiteralPath $Path
    if ($OwnList -and -not $Acl.AreAccessRulesProtected) { return "$Path still inherits access from its folder." }
    $Seen = @()
    foreach ($Rule in $Acl.Access) {
        if ($Rule.AccessControlType -ne 'Allow') { continue }
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Allowed -notcontains $Sid) { return "Someone else can open $Path." }
        if ($OwnList -and $Rule.IsInherited) { return "$Path inherits an entry from its folder." }
        $Seen += $Sid
    }
    if ($Seen -notcontains $Me) { return "You cannot open $Path." }
}
function Assert-Private {
    param([string]$Path, [bool]$OwnList)                                  # the check BEFORE anything secret is written
    $Problem = Test-PrivateProblem $Path $OwnList
    if ($Problem) { throw "$Problem Stop here; nothing secret has been written, and nothing was changed." }
}
function Assert-PrivateAfter {
    param([string]$Path, [bool]$OwnList)                                  # the check AFTER a secret was written
    $Problem = Test-PrivateProblem $Path $OwnList
    if ($Problem) { throw "$Problem A secret HAS ALREADY BEEN WRITTEN there. Stop here and tell the lead before you do anything else." }
}

# The entry guard (a link anywhere on the way, a folder outside C:\lw, the two roots overlapping, or saved names for another run: stop before anything is read,
# listed or entered), then the checked saved names. Every derived path is recomputed from the checked roots and checked for links; none is read back from names.json.
. (Join-Path $PSScriptRoot 'switchon-guard.ps1')
if ($N) {
    foreach ($P in $N.PSObject.Properties) { Set-Variable -Name $P.Name -Value $P.Value }
    $Src = Join-Path $LiveRoot 'src'
    $Store = Join-Path $LiveRoot 'store\inventory.db'
    $StateDir = Join-Path $LiveRoot 'state'
    $Journal = Join-Path $LiveRoot 'journal'
    $PolicyIds = Join-Path $LiveRoot 'policy-ids.json'
    $ConfigPath = Join-Path $LiveRoot 'reporter.toml'
    $Names = $NamesFile
    $BaseUrl = 'http://127.0.0.1:{0}' -f $PortNumber
    foreach ($Derived in $Src, $Store, $StateDir, $Journal, $PolicyIds, $ConfigPath, (Join-Path $LiveRoot '.owner.json'), (Join-Path $LiveRoot 'baseline.json')) {
        if (Test-Path -LiteralPath $Derived) { Assert-NoLink $Derived }
    }
    if (-not $KeepOutOfSource -and (Test-Path -LiteralPath $Src)) { Set-Location -LiteralPath $Src }   # run every Leasewise command from inside the source folder (after step 3)
}
