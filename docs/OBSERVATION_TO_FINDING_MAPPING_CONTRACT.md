# Observation -> Finding Mapping Contract (Step 1)

## Purpose
Формализовать правило, при котором `verified_observations[]` преобразуются в `findings[]`, и запретить silent drop фактов.

## Core invariant
Каждый `verified_observation` обязан получить ровно один исход:
- `mapped_to_finding`
- `suppressed`
- `duplicate_collapsed`
- `filtered`

Если исход не определён, отчёт считается невалидным.

## Definitions
- `verified_observation`: подтверждённый факт с `status=verified`.
- `eligible observation`: verified observation, удовлетворяющий policy для materialization в finding.
- `finding`: actionable запись риска/проблемы для потребителя отчёта.
- `suppression`: явное решение не создавать finding с кодом причины.

## Input contract
Для маппинга используются только:
- `verified_observations[]`
- rule/policy metadata (severity thresholds, suppress/allow lists, dedup rules)
- snapshot context (`source_audit_id`, `current_snapshot_ref`)

LLM/эвристическое пост-обогащение не является частью Step 1.

## Output contract
Результат маппинга должен быть полным:
- `findings[]`
- `suppression_records[]` (или эквивалентная структура)
- `mapping_summary`:
  - `verified_total`
  - `mapped_total`
  - `suppressed_total`
  - `duplicate_collapsed_total`
  - `filtered_total`

Требование консистентности:
`verified_total = mapped_total + suppressed_total + duplicate_collapsed_total + filtered_total`

## Condition -> finding_status

| Condition | finding_status | Required artifact |
|---|---|---|
| Observation is `verified` and rule is risk-eligible | `open` (mapped) | finding record |
| Observation verified but below policy threshold | `filtered` | suppression record (`below_threshold`) |
| Observation verified and explicitly suppressed by policy | `suppressed` | suppression record (`policy_suppressed`) |
| Observation semantically duplicates an already mapped observation | `duplicate_collapsed` | link to canonical finding (`canonical_finding_id`) |
| Observation cannot be mapped because required rule metadata missing | `suppressed` | suppression record (`mapping_metadata_missing`) + pipeline warning |

## Required reasons when finding is not created
Allowed reason codes:
- `below_threshold`
- `policy_suppressed`
- `duplicate_of`
- `mapping_metadata_missing`
- `insufficient_structured_fields`

Любой иной код запрещён до расширения контракта.

## Minimal finding fields for Step 1
До schema upgrade (Step 2) finding может быть минимальным, но обязан содержать:
- `finding_id`
- `source_observation_ids[]`
- `status`
- `statement`
- `snapshot_ref`

## Determinism requirements
- Один и тот же input + policy metadata => идентичный output.
- Порядок findings стабилен (stable sort key):
  1. severity rank (if available)
  2. `observation_id`

## Validation rules
- Запрещено публиковать отчёт, где `verified_observation_count > 0` и при этом:
  - `findings[]` пуст и
  - `suppression_records[]` пуст.
- Запрещено silent drop любого verified observation.

## Walkthrough examples (required by Step 1)

### Example A: high-risk verified fact
- Input: `jwt.decode(... verify_signature=False)` verified by deterministic pattern.
- Expected: mapped to finding with `status=open`.

### Example B: low-risk verified fact below threshold
- Input: verified style/config issue with policy threshold higher than observed risk.
- Expected: no finding, suppression reason `below_threshold`.

### Example C: policy suppression
- Input: verified fact in path covered by accepted exception policy.
- Expected: no finding, suppression reason `policy_suppressed`.

## Failure policy
- Если mapping outcome для любого verified observation не определён:
  - report status must move to failed state (implementation detail in later steps),
  - публикация отчёта блокируется.
