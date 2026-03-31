# demo.ps1 — Reproducible Codex audit demo (Windows)
#
# Runs the full DAR pipeline:
#   init-audit -> snapshot-target -> enqueue-scan -> run-task -> rebuild-state -> compile-report
#
# Requirements:
#   - Python 3.10+
#   - Git
#   - codex CLI (https://github.com/openai/codex)
#   - OPENAI_API_KEY env var set
#
# Usage:
#   .\demo.ps1                              # auto-detects sibling runtime-audit-fixture
#   .\demo.ps1 -TargetRepo C:\path\to\repo  # audits specified repo

param(
    [string]$TargetRepo = "",
    [int]$TimeoutSeconds = 120
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $ProjectRoot

function Step($msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Die($msg) { Write-Host "    $msg" -ForegroundColor Red; exit 1 }

$AuditId = "audit_codex_demo"
$Workspace = "demo_workspace"

# --- Resolve target repo ---
if (-not $TargetRepo) {
    $ParentDir = Split-Path -Parent $ProjectRoot
    foreach ($c in @("$ParentDir\runtime-audit-fixture", "$ParentDir\runtime-audit-fixture-main")) {
        if (Test-Path "$c\.git") { $TargetRepo = $c; break }
    }
}
if (-not $TargetRepo) { Die "No target repo. Pass -TargetRepo or clone runtime-audit-fixture as sibling dir." }
if (-not (Test-Path "$TargetRepo\.git")) { Die "Not a git repo: $TargetRepo" }

$SnapRef = git -C $TargetRepo rev-parse HEAD
Step "Target: $TargetRepo @ $($SnapRef.Substring(0,12))"

# --- Auto-detect scan target ---
$Tracked = @(git -C $TargetRepo ls-files)
$Scan = $null
foreach ($Candidate in @("app", "src", "lib")) {
    if ($Tracked -contains $Candidate) { $Scan = $Candidate; break }
    $Prefix = "$Candidate/"
    if ($Tracked | Where-Object { $_ -like "$Prefix*" } | Select-Object -First 1) { $Scan = $Candidate; break }
}
if (-not $Scan) {
    $FirstPy = $Tracked | Where-Object { $_ -like "*.py" } | Select-Object -First 1
    if ($FirstPy) { $Scan = $FirstPy }
}
if (-not $Scan) { $Scan = $Tracked | Select-Object -First 1 }
if (-not $Scan) { Die "No files found in target repo" }
Step "Scan: $Scan"

# --- Cleanup ---
if (Test-Path $Workspace) { Remove-Item -Recurse -Force $Workspace }

# --- Pipeline ---
Step "1/6: init-audit"
python cli.py init-audit --workspace $Workspace --target-repo $TargetRepo --audit-id $AuditId --title "Codex demo audit" --policy low_noise
if ($LASTEXITCODE -ne 0) { Die "init-audit failed" }
Ok "done"

Step "2/6: snapshot-target"
python cli.py snapshot-target --workspace $Workspace
if ($LASTEXITCODE -ne 0) { Die "snapshot-target failed" }
Ok "done"

Step "3/6: enqueue-scan"
python cli.py enqueue-scan --workspace $Workspace --target-kind path --targets $Scan
if ($LASTEXITCODE -ne 0) { Die "enqueue-scan failed" }
Ok "done"

Step "4/6: run-task (codex)"
python cli.py run-task --workspace $Workspace --backend codex --timeout-seconds $TimeoutSeconds
if ($LASTEXITCODE -ne 0) { Die "run-task failed" }
Ok "done"

Step "5/6: rebuild-state"
python cli.py rebuild-state --workspace $Workspace
if ($LASTEXITCODE -ne 0) { Die "rebuild-state failed" }
Ok "done"

Step "6/6: compile-report"
python cli.py compile-report --workspace $Workspace
if ($LASTEXITCODE -ne 0) { Die "compile-report failed" }
Ok "done"

# --- Verify ---
Write-Host ""
Step "Artifacts:"

$VerifyScript = @"
import json, glob, sys
w = sys.argv[1]
s = json.load(open(w + '/state/canonical_state.json'))
a = s['audit']
print('  audit:         ' + a['id'])
print('  snapshot_ref:  ' + a['current_snapshot_ref'][:12])
print('  observations:  ' + str(len(s.get('observations', {}))))
tq = json.load(open(w + '/state/task_queue.json'))
ts = tq.get('tasks', {})
d = sum(1 for t in ts.values() if t['status'] == 'done')
print('  tasks:         ' + str(d) + ' done / ' + str(len(ts)) + ' total')
for f in glob.glob(w + '/reports/report.*.json'):
    r = json.load(open(f))
    print('  report:        ' + r['report_id'])
    print('  audit_id:      ' + r['source_audit_id'])
"@
python -c $VerifyScript $Workspace 2>$null

Write-Host ""
Ok "Demo complete."
