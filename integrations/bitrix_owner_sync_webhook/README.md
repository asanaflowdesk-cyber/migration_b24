# Обработчик изменения ответственного учредителя

Vercel Function принимает событие `ONCRMCONTACTUPDATE` из Bitrix24 и запускает
точечную синхронизацию пакета через GitHub Actions.

## Переменные Vercel

- `PUBLIC_BASE_URL` — адрес проекта, например `https://owner-sync.example.vercel.app`.
- `BITRIX_WEBHOOK_KEY` — случайная секретная строка длиной не менее 32 символов.
- `BITRIX_ALLOWED_DOMAIN` — домен портала без протокола, например `portal.bitrix24.ru`.
- `GITHUB_REPOSITORY` — `asanaflowdesk-cyber/migration_b24`.
- `GITHUB_DISPATCH_TOKEN` — fine-grained GitHub token с доступом Actions: write к репозиторию.

## Адреса локального приложения Bitrix24

- Путь обработчика: `https://<проект>.vercel.app/api/bitrix/app`
- Путь первоначальной установки: `https://<проект>.vercel.app/api/bitrix/install`

Отметьте «Использует только API» и предоставьте право CRM. Во время первой
установки приложение регистрирует обработчик события `ONCRMCONTACTUPDATE`.

## Что происходит

1. Bitrix24 отправляет ID измененного контакта в `/api/bitrix/event`.
2. Обработчик проверяет секретный ключ и домен портала.
3. Обработчик отправляет событие `founder_owner_changed` в GitHub.
4. Workflow 31A запускается на self-hosted runner и передает ID контакта скрипту.
5. Если контакт является учредителем или руководителем, его ответственный
   назначается всему пакету. Иначе запуск завершается без изменений.

Секреты не записываются в репозиторий. После смены URL проекта повторно откройте
страницу установки приложения, чтобы Bitrix24 зарегистрировал новый URL события.
