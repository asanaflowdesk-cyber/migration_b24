# Карта файлов

## Общие файлы

- `common/bitrix.py` — REST-клиент коробочного Bitrix24, повторы запросов и корпоративные сертификаты Windows.
- `scripts/prepare-python.cmd` — создает локальную `.venv` без административных прав.

## Выгрузка облака

- `processes/cloud_export/export_bitrix.py` — выгружает сущности облачного Bitrix24.
- `processes/cloud_export/requirements.txt` — зависимости выгрузки.
- `processes/cloud_export/tests/` — проверки выгрузчика.

## Перенос облако → коробка

- `processes/cloud_to_box/migrate.py` — команды `plan`, `invite-users`, `import`, `verify`.
- `processes/cloud_to_box/input/bitrix24_export.xlsx` — входная выгрузка облака.
- `processes/cloud_to_box/config/migration.json` — правила стадий и полей.
- `processes/cloud_to_box/config/users.csv` — сопоставление ответственных и подразделений.
- `processes/cloud_to_box/src/` — логика миграции.
- `processes/cloud_to_box/tests/` — контрольные тесты.

## Регистрация пользователей

- `processes/user_registration/register_users_from_excel.py` — проверка и приглашение пользователей.
- `processes/user_registration/input/users_to_invite.xlsx` — редактируемый документ.
