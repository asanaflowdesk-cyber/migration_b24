import os

webhook = os.getenv("TARGET_BITRIX_WEBHOOK_URL")

if webhook is None:
    print("Webhook не передан")
else:
    print("Webhook получен")