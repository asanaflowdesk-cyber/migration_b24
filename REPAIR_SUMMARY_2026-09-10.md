# Итог исправления migration_b24 — 2026-09-10

Основание: технический аудит `audit_migration_b24_ru.md` от 2026-09-10.

## Статус

Код исправлен и повторно проверен локально без обращения к рабочим внешним системам.

Контрольные результаты:

- 53/53 dataset в исходном дампе прошли проверку manifest, SHA-256 и row count;
- source integrity: OK;
- 1 160 raw address rows → 907 уникальных payload; 253 точных повтора обрабатываются детерминированно;
- две исходные orphan-связи `deal→company` (`668→382`, `1344→730`) закреплены как явные source exceptions; несуществующие компании не создаются;
- Python compile: OK;
- 139 тестов: passed;
- все GitHub Actions YAML: parsed;
- все внешние GitHub Actions закреплены на 40-символьные commit SHA;
- прямые Python dependencies закреплены точными версиями;
- небезопасные `crm.*.contact.items.set` в миграции отсутствуют;
- общий state cache между workflow runs отсутствует;
- `lead_reopen` удалён;
- небезопасный неиспользуемый XLSX XML reader удалён;
- cache/venv/bytecode не входят в релизный ZIP.

## Закрытые блокеры

1. `import` и `verify` возвращают ненулевой exit code при необъяснённой потере, ERROR/FATAL или verification gap.
2. Автоматический merge ограничен только одинаковыми точными migration marker; merge по БИН/ФИО/телефону/email запрещён.
3. Автоматический повтор изменяющих REST POST после неоднозначного timeout отключён; повторный запуск выполняет reconciliation по существующему состоянию.
4. Workflow inputs передаются в команды через environment + валидирующие Python wrappers; прямой ввод пользовательских строк в `cmd` исключён.
5. Webhook secrets санитизируются в ошибках; обычные отчёты редактируют чувствительные значения; raw/sensitive artifacts требуют отдельного opt-in и имеют короткий retention.
6. Исправлен fallback комментариев задач, ранее падавший с `UnboundLocalError`.
7. Состояние миграции привязано к hash snapshot/config/users и target portal; неподходящий/corrupt state отклоняется.
8. CRM relation import переведён на additive `*.add`, поэтому вручную добавленные target-only связи не удаляются.

## Дополнительные исправления

- production `apply` разрешён только из immutable dump (`source_mode=dump`);
- manifest проверяется приложением до первой записи;
- converted leads используют явную карту source lead → source deal вместо сопоставления по title;
- FlowDesk получил stable event marker и защиту от повторной задачи;
- user registration проверяет положительный `user.add` ID и дедуплицирует внутри одного файла;
- company owner sync больше не считает одинаковое ФИО глобальной идентичностью: owner берётся из director contact той же компании;
- e-Qazyna manager pool синхронизирован между кодом, workflow и README;
- eGov enrichment действительно необязателен;
- pagination в common client, e-Qazyna client и cloud exporter работает fail-closed при повторе страницы/`next`;
- CSV/XLSX formula injection нейтрализована;
- filename fingerprint переведён с короткого SHA-1 на SHA-256;
- добавлен автоматический CI на push/pull_request и единый локальный `scripts/run_quality_checks.py`;
- документация приведена к фактическим правилам: 589 переносимых задач, 907 уникальных адресов, разные product fields для lead/deal, отсутствие общего GitHub cache resume.

## Что нельзя подтвердить офлайн

Ни один тест не выполнял реальные изменяющие запросы к production Bitrix24, 1С, e-Qazyna или eGov. Поэтому перед full production apply обязательны полный `dry_run`, ограниченный `apply` на контролируемой среде и `verify`. Это проверка внешней среды/прав/роботов, а не известная ошибка релизного кода.

## Финальная локальная проверка перед упаковкой

- повторный прогон всех 8 тестовых пакетов: **139 passed**;
- `compileall` и AST-разбор всех Python-файлов: OK;
- все workflow YAML разбираются; внешние Actions закреплены на полных 40-символьных SHA;
- прямой shell/cmd-интерполяции `${{ inputs.* }}` внутри `run:` не обнаружено;
- опасные `crm.contact.company.items.set`, `crm.lead.contact.items.set`, `crm.deal.contact.items.set` отсутствуют;
- lead recovery использует только явный `moved_by_id`/`LEAD_RECOVERY_MOVED_BY_ID` и не пытается угадать пользователя через `user.current`;
- company owner sync запускается напрямую из репозитория без ручной настройки `PYTHONPATH`;
- исходный dump повторно проверен: manifest 53/53, source integrity OK, 907 уникальных адресов, 589 переносимых задач.

### Additional runner fix after live GitHub Actions validation

A Windows self-hosted run exposed an environment dependency that offline tests could not reproduce: the runner service account had no Python 3.11+ on `PATH`. The Windows workflows previously called `scripts/prepare-python.cmd` directly and therefore stopped before tests or migration logic ran.

Corrective action:
- added pinned `actions/setup-python` (Python 3.12 x64) to every Windows self-hosted workflow before `prepare-python.cmd`;
- changed `prepare-python.cmd` to consume the interpreter provisioned by the action rather than searching user-profile installations;
- preserved `PYTHON_EXE` only as a manual-run override;
- recreate `.venv` on each job so a persistent self-hosted workspace cannot reuse an environment from an older run.
