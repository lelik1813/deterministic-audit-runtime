# Deterministic Audit Runtime (DAR)

Verification-first, event-sourced audit runtime with reproducible traces.

## Quick demo

```bash
# Prerequisites: Python 3.10+, Git, codex CLI, OPENAI_API_KEY
./demo.sh                          # Linux/macOS
.\demo.ps1                         # Windows PowerShell
```

The demo script runs the full audit pipeline with zero manual steps:

1. **init-audit** — creates workspace, loads policy
2. **snapshot-target** — captures deterministic git snapshot
3. **enqueue-scan** — auto-detects target files
4. **run-task** — executes Codex backend, produces observations
5. **rebuild-state** — rebuilds canonical state from event log
6. **compile-report** — generates final audit report

### What it produces

```
demo_workspace/
├── audit_config.json          # audit metadata + policy binding
├── state/
│   ├── canonical_state.json   # deterministic projection of accepted events
│   ├── task_queue.json        # task lifecycle (pending → running → done)
│   └── slices/                # worker input snapshots (replayable)
├── events/
│   └── events.ndjson          # append-only event log (source of truth)
├── runs/
│   └── run_ledger.ndjson      # execution traces with digests
└── reports/
    └── report.<audit_id>.json # final audit report
```

### Verified postconditions

| Check | What it proves |
|-------|----------------|
| Canonical state has snapshot_ref | Deterministic snapshot bound to git HEAD |
| At least 1 observation accepted | Backend produced real findings |
| At least 1 task reached `done` | Task lifecycle works end-to-end |
| Report file exists | Full pipeline completed |
| Event log has entries | Event-sourced audit trail is intact |

## Architecture

```
Target Repo (git)
     │
     ▼
┌─────────────────┐     ┌──────────────────┐
│  CLI (cli.py)    │────▶│  Slice Builder   │──▶ Worker Input (JSON slice)
│  Orchestrator    │     └──────────────────┘
│                  │────▶┌──────────────────┐     ┌──────────────────┐
│                  │     │  Backend Adapter │────▶│  Codex / Claude   │
│                  │     │  (selector.py)   │     │  Worker Backend   │
│                  │     └──────────────────┘     └──────────────────┘
│                  │────▶┌──────────────────┐
│                  │     │  Event Processor │──▶ Append-only Event Log
│                  │     └──────────────────┘
│                  │────▶┌──────────────────┐
│                  │     │  State Rebuilder │──▶ Canonical State (projection)
│                  │     └──────────────────┘
│                  │────▶┌──────────────────┐
│                  │     │ Report Compiler  │──▶ Audit Report (JSON)
│                  │     └──────────────────┘
└─────────────────┘
```

### Key guarantees

- **Verification-first**: Every observation requires source-bound evidence
- **Event-sourced**: Canonical state is a deterministic projection of the append-only event log
- **Reproducible**: Same snapshot + same worker output = same audit state
- **No implicit fallback**: Backend selection is explicit (`--backend codex` or `--backend claude`)

## Smoke test

```bash
python -m pytest tests/test_codex_demo_smoke.py -v
```

Runs the full pipeline with mocked Codex executor. No API key needed.
