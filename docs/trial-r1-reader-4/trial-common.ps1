param([switch]$KeepOutOfSource)    # step 12 sets this: it must not enter the source folder (which lives inside the folder it deletes)
# Shared start for every trial step script (made 2026-10-07 by the desktop lead so the operator need not paste blocks).
# It only does what the pasted runbook got from step 1's block and the open window: stop at the first error, the UTF-8 writer,
# the two privacy helpers (copied unchanged from step 1), the saved names, and Leasewise's source folder as the working folder.
$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'                       # keeps the source folder clean
$Utf8 = New-Object System.Text.UTF8Encoding($false)      # UTF-8 without a byte-order mark, for every file written below

# Who may open the private things: you (by security identifier), SYSTEM and Administrators. Names are never compared, only identifiers.
$Me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$Allowed = @($Me, 'S-1-5-18', 'S-1-5-32-544')
function Protect-Path {
    param([string]$Path, [bool]$Folder)
    $Item = Get-Item -LiteralPath $Path -Force
    $Acl = [IO.FileSystemAclExtensions]::GetAccessControl($Item, [Security.AccessControl.AccessControlSections]::Access)   # the access list only: writing back the owner or audit parts needs an administrator privilege
    $Acl.SetAccessRuleProtection($true, $false)                          # nothing inherited from the folder above
    foreach ($Rule in @($Acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))) { [void]$Acl.RemoveAccessRule($Rule) }     # by identifier: the .Access shortcut is empty on this kind of object
    $Flags = if ($Folder) { 'ContainerInherit,ObjectInherit' } else { 'None' }
    foreach ($Sid in $Allowed) {
        $Who = New-Object Security.Principal.SecurityIdentifier($Sid)
        $Acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($Who, 'FullControl', $Flags, 'None', 'Allow')))
    }
    [IO.FileSystemAclExtensions]::SetAccessControl($Item, $Acl)
}
function Assert-Private {
    param([string]$Path, [bool]$OwnList)                                  # $OwnList: the item has its own list and inherits nothing
    $Acl = Get-Acl -LiteralPath $Path
    if ($OwnList -and -not $Acl.AreAccessRulesProtected) { throw "$Path still inherits access from its folder. Stop here; nothing secret has been written." }
    $Seen = @()
    foreach ($Rule in $Acl.Access) {
        if ($Rule.AccessControlType -ne 'Allow') { continue }
        $Sid = $Rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($Allowed -notcontains $Sid) { throw "Someone else can open $Path. Stop here; nothing secret has been written." }
        if ($OwnList -and $Rule.IsInherited) { throw "$Path inherits an entry from its folder. Stop here; nothing secret has been written." }
        $Seen += $Sid
    }
    if ($Seen -notcontains $Me) { throw "You cannot open $Path. Stop here." }
}

# The entry guard (a link in the way, or a different trial folder: stop before anything is read, listed or entered), then the names saved by step 1.
. (Join-Path $PSScriptRoot 'trial-guard.ps1')
if ($N) {
    foreach ($P in $N.PSObject.Properties) { Set-Variable -Name $P.Name -Value $P.Value }
    $GatewayPre = @($GatewayPre)                         # an empty list stays a list
    if (-not $KeepOutOfSource -and (Test-Path -LiteralPath $Src)) { Set-Location -LiteralPath $Src }   # run every Leasewise command from inside the source folder (after step 2)
}
