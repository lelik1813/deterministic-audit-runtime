# TODO: Close Semantic Gap `verified_observations -> findings`

## Goal
Убрать разрыв между подтверждёнными фактами и списком проблем: если факт верифицирован и попадает под policy риска, он должен материализоваться в `findings` (или быть явно отфильтрован по правилу).

---

## Step 1
### Status
Completed

### Objective
Зафиксировать формальный mapping contract: когда `verified_observation` становится `finding`.

### Invariant
Ни один `verified_observation` не теряется без явной причины (`filtered`, `duplicate`, `suppressed_by_policy`).

### Scope
- Спецификация правил трансформации.
- Перечень обязательных полей `finding`.

### Out of scope
- Реализация кода трансформации.

### Allowed change level
Docs-only (spec/contracts).

### Inputs
- Текущая схема отчёта.
- Примеры отчётов с пустым `findings`.

### Artifacts
- Документ mapping contract (в `docs`).
- Реализовано: `docs/OBSERVATION_TO_FINDING_MAPPING_CONTRACT.md`.

### Verification procedure
- Ручной walkthrough 3 примеров: high-risk факт, low-risk факт, suppressed факт.

### DoD
- Есть таблица `condition -> finding_status`.
- Описаны причины, когда finding не создаётся.

### Failure policy
- Если контракт не согласован, implementation блокируется.

---

## Step 2
### Status
Completed

### Objective
Расширить schema/model для `finding` до actionable-уровня.

### Invariant
Каждый finding имеет traceability к source observation.

### Scope
- Добавить/зафиксировать поля:
  - `finding_id`
  - `source_observation_ids[]`
  - `severity`
  - `confidence`
  - `impact`
  - `recommended_fix`
  - `status` (`open|accepted_risk|fixed|false_positive`)

### Out of scope
- UI визуализация новых полей.

### Allowed change level
Schema + model definitions.

### Inputs
- Contract из Step 1.

### Artifacts
- Обновлённая schema version.
- Миграционные примечания.
- Реализовано: `schema/report.schema.json`.
- Реализовано: `docs/REPORT_SCHEMA_MIGRATION_NOTES_STEP2.md`.

### Verification procedure
- Schema validation на старом и новом sample JSON.

### DoD
- Новая схема валидирует expected report.
- Есть backward-compat strategy.

### Failure policy
- При schema-break без миграции: rollback schema patch.

---

## Step 3
### Status
Completed

### Objective
Реализовать deterministic mapper `verified_observations -> findings`.

### Invariant
Mapper детерминирован: одинаковый input state даёт одинаковый findings output.

### Scope
- Правила трансформации.
- Проставление severity/confidence/impact/fix по rule metadata.

### Out of scope
- ML/LLM-обогащение findings.

### Allowed change level
Runtime logic in report-building layer.

### Inputs
- `verified_observations[]`
- Rule metadata / policy config.

### Artifacts
- Mapper module.
- Unit tests.
- Реализовано в `runtime/report_compiler.py` (`_merge_with_observation_mapped_findings`).
- Реализованы тесты в `tests/test_report_compiler_observation_mapper.py`.

### Verification procedure
- Golden tests: один вход -> бит-в-бит одинаковый findings список.

### DoD
- Для каждого eligible observation создаётся finding или explicit suppression record.
- Пустой findings допустим только при documented filters.

### Failure policy
- Если mapper падает: report помечается `failed_mapping`, не silently-empty findings.

---

## Step 4
### Status
Completed

### Objective
Добавить explicit suppression path вместо “тихого исчезновения”.

### Invariant
Отсутствие finding всегда объяснимо машинно-читаемым reason code.

### Scope
- `suppression_records[]` или `finding_status=filtered`.
- Reason codes: `below_threshold`, `duplicate`, `policy_suppressed`, `insufficient_evidence`.

### Out of scope
- Полноценный workflow по ручному апруву suppressions.

### Allowed change level
Runtime/report serialization.

### Inputs
- Output mapper (Step 3).

### Artifacts
- Suppression schema + serialization.
- Реализовано: `suppression_records` в `runtime/report_compiler.py`.
- Реализовано: `suppression_record` в `schema/report.schema.json`.
- Реализовано: тесты suppression path в `tests/test_report_compiler_observation_mapper.py`.

### Verification procedure
- Тест: observation не в findings -> обязан появиться suppression record.

### DoD
- Нулевые “silent drops”.

### Failure policy
- При отсутствии suppression reason для dropped observation: pipeline fail.

---

## Step 5
### Status
Completed

### Objective
Согласовать `audit.status` с фактической стадией пайплайна.

### Invariant
Статус аудита отражает фактическое состояние артефактов.

### Scope
- Ввести transition map:
  - `initialized -> scanning -> analyzed -> reported -> completed`
  - `* -> failed`

### Out of scope
- Оркестрация распределённых воркеров.

### Allowed change level
State machine logic.

### Inputs
- Текущий lifecycle аудита.

### Artifacts
- Status transition policy.
- Runtime guards.
- Реализовано: `docs/AUDIT_STATUS_TRANSITION_POLICY.md`.
- Реализовано: runtime guard в `runtime/report_compiler.py` (`_validate_report_audit_status`).
- Реализовано: тесты в `tests/test_report_audit_status_guard.py`.

### Verification procedure
- Тест-кейсы запрещённых переходов и happy-path.

### DoD
- Нельзя получить populated report со статусом `initialized`.

### Failure policy
- Некорректный переход -> hard error + audit marked `failed`.

---

## Step 6
### Status
Completed

### Objective
Добавить consistency checks на этапе сборки отчёта.

### Invariant
`summary` всегда консистентен с payload.

### Scope
- Проверки:
  - `verified_observation_count == len(verified_observations)`
  - `finding_count == len(findings)`
  - если eligible observations > 0 и findings=0, требуется suppression coverage.

### Out of scope
- Исправление данных постфактум в UI.

### Allowed change level
Report validator / assembler.

### Inputs
- Отчёт перед публикацией.

### Artifacts
- Consistency validator.
- Error codes.
- Реализовано: `_validate_compiled_report_consistency` в `runtime/report_compiler.py`.
- Реализовано: тесты `tests/test_report_consistency_validator.py`.

### Verification procedure
- Негативные тесты с искусственно сломанными счётчиками.

### DoD
- Неконсистентный отчёт не публикуется.

### Failure policy
- Publish blocked, emit validation error.

---

## Step 7
### Status
Completed

### Objective
Покрыть end-to-end тестами сценарий “факт найден -> finding создан”.

### Invariant
Security-факт из deterministic pattern не пропадает в финальном отчёте.

### Scope
- E2E fixture:
  - wildcard CORS
  - JWT no signature verify
- Проверка findings count >= 2 (или suppression с reason).

### Out of scope
- Нагрузочные тесты.

### Allowed change level
Tests only.

### Inputs
- Mini fixture repo.
- Обновлённый runtime.

### Artifacts
- E2E test cases.
- Golden report snapshots.
- Реализовано: `tests/test_report_security_mini_e2e.py`.
- Реализовано: `tests/fixtures/golden_report_security_mini.json`.

### Verification procedure
- Запуск полного audit pipeline в CI.

### DoD
- Тест стабильно зелёный в CI.

### Failure policy
- Любой regression блокирует merge.

---

## Step 8
### Status
Completed

### Objective
Обновить документацию контракта отчёта для потребителей API/UI.

### Invariant
Потребитель не угадывает семантику `findings` и `verified_observations`.

### Scope
- Раздел “observation vs finding”.
- Примеры payload с suppression.

### Out of scope
- Редизайн UI.

### Allowed change level
Docs + examples.

### Inputs
- Итоги шагов 1–7.

### Artifacts
- Обновлённый runtime contract doc.
- Changelog.
- Реализовано: `docs/REPORT_RUNTIME_CONTRACT.md`.
- Актуализированы разделы `observation vs finding`, suppression reason codes, lifecycle statuses, payload examples, failure modes.

### Verification procedure
- Manual doc review + payload example lint.

### DoD
- Документация покрывает все reason codes и lifecycle статусы.

### Failure policy
- Если docs не обновлены, релиз schema/runtime откладывается.
