# Report Schema Migration Notes (Step 2)

## Scope
Step 2 introduces additive finding fields for actionable reporting without changing mapping logic.

## Added finding fields
- `finding_id` (stable finding identity; defaults to `issue_id` for backward compatibility)
- `source_observation_ids[]` (traceability alias; mirrors supporting observation ids)
- `confidence` (`low|medium|high`, default: `medium`)
- `impact` (default: issue summary)
- `recommended_fix` (`string|null`, default: `null`)

## Backward compatibility strategy
- Existing fields are preserved (`issue_id`, `supporting_observation_ids`, etc.).
- New fields are additive, so existing consumers that ignore unknown fields continue to work.
- Runtime defaults guarantee fields are always present in new reports.

## Notes on schema versioning
- Runtime top-level `schema_version` is unchanged in this step.
- Step 2 focuses on model/schema definition and additive field introduction.
- A dedicated version bump can be performed later when strict report.schema enforcement is introduced in pipeline gates.

## Risk
- Low migration risk (additive only).
- Potential consumer assumptions about exact finding key set should be validated.

## Validation expectation
- Compiled reports include all new fields in each finding.
- Existing tests relying on legacy fields still pass.
