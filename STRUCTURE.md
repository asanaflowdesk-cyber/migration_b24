# Структура репозитория

```text
.github/workflows/
  00-ci.yml                   автоматический read-only CI на push/PR/manual
  01-cloud-export.yml         read-only экспорт облачного snapshot
  10-migration-plan.yml       проверка дампа, коробки и конфигурации
  11-migration-users.yml      сопоставление существующих пользователей
  12-migration-import.yml     dry-run и реальный перенос
  13-migration-verify.yml     итоговая field/relation сверка
  20-user-registration.yml    отдельная регистрация пользователей из Excel
  30-eqazyna-leads.yml        постоянный парсер e-Qazyna → лиды коробки
  31-company-owner-sync.yml   ответственный компании по руководителю той же компании
  32-restore-failed-leads.yml контролируемый возврат ошибочно закрытых лидов
  33-eqazyna-status-sync.yml  сверка статусов e-Qazyna по точному номеру заявки → стадии лидов
  40-flowdesk.yml             FlowDesk event → идемпотентная задача
  50-create-departments.yml   отдельное создание подразделений

common/
  bitrix.py                   общий REST-клиент: fail-closed pagination, safe read retry
  security.py                 санитизация webhook/PII и защита CSV/Excel literals
  naming.py                   нормализация наименований CRM

processes/cloud_to_box/
  input/
    bitrix24_dump_20260805_072425.zip   immutable source snapshot
    bitrix24_export.xlsx                человекочитаемая сверка
  config/
    migration.json            маршрутизация, поля, source integrity policy
    users.csv                 ручные соответствия пользователей
    source_plan.json          raw/expected counts и approved exclusions
  src/
    dump_reader.py            чтение ZIP + проверка manifest/SHA-256/counts
    migration.py              preflight, mapping, import, additive relations, verify
    file_transfer.py          перенос вложений с SHA-256 именованием
    live_source.py            live enrichment только когда явно выбран/нужен
    reporting.py              отчёты, redaction, ID maps и state identity
  tests/
    test_migration.py         дамп, failure policy, relations, verify, workflows
  migrate.py                  CLI для workflow
  run_from_env.py             безопасная сборка workflow inputs из env

processes/cloud_export/       read-only экспорт cloud snapshot
processes/company_owner_sync/ сверка владельца компании по director contact этой компании
processes/departments/        отдельное создание подразделений
processes/eqazyna_leads/      e-Qazyna parser
processes/eqazyna_status_sync/ точная сверка статуса уже загруженных заявок e-Qazyna и стадий лидов
processes/flowdesk/           FlowDesk → задачи с event deduplication marker
processes/lead_recovery/      восстановление ошибочно проваленных лидов
processes/user_registration/  регистрация пользователей из Excel

scripts/
  prepare-python.cmd          создание локальной .venv из Python 3.12, уже установленного на self-hosted Windows runner
  run_quality_checks.py       compileall + все 9 тестовых пакетов

requirements-ci.txt           единый точный набор прямых CI-зависимостей
```

Папки `output/`, `.venv`, cache/bytecode создаются только во время выполнения и не должны попадать в поставку.

Workflow 12 не восстанавливает общий «последний» GitHub Actions Cache. Идемпотентность повторного запуска строится на migration markers и reconciliation целевого портала; сохранённые ID-карты принимаются только с совпадающим state identity.

Входной snapshot содержит ПДн и включён в handoff ZIP только для самодостаточности. `.gitignore` запрещает случайный новый commit ZIP/XLSX/input-файлов.
