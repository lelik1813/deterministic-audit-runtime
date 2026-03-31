$ErrorActionPreference = "Stop"

$TargetRepo = "C:\Users\rocki\Documents\EGE\Projects\runtime-audit-fixture"
$Workspace = "claude_manual_ws"
$AuditId = "audit_claude_manual"
$Title = "Manual Claude test"
$Policy = "low_noise"
$TimeoutSeconds = 300

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $ProjectRoot

if (-not (Test-Path $TargetRepo)) {
    throw "Target repo not found: $TargetRepo"
}

$GitStatus = git -C $TargetRepo status --porcelain
if ($LASTEXITCODE -ne 0) {
    throw "Failed to run git status for target repo: $TargetRepo"
}
if ($GitStatus) {
    throw "Target repo working tree is not clean: $TargetRepo"
}

$TrackedFiles = @(git -C $TargetRepo ls-files)
if ($LASTEXITCODE -ne 0 -or $TrackedFiles.Count -eq 0) {
    throw "Failed to enumerate tracked files in target repo"
}

$PreferredTargets = @(
    "app",
    "src",
    "inventory",
    "main.py",
    "app.py",
    "server.py"
)

$SelectedTarget = $null

foreach ($Candidate in $PreferredTargets) {
    if ($TrackedFiles -contains $Candidate) {
        $SelectedTarget = $Candidate
        break
    }

    $Prefix = $Candidate.TrimEnd("/") + "/"
    if ($TrackedFiles | Where-Object { $_ -like "$Prefix*" } | Select-Object -First 1) {
        $SelectedTarget = $Candidate
        break
    }
}

if (-not $SelectedTarget) {
    $FirstPythonFile = $TrackedFiles | Where-Object { $_ -like "*.py" } | Select-Object -First 1
    if ($FirstPythonFile) {
        $SelectedTarget = $FirstPythonFile
    }
}

if (-not $SelectedTarget) {
    $SelectedTarget = $TrackedFiles | Select-Object -First 1
}

Write-Host "Selected target for enqueue-scan: $SelectedTarget"

if (Test-Path $Workspace) {
    Remove-Item -Recurse -Force $Workspace
}

python cli.py init-audit --workspace $Workspace --target-repo $TargetRepo --audit-id $AuditId --title $Title --policy $Policy
if ($LASTEXITCODE -ne 0) { throw "init-audit failed" }

python cli.py snapshot-target --workspace $Workspace
if ($LASTEXITCODE -ne 0) { throw "snapshot-target failed" }

python cli.py enqueue-scan --workspace $Workspace --target-kind path --targets $SelectedTarget
if ($LASTEXITCODE -ne 0) { throw "enqueue-scan failed" }

python cli.py run-task --workspace $Workspace --backend claude --timeout-seconds $TimeoutSeconds
if ($LASTEXITCODE -ne 0) { throw "first run-task failed" }

python cli.py run-task --workspace $Workspace --backend claude --timeout-seconds $TimeoutSeconds
if ($LASTEXITCODE -ne 0) { throw "second run-task failed" }

python cli.py rebuild-state --workspace $Workspace
if ($LASTEXITCODE -ne 0) { throw "rebuild-state failed" }

python cli.py compile-report --workspace $Workspace
if ($LASTEXITCODE -ne 0) { throw "compile-report failed" }
