# Report Runtime Contract

## Purpose
Контракт для потребителей отчёта (API/UI/аналитика): что является фактом, что является проблемой, как интерпретировать suppression, и какие инварианты обязаны выполняться.

## 1. Observation vs Finding

### `verified_observations[]`
- Это подтверждённые факты из кода/структуры.
- Они описывают **что обнаружено**, но не обязаны быть напрямую actionable.
- Могут существовать без ручного/issue pipeline (автоматический маппинг в findings поддерживается).

### `findings[]`
- Это actionable-слой для принятия решений.
- Находка должна иметь traceability к фактам:
  - `source_observation_ids[]`
  - `supporting_observation_ids[]`
- UI и API должны трактовать findings как основной список проблем.

### `suppression_records[]`
- Это явные причины, почему verified observation не материализовался в finding.
- `suppression_records` устраняют silent drop.

## 2. Status and Lifecycle

Поддерживаемые статусы аудита:
- `initialized`
- `scanning`
- `analyzed`
- `reported`
- `completed`
- `failed`
- `accepted` (legacy alias)

Критичный guard:
- Нельзя публиковать populated report со статусом `initialized`.

## 3. Required Invariants

- `summary.finding_count == len(findings)`
- `summary.verified_observation_count == len(verified_observations)`
- `summary.open_question_count == len(open_questions)`
- `summary.contradiction_count == len(contradictions)`
- `summary.decision_count == len(decisions)`

Coverage invariant:
- Если есть eligible verified observations (допустимый `evidence_class`) и `findings=[]`,
  то обязана быть suppression coverage для этих observation ids.

## 4. Suppression Reason Codes

Разрешённые коды:
- `below_threshold`
- `policy_suppressed`
- `duplicate_of`
- `mapping_metadata_missing`
- `insufficient_structured_fields`

Любой observation без finding и без suppression record считается нарушением контракта.

## 5. Finding Model (current)

Минимально ожидаемые поля finding:
- `finding_id`
- `issue_id`
- `status`
- `title`
- `summary`
- `severity`
- `confidence`
- `impact`
- `recommended_fix`
- `severity_rule_ref`
- `source_observation_ids[]`
- `supporting_observation_ids[]`
- `supporting_evidence_classes[]`
- `supporting_evidence[]`
- `uncertainty`

## 6. Payload Example: findings path

```json
{
  "summary": {
    "finding_count": 2,
    "verified_observation_count": 2
  },
  "findings": [
    {
      "finding_id": "finding_obs_obs_cors_wildcard",
      "issue_id": "derived:obs_cors_wildcard",
      "status": "open",
      "severity": "high",
      "confidence": "high",
      "source_observation_ids": ["obs_cors_wildcard"]
    },
    {
      "finding_id": "finding_obs_obs_jwt_no_verify",
      "issue_id": "derived:obs_jwt_no_verify",
      "status": "open",
      "severity": "high",
      "confidence": "high",
      "source_observation_ids": ["obs_jwt_no_verify"]
    }
  ],
  "suppression_records": []
}
```

## 7. Payload Example: suppression path

```json
{
  "summary": {
    "finding_count": 0,
    "verified_observation_count": 1
  },
  "findings": [],
  "suppression_records": [
    {
      "suppression_id": "sup_obs_001",
      "observation_id": "obs_001",
      "reason_code": "policy_suppressed",
      "reason_detail": "evidence_class_not_allowed:inferred_hypothesis",
      "status": "suppressed"
    }
  ]
}
```

## 8. Failure Modes and Consumer Guidance

### Failure mode: summary mismatch
- Symptom: count fields do not match payload arrays.
- Runtime behavior: report compilation error.
- Consumer action: treat report as invalid, do not cache.

### Failure mode: eligible observations but no findings/suppressions
- Symptom: факты есть, но в problem-layer пусто и без объяснений.
- Runtime behavior: report compilation error.
- Consumer action: block downstream processing.

### Failure mode: unknown audit status
- Runtime behavior: report compilation error.
- Consumer action: reject payload.

## 9. Compatibility Notes

- Новые поля finding добавлены как additive.
- Legacy consumers, которые читают только старые поля (`issue_id`, `summary`, `severity`), продолжают работать.
- Новым consumer-ам рекомендуется опираться на `finding_id`, `source_observation_ids`, `suppression_records`.
