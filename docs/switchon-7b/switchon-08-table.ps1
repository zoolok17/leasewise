# Step 8 (operator step): the account table. Copy each account's text from your own screen (the line `agenttalk capacity` prints for it) when
# asked; it holds your Windows user name, so it goes only into the private settings file. It is never printed, logged or sent anywhere.
# The script checks the SHAPE of each text and says which one is wrong without showing it; it writes nothing unless both are right.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 8: the account table'
if ((Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8) -match '\[\[readings\.sources\]\]') { throw 'The account table is already in the settings file. Nothing was changed. Tell the lead.' }
$ClaudeText = if ($env:LW_SWITCHON_CLAUDE_TEXT) { $env:LW_SWITCHON_CLAUDE_TEXT } else { Read-Host 'Claude account text, as agenttalk capacity prints it' }
$CodexText = if ($env:LW_SWITCHON_CODEX_TEXT) { $env:LW_SWITCHON_CODEX_TEXT } else { Read-Host 'Codex account text, as agenttalk capacity prints it' }
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
Assert-Private $ConfigPath $true
Remove-Variable ClaudeText, CodexText, Rows
'two rows written'
