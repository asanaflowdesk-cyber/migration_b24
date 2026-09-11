# Структура репозитория

```text
.github/workflows/                 GitHub Actions
common/                            общие утилиты
processes/
  user_registration/              регистрация пользователей
  eqazyna_leads/                   загрузка ГПО из e-Qazyna
  company_owner_sync/              синхронизация ответственных компаний
  lead_recovery/                   возврат ошибочно проваленных лидов
  excluded_user_reassignment/      перенос пакетов исключённых пользователей
  eqazyna_status_sync/             синхронизация статусов e-Qazyna
  flowdesk/                        FlowDesk → Bitrix24
  departments/                     создание подразделений
scripts/
  prepare-python.cmd               создание локального .venv на Windows runner
  run_quality_checks.py            общий запуск проверок
```

Разовые `cloud_export` и `cloud_to_box` удалены.
