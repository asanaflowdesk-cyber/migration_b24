# Структура репозитория

```text
.github/workflows/
  10-migration-plan.yml       проверка дампа, коробки и конфигурации
  11-migration-users.yml      только сопоставление существующих пользователей
  12-migration-import.yml     dry-run и реальный перенос
  13-migration-verify.yml     итоговая сверка по маркерам и картам ID
  20-user-registration.yml    отдельный инструмент; сейчас не используется
  30-eqazyna-leads.yml        постоянный парсер e-Qazyna → лиды коробки

common/
  bitrix.py                   общий REST-клиент миграции и регистрации

processes/cloud_to_box/
  input/
    bitrix24_dump_20260805_072425.zip   зафиксированный разовый дамп облака
  config/
    migration.json            маршрутизация стадий, поля и справочники коробки
    users.csv                 ручные соответствия пользователей
    source_plan.json          контрольные количества по дампу
  src/
    dump_reader.py            чтение JSON из ZIP
    migration.py              основная логика переноса
    file_transfer.py          скачивание и загрузка вложений
    reporting.py              отчеты и карты ID
  tests/
    test_migration.py         автономные проверки дампа и правил
  migrate.py                  командная строка для workflow

processes/eqazyna_leads/
  eqazyna_bitrix/
    main.py                   последовательность обработки
    scraper.py                чтение реестра e-Qazyna
    egov_client.py            необязательное обогащение через data.egov.kz
    bitrix_client.py          REST-клиент коробки и поиск старых мигрированных лидов
    lead_pipeline.py          один БИН → один основной лид
    formatter.py              текст карточки и таймлайна
    exporter.py               Excel/JSON журнала запуска
  scripts/
    run_from_env.py           безопасная сборка аргументов для Windows cmd
    check_run_result.py       итоговая проверка журнала
  tests/                      автономные тесты парсера

processes/user_registration/  отдельный процесс регистрации пользователей
scripts/prepare-python.cmd     подготовка Python на Windows runner
```

Папки `output/` создаются только во время выполнения. Миграционный поток 12 сохраняет карты ID в GitHub Actions Cache. Парсер e-Qazyna не использует миграционные карты и не изменяет файлы разовой миграции.
