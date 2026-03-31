# Publication Cleanup & Public-Boundary Plan

> Generated: 2026-04-01 | Baseline: `9b8b4a3`

## Classification Legend

| Class | Meaning |
|---|---|
| `publish_as_is` | Ship without changes |
| `sanitize_before_publish` | Contains local paths or sensitive data; clean before shipping |
| `move_to_examples` | Useful as example but not a top-level directory |
| `exclude_from_public_surface` | Internal/tooling; add to `.gitignore` |
| `remove_before_publication` | Delete from repo before publishing |
| `publish_but_mark_experimental` | Ship with an experimental/preview disclaimer |

---

## Per-Path Classification

### Mandatory Cleanup (remove or exclude before publish)

| # | Path | Classification | Rationale | Action | Risk if kept public |
|---|---|---|---|---|---|
| 1 | `test_mini/` | `remove_before_publication` | Generated run output, not a fixture. Contains 607 hardcoded local path references (`C:\Users\rocki\...`). | Delete entire directory | Exposes local filesystem structure and username |
| 2 | `test_mini_20260331_125725/` | `remove_before_publication` | Timestamped run debris. Contains local paths. | Delete entire directory | Same as above |
| 3 | `test_mini_20260331_131011/` | `remove_before_publication` | Timestamped run debris. Contains local paths. | Delete entire directory | Same as above |
| 4 | `test_mini_20260331_155716/` | `remove_before_publication` | Timestamped run debris. Contains local paths. | Delete entire directory | Same as above |
| 5 | `test_mini_20260331_180738/` | `remove_before_publication` | Timestamped run debris. Contains local paths. | Delete entire directory | Same as above |
| 6 | `claude_manual_ws/` | `remove_before_publication` | Manual test output with 590+ local path references. Not a clean example. | Delete entire directory | Exposes local paths, clutter |
| 7 | `codex_manual_ws/` | `remove_before_publication` | Manual test output (3.3 MB), 120+ files of projections/slices. | Delete entire directory | Large, noisy, exposes paths |
| 8 | `_user_reply/` | `exclude_from_public_surface` | Agent instruction files for internal workflow. | Add to `.gitignore`; keep locally | Confusing to public users, not part of the project |
| 9 | `.claude/` | `exclude_from_public_surface` | Claude Code session settings (`settings.local.json`, plans). | Add to `.gitignore`; keep locally | Exposes tool configuration |
| 10 | `_scripts/` | `exclude_from_public_surface` | Contains hardcoded `C:\Users\rocki\...` paths pointing to a local fixture repo. Developer-only scripts. | Add to `.gitignore`; keep locally or rewrite paths with `$env:DAR_TARGET_REPO` | Exposes username and local directory structure |
| 11 | `Deterministic Audit Runtime (DAR) repository structure and publication-scope audit.txt` | `remove_before_publication` | One-time 447 KB analysis dump. Not part of the project. | Delete file | Confusing filename with spaces; not useful |
| 12 | `.pytest_cache/` | `exclude_from_public_surface` | Test runner cache. Should not be tracked. | Add to `.gitignore`; remove from tracking | Unnecessary noise |
| 13 | `__pycache__/` (all levels) | `exclude_from_public_surface` | Python bytecode cache already partially in `.gitignore` but committed in baseline. | Add glob to `.gitignore`; `git rm -r --cached` all `__pycache__/` | Binary artifacts, noise |

### Requires Sanitization (clean before publish)

| # | Path | Classification | Rationale | Action | Risk if kept as-is |
|---|---|---|---|---|---|
| 14 | `demo_workspace/` | `sanitize_before_publish` | Clean demo fixture but contains 17 hardcoded `C:\Users\rocki\...` references in `audit_config.json`, `events.ndjson`, `canonical_state.json`. | Replace local paths with placeholder (e.g. `/path/to/target-repo`) or regenerate with clean workspace | Exposes username and local paths |
| 15 | `demo_single_ws/` | `sanitize_before_publish` | Same issue as `demo_workspace/` — 17 local path references. | Same sanitization | Same |

### Structural Changes

| # | Path | Classification | Rationale | Action | Risk if kept as-is |
|---|---|---|---|---|---|
| 16 | `demo_workspace/` | `move_to_examples` | Better organized under an `examples/` directory. | Move to `examples/demo_workspace/` | Top-level clutter; inconsistent naming |
| 17 | `demo_single_ws/` | `move_to_examples` | Same rationale. | Move to `examples/demo_single_ws/` | Same |
| 18 | `demo.sh` | `move_to_examples` | Demo launcher; belongs with examples. | Move to `examples/` or `examples/demo.sh` | Misplaced at root |
| 19 | `demo.ps1` | `move_to_examples` | Same. | Move to `examples/` or `examples/demo.ps1` | Same |

### Publish with Experimental Marker

| # | Path | Classification | Rationale | Action | Risk if kept as-is |
|---|---|---|---|---|---|
| 20 | `runtime/adapters/codex_adapter.py` (1591 lines) | `publish_but_mark_experimental` | Codex adapter is less mature than Claude SDK adapter. Add experimental note in module docstring. | Add `# EXPERIMENTAL` marker in module docstring | Users may assume equal stability |

### Publish As-Is

| # | Path | Classification | Notes |
|---|---|---|---|
| 21 | `runtime/` (core modules) | `publish_as_is` | 30+ `.py` files, 1.6 MB. Core IP. No local paths found. |
| 22 | `cli.py` | `publish_as_is` | 1140 lines, entry point. No local paths. |
| 23 | `schema/` | `publish_as_is` | 7 JSON schemas, API contracts. |
| 24 | `tests/` | `publish_as_is` | 36 test files + 1 golden fixture. No local paths. |
| 25 | `config/policies.yaml` | `publish_as_is` | Policy definitions. |
| 26 | `rules/transition_rules.yaml` | `publish_as_is` | State machine rules. |
| 27 | `prompts/` | `publish_as_is` | 4 worker prompt templates. |
| 28 | `README.md` | `publish_as_is` | Will be rewritten separately (DOC-01). |
| 29 | `.gitignore` | `publish_as_is` | Needs updates (see below). |
| 30 | `docs/adr/001-claude-sdk-integration-boundary.md` | `publish_as_is` | Architecture decision record. |
| 31 | `docs/AUDIT_STATUS_TRANSITION_POLICY.md` | `publish_as_is` | Status transition spec. |
| 32 | `docs/OBSERVATION_TO_FINDING_MAPPING_CONTRACT.md` | `publish_as_is` | Mapping contract. |
| 33 | `docs/REPORT_RUNTIME_CONTRACT.md` | `publish_as_is` | Report contract. |
| 34 | `docs/v1_2_invariants.md` | `publish_as_is` | Version invariants. |
| 35 | `docs/non_goals.md` | `publish_as_is` | Explicit non-goals. |
| 36 | `docs/REPORT_SCHEMA_MIGRATION_NOTES_STEP2.md` | `publish_as_is` | Migration notes. Internal but harmless. |
| 37 | `docs/TODO_VERIFIED_OBSERVATIONS_TO_FINDINGS.md` | `publish_as_is` | All steps marked Completed. Useful process record. |

---

## Proposed `.gitignore` Updates

Add these entries to `.gitignore`:

```gitignore
# --- Start of additions ---

# Claude Code session data
.claude/

# Internal agent instruction files
_user_reply/

# Developer scripts with local paths (keep locally, exclude from repo)
_scripts/

# Generated workspace artifacts (runtime output)
test_mini*/
claude_manual_ws/
codex_manual_ws/

# Ensure these are properly excluded (some committed in baseline)
__pycache__/
*.pyc
*.pyo
.pytest_cache/
```

After updating `.gitignore`, run:
```bash
git rm -r --cached .claude/ _user_reply/ _scripts/ .pytest_cache/
git rm -r --cached '*/__pycache__/' '__pycache__/'
git rm -r --cached test_mini/ test_mini_20260331_*/
git rm -r --cached claude_manual_ws/ codex_manual_ws/
git rm --cached 'Deterministic Audit Runtime (DAR) repository structure and publication-scope audit.txt'
```

---

## Proposed Public Repository Surface

```
DAR/
├── cli.py                          # Entry point
├── README.md                       # Project overview
├── .gitignore                      # Standard + project-specific excludes
│
├── runtime/                        # Core runtime
│   ├── adapters/                   # Backend abstraction (Claude SDK, Codex)
│   ├── validators/                 # Multi-pass validation pipeline
│   ├── repair/                     # Auto-repair layer
│   ├── workers/                    # Audit pipeline stages
│   └── *.py                        # Core modules (~20)
│
├── schema/                         # JSON schemas (API contracts)
│   ├── audit.schema.json
│   ├── candidate.schema.json
│   ├── event.schema.json
│   ├── report.schema.json
│   ├── worker_input.schema.json
│   ├── worker_output.schema.json
│   └── codex_transport_output.schema.json
│
├── tests/                          # Test suite
│   ├── fixtures/
│   │   └── golden_report_security_mini.json
│   └── test_*.py                   # 36 test files
│
├── config/
│   └── policies.yaml               # Policy profiles
├── rules/
│   └── transition_rules.yaml       # State machine rules
├── prompts/
│   ├── reader.md
│   ├── candidate_generator.md
│   ├── verifier.md
│   └── issue_composer.md
│
├── examples/                       # (new directory)
│   ├── demo_workspace/             # Full demo (sanitized)
│   ├── demo_single_ws/             # Single-worker demo (sanitized)
│   ├── demo.sh                     # Demo launcher
│   └── demo.ps1                    # Demo launcher (PowerShell)
│
└── docs/
    ├── ADR/001-claude-sdk-integration-boundary.md
    ├── AUDIT_STATUS_TRANSITION_POLICY.md
    ├── OBSERVATION_TO_FINDING_MAPPING_CONTRACT.md
    ├── REPORT_RUNTIME_CONTRACT.md
    ├── REPORT_SCHEMA_MIGRATION_NOTES_STEP2.md
    ├── TODO_VERIFIED_OBSERVATIONS_TO_FINDINGS.md
    ├── non_goals.md
    └── v1_2_invariants.md
```

---

## Cleanup Actions by Priority

### Mandatory (must do before publish)

| # | Action | Impact |
|---|---|---|
| M1 | Delete `test_mini/`, `test_mini_20260331_*/` (5 dirs, ~8.1 MB) | Removes 607+ local path references |
| M2 | Delete `claude_manual_ws/`, `codex_manual_ws/` (2 dirs, ~3.7 MB) | Removes 590+ local path references |
| M3 | Delete `Deterministic Audit Runtime (DAR) repository structure and publication-scope audit.txt` | Removes 447 KB analysis dump |
| M4 | Sanitize `demo_workspace/` and `demo_single_ws/` — replace `C:\Users\rocki\...` with `/path/to/target-repo` | Removes 34 local path references |
| M5 | Update `.gitignore` to exclude `.claude/`, `_user_reply/`, `_scripts/`, `.pytest_cache/`, `test_mini*/`, `claude_manual_ws/`, `codex_manual_ws/` | Prevents re-introduction |
| M6 | `git rm -r --cached` all `__pycache__/` directories (~1.4 MB) | Removes committed bytecode |
| M7 | `git rm -r --cached .pytest_cache/` | Removes committed test cache |

### Recommended (should do before publish)

| # | Action | Impact |
|---|---|---|
| R1 | Move `demo_workspace/`, `demo_single_ws/`, `demo.sh`, `demo.ps1` to `examples/` | Cleaner top-level |
| R2 | Add `# EXPERIMENTAL` note to `runtime/adapters/codex_adapter.py` docstring | Sets correct expectations |
| R3 | Commit `docs/PUBLICATION_INVENTORY.md`, `docs/DOCUMENTATION_PLAN.md`, `docs/PUBLICATION_CLEANUP_PLAN.md` | Preserve analysis artifacts |

### Optional (nice to have)

| # | Action | Impact |
|---|---|---|
| O1 | Rewrite `_scripts/` with environment-variable paths and re-include in repo | Makes scripts reusable by others |
| O2 | Add `CONTRIBUTING.md` (DOC-09) | Standard for open source |
| O3 | Add `CHANGELOG.md` (DOC-11) | Version tracking |

---

## Docs References Affected by Removals

| Removed item | Referencing doc | Action needed |
|---|---|---|
| `claude_manual_ws/`, `codex_manual_ws/` | None found in docs | None |
| `test_mini/`, `test_mini_20260331_*/` | None found in docs | None |
| `_scripts/` | None found in docs | None |
| `demo_workspace/` → `examples/demo_workspace/` | `README.md` may reference demo paths | Update path references in README after rewrite |
| `demo.sh`, `demo.ps1` → `examples/` | `README.md` may reference | Update path references |

No docs currently reference any of the removed directories, so cleanup impact on documentation is minimal. The only path updates needed are in `README.md` (which will be rewritten anyway as DOC-01).

---

## Risk Summary

| Risk | Severity | Mitigation |
|---|---|---|
| Username `rocki` exposed in workspace artifacts | **High** | M1, M2, M4 — remove/sanitize all workspaces with local paths |
| Local path `C:\Users\rocki\Documents\EGE\Projects\runtime-audit-fixture` in configs | **High** | M4 — sanitize demo fixtures |
| Local path in `_scripts/` | **Medium** | M5 — exclude from repo (or O1 — rewrite with env vars) |
| 10 MB+ of generated run artifacts polluting repo | **Medium** | M1, M2, M6, M7 — remove and gitignore |
| `.claude/settings.local.json` exposing tool config | **Low** | M5 — gitignore |
| Codex adapter assumed stable | **Low** | R2 — mark experimental |

Total paths to remove/exclude: **15 items**
Total local path references to sanitize: **~1 260 occurrences** across 7 directories (removed entirely) + **34 occurrences** in 2 demo fixtures (sanitized).
