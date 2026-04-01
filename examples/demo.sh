#!/usr/bin/env bash
# demo.sh — Reproducible Codex audit demo
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
#   ./demo.sh                          # auto-detects sibling runtime-audit-fixture
#   ./demo.sh /path/to/repo            # audits specified repo

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m' CYAN='\033[0;36m' RED='\033[0;31m' NC='\033[0m'
step() { echo -e "${CYAN}▶ $1${NC}"; }
ok()   { echo -e "${GREEN}  ✓ $1${NC}"; }
die()  { echo -e "${RED}  ✗ $1${NC}"; exit 1; }

AUDIT_ID="audit_codex_demo"
WORKSPACE="demo_workspace"
TIMEOUT=120

# --- Resolve target repo ---
TARGET_REPO="${1:-}"
if [ -z "$TARGET_REPO" ]; then
    PARENT="$(dirname "$SCRIPT_DIR")"
    for c in "$PARENT/runtime-audit-fixture" "$PARENT/runtime-audit-fixture-main"; do
        [ -d "$c/.git" ] && TARGET_REPO="$c" && break
    done
fi
[ -z "$TARGET_REPO" ] && die "No target repo. Pass path or clone runtime-audit-fixture as sibling."
[ ! -d "$TARGET_REPO/.git" ] && die "Not a git repo: $TARGET_REPO"

SNAP=$(git -C "$TARGET_REPO" rev-parse HEAD)
step "Target: $TARGET_REPO @ ${SNAP:0:12}"

# --- Auto-detect scan target ---
SCAN=""
for c in app src lib main.py app.py server.py; do
    if git -C "$TARGET_REPO" ls-tree -r HEAD --name-only -- "$c" 2>/dev/null | grep -q .; then
        SCAN="$c"; break
    fi
done
if [ -z "$SCAN" ]; then
    SCAN=$(git -C "$TARGET_REPO" ls-tree -r HEAD --name-only | grep '\.py$' | head -1)
fi
[ -z "$SCAN" ] && SCAN=$(git -C "$TARGET_REPO" ls-tree -r HEAD --name-only | head -1)
[ -z "$SCAN" ] && die "No files in target repo"
step "Scan: $SCAN"

# --- Cleanup ---
[ -d "$WORKSPACE" ] && rm -rf "$WORKSPACE"

# --- Pipeline ---
step "1/6: init-audit"
python cli.py init-audit \
    --workspace "$WORKSPACE" \
    --target-repo "$TARGET_REPO" \
    --audit-id "$AUDIT_ID" \
    --title "Codex demo audit" \
    --policy low_noise || die "init-audit failed"
ok "done"

step "2/6: snapshot-target"
python cli.py snapshot-target --workspace "$WORKSPACE" || die "snapshot-target failed"
ok "done"

step "3/6: enqueue-scan"
python cli.py enqueue-scan --workspace "$WORKSPACE" --target-kind path --targets "$SCAN" || die "enqueue-scan failed"
ok "done"

step "4/6: run-task (codex)"
python cli.py run-task --workspace "$WORKSPACE" --backend codex --timeout-seconds "$TIMEOUT" || die "run-task failed"
ok "done"

step "5/6: rebuild-state"
python cli.py rebuild-state --workspace "$WORKSPACE" || die "rebuild-state failed"
ok "done"

step "6/6: compile-report"
python cli.py compile-report --workspace "$WORKSPACE" || die "compile-report failed"
ok "done"

# --- Verify ---
echo ""
step "Artifacts:"
python -c "
import json, glob
s = json.load(open('$WORKSPACE/state/canonical_state.json'))
a = s['audit']
print(f'  audit:         {a[\"id\"]}')
print(f'  snapshot_ref:  {a[\"current_snapshot_ref\"][:12]}')
print(f'  observations:  {len(s.get(\"observations\",{}))}')
print(f'  questions:     {len(s.get(\"questions\",{}))}')
tq = json.load(open('$WORKSPACE/state/task_queue.json'))
ts = tq.get('tasks', {})
d = sum(1 for t in ts.values() if t['status'] == 'done')
print(f'  tasks:         {d} done / {len(ts)} total')
for f in glob.glob('$WORKSPACE/reports/report.*.json'):
    r = json.load(open(f))
    print(f'  report:        {r[\"report_id\"]}')
    print(f'  audit_id:      {r[\"source_audit_id\"]}')
"

echo ""
ok "Demo complete."
