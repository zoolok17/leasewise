# Step 8 (operator step): the account table. Copy each account's text from your own screen (the line `agenttalk capacity` prints for it) when asked.
# It holds your Windows user name, so the question does NOT show what you type (it is masked), and the text goes only into the private settings file.
# The script first checks that the settings file is private, then asks; it checks the SHAPE of each text and says which one is wrong without showing it,
# and it writes nothing unless both are right.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 8: the account table'
Assert-Private $LiveRoot $true                           # before anything is asked or written
Assert-Private $ConfigPath $true
if ((Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8) -match '\[\[readings\.sources\]\]') { throw 'The account table is already in the settings file. Nothing was changed. Tell the lead.' }
if ($env:LW_SWITCHON_INPUT -ceq 'env') {                 # the lead's test run only (the guard allows it in rehearsal mode and nowhere else)
    $ClaudeText = $env:LW_SWITCHON_CLAUDE_TEXT
    $CodexText = $env:LW_SWITCHON_CODEX_TEXT
}
else {
    $ClaudeText = Read-Host -MaskInput 'Claude account text, as agenttalk capacity prints it (it is not shown as you type)'
    $CodexText = Read-Host -MaskInput 'Codex account text, as agenttalk capacity prints it (it is not shown as you type)'
}
$Shape = '^{0}:.+:home-[0-9a-f]{{8}}$'
if ($ClaudeText -cnotmatch ($Shape -f 'claude')) { throw 'The Claude text is not in the form claude:<user>:home-<8 characters 0-9 a-f>. Nothing was changed. Copy it again.' }
if ($CodexText -cnotmatch ($Shape -f 'codex')) { throw 'The Codex text is not in the form codex:<user>:home-<8 characters 0-9 a-f>. Nothing was changed. Copy it again.' }
if ($ClaudeText -eq $CodexText) { throw 'The two texts are the same. Nothing was changed. Copy them again.' }
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
Remove-Variable ClaudeText, CodexText, Rows
'two rows written'
