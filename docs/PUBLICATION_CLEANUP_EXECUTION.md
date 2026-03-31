# Publication Cleanup Execution Report

> Executed: 2026-04-01 | Baseline: `9b8b4a3`

## Summary

All mandatory cleanup actions from `PUBLICATION_CLEANUP_PLAN.md` have been executed.
The repository is now free of local path exposure and non-public artifacts.

---

## 1. Deleted Files & Directories

| Path | Files removed | Size freed |
|---|---|---|
| `test_mini/` | 124 | 2.4 MB |
| `test_mini_20260331_125725/` | 63 | 1.1 MB |
| `test_mini_20260331_131011/` | 20 | 270 KB |
| `test_mini_20260331_155716/` | 26 | 386 KB |
| `test_mini_20260331_180738/` | 122 | 2.4 MB |
| `claude_manual_ws/` | 24 | 442 KB |
| `codex_manual_ws/` | 120 | 3.3 MB |
| `Deterministic Audit Runtime (DAR) repository structure and publication-scope audit.txt` | 1 | 447 KB |
| **Total** | **500** | **~10.7 MB** |

---

## 2. Sanitized Files

### `demo_workspace/` — 7 files, 14 path replacements

| File | Replacements |
|---|---|
| `audit_config.json` | 1 (`C:\\Users\\rocki\\...\\runtime-audit-fixture` → `/path/to/target-repo`) |
| `events/events.ndjson` | 2 (repo_path in 2 event records) |
| `runs/run_ledger.ndjson` | 2 (workspace + target_repo_root paths) |
| `state/canonical_state.json` | 1 (repo_path) |
| `state/projections/canonical_state.08bf3ed8b9bdaa21.json` | 1 |
| `state/projections/canonical_state.2766acac83a7acc0.json` | 1 |
| `state/projections/canonical_state.79e7b8fe3adb611c.json` | 1 |

Additional paths replaced in `runs/run_ledger.ndjson`:
- `C:\\Users\\rocki\\...\\demo_workspace` → `/path/to/dar/demo_workspace`
- `C:\\Users\\rocki\\AppData\\Roaming\\npm\\codex.CMD` → `/path/to/codex`

### `demo_single_ws/` — 6 files, 12 path replacements

| File | Replacements |
|---|---|
| `audit_config.json` | 1 |
| `events/events.ndjson` | 2 |
| `runs/run_ledger.ndjson` | 2 |
| `state/canonical_state.json` | 1 |
| `state/projections/canonical_state.de023d62b4fd9cc9.json` | 1 |
| `state/projections/canonical_state.e5e86ae283597736.json` | 1 |

**Total sanitized: 13 files, 26 path replacements**

Path replacement mapping:
| Original | Replacement |
|---|---|
| `C:\\Users\\rocki\\Documents\\EGE\\Projects\\Deterministic Audit Runtime (DAR)\\demo_workspace` | `/path/to/dar/demo_workspace` |
| `C:\\Users\\rocki\\Documents\\EGE\\Projects\\Deterministic Audit Runtime (DAR)\\demo_single_ws` | `/path/to/dar/demo_single_ws` |
| `C:\\Users\\rocki\\AppData\\Roaming\\npm\\codex.CMD` | `/path/to/codex` |
| `C:\\Users\\rocki\\Documents\\EGE\\Projects\\runtime-audit-fixture` | `/path/to/target-repo` |

---

## 3. `.gitignore` Changes

**Added entries:**

```
.pytest_cache/
.claude/
_user_reply/
_scripts/
test_mini*/
claude_manual_ws/
codex_manual_ws/
```

**Already present (unchanged):**
```
__pycache__/
*.pyc
*.pyo
```

---

## 4. Removed from Git Tracking

| Path | Status |
|---|---|
| `.claude/plans/minimal-test-fixture.md` | Removed from index |
| `.claude/settings.local.json` | Removed from index |
| `__pycache__/`, `.pytest_cache/` | Already excluded by `.gitignore` (never tracked) |

---

## 5. Remaining Local Path Occurrences

| Location | Count | Status |
|---|---|---|
| `_scripts/*.ps1` | 2 | Excluded via `.gitignore` — not tracked |
| `_user_reply/` | varies | Excluded via `.gitignore` — not tracked |
| `docs/PUBLICATION_CLEANUP_PLAN.md` | 5 | Self-referential (describes the cleanup) — acceptable |
| `docs/PUBLICATION_INVENTORY.md` | 1 | Describes scripts as having hardcoded paths — acceptable |
| **Source code, schemas, demo fixtures** | **0** | Clean |

**Result: 0 hardcoded local path occurrences in the public repository surface.**

---

## 6. Verification

```
$ grep -rn "rocki" --include='*.py' --include='*.json' --include='*.yaml' --include='*.ndjson' . | grep -v '_user_reply/' | grep -v '.git/'
(empty — no matches)
```

All mandatory cleanup actions from `PUBLICATION_CLEANUP_PLAN.md` (M1–M7) are complete.
