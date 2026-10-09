# Step 8 (operator step): the account table. On your own screen, `agenttalk capacity` prints one line per account like
#   claude account of OS user <name> (home-xxxxxxxx), 2 seats: ...
# The script asks for two parts of that line for each account: the name after "OS user" and the 8 characters of home-xxxxxxxx. The questions do NOT show what you
# type (they are masked), because the name is your Windows user name. The script puts the parts together as <provider>:<name>:home-<8 characters> (the text agenttalk
# writes into its usage files), checks the shape of each part and says which one is wrong without showing it, and then checks the result against our own usage files:
# a typo is found before anything is written. The text goes only into the private settings file, and nothing is written unless both accounts are right.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 8: the account table'
Assert-Private $LiveRoot $true                           # before anything is asked or written
Assert-Private $ConfigPath $true
if ((Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8) -match '\[\[readings\.sources\]\]') { throw 'The account table is already in the settings file. Nothing was changed. Tell the lead.' }
if ($env:LW_SWITCHON_INPUT -ceq 'env') {                 # the lead's test run only (the guard allows it in rehearsal mode and nowhere else)
    $ClaudeName, $ClaudeHome = $env:LW_SWITCHON_CLAUDE_NAME, $env:LW_SWITCHON_CLAUDE_HOME
    $CodexName, $CodexHome = $env:LW_SWITCHON_CODEX_NAME, $env:LW_SWITCHON_CODEX_HOME
}
else {
    $ClaudeName = Read-Host -MaskInput 'Claude: the name after "OS user" (it is not shown as you type)'
    $ClaudeHome = Read-Host -MaskInput 'Claude: the 8 characters of home-xxxxxxxx (it is not shown as you type)'
    $CodexName = Read-Host -MaskInput 'Codex: the name after "OS user" (it is not shown as you type)'
    $CodexHome = Read-Host -MaskInput 'Codex: the 8 characters of home-xxxxxxxx (it is not shown as you type)'
}
function Join-AccountText {
    param([string]$Provider, [string]$Label, [string]$Name, [string]$Home8)
    if ($Name.Length -lt 1 -or $Name.Length -gt 256 -or $Name -cmatch '[\p{Cc}:]' -or $Name -cne $Name.Trim()) {
        throw "The $Label name is not in the allowed form (1 to 256 characters, no colon, no control character, no space at either end). Nothing was changed. Run step 8 again."
    }
    if ($Home8 -cnotmatch '^(home-)?([0-9a-f]{8})$') { throw "The $Label home part must be exactly 8 characters 0-9 and a-f, with or without home- in front. Nothing was changed. Run step 8 again." }
    '{0}:{1}:home-{2}' -f $Provider, $Name, $Matches[2]
}
$ClaudeText = Join-AccountText 'claude' 'Claude' $ClaudeName $ClaudeHome
$CodexText = Join-AccountText 'codex' 'Codex' $CodexName $CodexHome
$Counts = Invoke-UsageScan 'match' @{ claude = $ClaudeText; codex = $CodexText }          # the reporter's own scan; the texts go in on standard input, only counts come back
foreach ($Pair in @(@('Claude', 'claude'), @('Codex', 'codex'))) {
    $Found = $Counts.($Pair[1])
    if ($Found -eq 0) { throw ('The {0} text you typed does not match any of our usage files. Nothing was changed. Compare with your agenttalk capacity screen and run step 8 again.' -f $Pair[0]) }
    '{0}: matches {1} usage file(s)' -f $Pair[0], $Found
}
$ToToml = { param($Value) '"' + ([string]$Value).Replace('\', '\\').Replace('"', '\"') + '"' }       # a TOML basic string: only \ and " need escaping
$Rows = @"

[[readings.sources]]
bus_root = $(& $ToToml $BusRoot)
provider = "claude"
agenttalk_account = $(& $ToToml $ClaudeText)
account = $(& $ToToml $ClaudeAlias)

[[readings.sources]]
bus_root = $(& $ToToml $BusRoot)
provider = "codex"
agenttalk_account = $(& $ToToml $CodexText)
account = $(& $ToToml $CodexAlias)
"@
[IO.File]::AppendAllText($ConfigPath, $Rows, $Utf8)           # the file keeps its private list
Assert-PrivateAfter $ConfigPath $true
Remove-Variable ClaudeName, ClaudeHome, CodexName, CodexHome, ClaudeText, CodexText, Rows
'two rows written'
