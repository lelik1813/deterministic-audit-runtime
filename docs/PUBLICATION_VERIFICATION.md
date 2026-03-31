# Publication Verification

> Executed: 2026-04-01 | Post-cleanup verification against PUBLICATION_CLEANUP_PLAN.md

## 1. Current Top-Level Public Surface

### Tracked files (git index after cleanup)

```
.gitignore
README.md
cli.py
demo.ps1
demo.sh
demo_single_ws/
demo_workspace/
docs/
config/
prompts/
rules/
runtime/
schema/
tests/
```

### Excluded from tracking (`.gitignore`)

```
.claude/
.pytest_cache/
__pycache__/
_scripts/
_user_reply/
claude_manual_ws/
codex_manual_ws/
test_mini*/
```

### Removed entirely (filesystem deleted)

```
test_mini/
test_mini_20260331_125725/
test_mini_20260331_131011/
test_mini_20260331_155716/
test_mini_20260331_180738/
claude_manual_ws/
codex_manual_ws/
Deterministic Audit Runtime (DAR) repository structure and publication-scope audit.txt
```

### Surface matches plan?

Yes. The current surface matches `docs/PUBLICATION_CLEANUP_PLAN.md` "Proposed Public Repository Surface" with two exceptions noted as recommended (not mandatory):
- `demo_workspace/` and `demo_single_ws/` are at top-level instead of `examples/` (recommended move R1, deferred)
- `demo.sh` and `demo.ps1` are at top-level instead of `examples/` (same)

No unexpected items present.

---

## 2. Demo Fixture Integrity

Both demo workspaces verified after sanitization:

| Check | demo_workspace | demo_single_ws |
|---|---|---|
| Directory structure intact | Yes (9 subdirs) | Yes (9 subdirs) |
| `audit_config.json` valid JSON | Yes | Yes |
| `schema_version` = `1.0.0` | Yes | Yes |
| `policy` = `low_noise` | Yes | Yes |
| `target_repo_path` sanitized | `/path/to/target-repo` | `/path/to/target-repo` |
| `rocki` in any file | No | No |

---

## 3. Local-Path Leakage Check

### Pattern: `rocki`

| Location | Count | Classification |
|---|---|---|
| `docs/PUBLICATION_CLEANUP_PLAN.md` | 7 | Self-referential — describes cleanup. Acceptable. |
| `docs/PUBLICATION_CLEANUP_EXECUTION.md` | 7 | Self-referential — reports what was sanitized. Acceptable. |
| `docs/PUBLICATION_INVENTORY.md` | 3 | Self-referential — describes scripts with hardcoded paths. Acceptable. |
| `_scripts/*.ps1` | 2 | Excluded via `.gitignore`. Not in public surface. |
| `_user_reply/_user_reply.txt` | 1 | Excluded via `.gitignore`. Not in public surface. |
| **Source code, schemas, demo fixtures** | **0** | **Clean.** |

### Pattern: `C:\Users\`

| Location | Count | Classification |
|---|---|---|
| `docs/PUBLICATION_CLEANUP_PLAN.md` | 5 | Self-referential. Acceptable. |
| `docs/PUBLICATION_INVENTORY.md` | 2 | Self-referential. Acceptable. |
| `_scripts/*.ps1` | 2 | Excluded via `.gitignore`. |
| **Source code, schemas, demo fixtures** | **0** | **Clean.** |

### Pattern: `AppData`

| Location | Count | Classification |
|---|---|---|
| `docs/PUBLICATION_CLEANUP_EXECUTION.md` | 2 | Self-referential (path replacement table). Acceptable. |
| **All other files** | **0** | **Clean.** |

### Pattern: `runtime-audit-fixture`

| Location | Count | Classification |
|---|---|---|
| `demo.sh`, `demo.ps1` | 6 | Repo name used in auto-detection logic. Not a local path — it's the name of a sibling demo target repository. Acceptable. |
| `demo_workspace/`, `demo_single_ws/` JSON | 10 | `repo_label: "runtime-audit-fixture"` — metadata field, not a filesystem path. Acceptable. |
| `docs/PUBLICATION_CLEANUP_*.md` | 3 | Self-referential. Acceptable. |
| `.claude/plans/` | 1 | Already removed from git index. |
| `_scripts/*.ps1` | 2 | Excluded via `.gitignore`. |
| **Leakage (actual local path in public surface)** | **0** | **Clean.** |

---

## 4. Verification Result

**PASS.** The public repository surface contains:

- 0 hardcoded local paths (`C:\Users\...`)
- 0 username references (`rocki`) outside self-referential docs and excluded files
- 0 `AppData` references outside self-referential docs
- `runtime-audit-fixture` appears only as a repo name/label, never as a local filesystem path

All mandatory cleanup actions (M1–M7) from `PUBLICATION_CLEANUP_PLAN.md` are verified complete.
