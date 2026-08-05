from eqazyna_bitrix.settings import Settings


def test_target_webhook_has_priority(monkeypatch):
    monkeypatch.setenv("TARGET_BITRIX_WEBHOOK_URL", "https://box.example.invalid/webhook/")
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://cloud.example.invalid/webhook/")

    settings = Settings.from_env()

    assert settings.bitrix_webhook_url == "https://box.example.invalid/webhook/"
