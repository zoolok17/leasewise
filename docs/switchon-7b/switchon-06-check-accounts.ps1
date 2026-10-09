# Step 6: prove step 5. It only reads the store (read-only) and asks the question Leasewise itself asks when a reading arrives:
# does this machine have a seat in a project that has the account? It stops at the first account where the answer is no.
. (Join-Path $PSScriptRoot 'switchon-common.ps1')
'== Step 6: this machine is an allowed reporter for both accounts'
$Question = @'
import sqlite3, sys
store, machine = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("file:" + store + "?mode=ro", uri=True)
bad = 0
for alias in sys.argv[3:]:
    row = conn.execute("SELECT 1 FROM seats s JOIN machines m ON m.id = s.machine_id JOIN teams t ON t.id = s.team_id "
                       "JOIN project_accounts pa ON pa.project_id = t.project_id JOIN accounts a ON a.id = pa.account_id "
                       "WHERE m.alias = ? AND a.alias = ? LIMIT 1", (machine, alias)).fetchone()
    print(alias + ": " + ("allowed reporter" if row else "NOT an allowed reporter"))
    bad += row is None
sys.exit(1 if bad else 0)
'@
& $Py @PyFlags -c $Question $Store $MachineAlias $ClaudeAlias $CodexAlias
if ($LASTEXITCODE -ne 0) { throw 'This machine is not an allowed reporter for both accounts. Do not go on. Tell the lead.' }
'both accounts: allowed'
