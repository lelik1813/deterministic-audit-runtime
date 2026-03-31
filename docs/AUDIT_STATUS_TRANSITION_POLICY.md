# Audit Status Transition Policy (Step 5)

## Canonical transition map
- `initialized -> scanning`
- `scanning -> analyzed`
- `analyzed -> reported`
- `reported -> completed`
- `* -> failed`

## Backward-compatible alias
- `accepted` is currently treated as a valid status in runtime for compatibility with existing states.

## Report emission guard
- A populated report (`findings` or `verified_observations` or `open_questions` or `contradictions` or `decisions`) MUST NOT be emitted while `audit.status == "initialized"`.

## Allowed statuses recognized by report compiler
- `initialized`
- `scanning`
- `analyzed`
- `reported`
- `completed`
- `failed`
- `accepted` (legacy alias)

## Failure behavior
- Unknown status -> report compilation error.
- Populated report with `initialized` -> report compilation error.
