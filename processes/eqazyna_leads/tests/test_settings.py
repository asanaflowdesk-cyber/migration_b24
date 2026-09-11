from eqazyna_bitrix.settings import Settings


def test_target_webhook_has_priority(monkeypatch):
    monkeypatch.setenv("TARGET_BITRIX_WEBHOOK_URL", "https://box.example.invalid/webhook/")
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://cloud.example.invalid/webhook/")

    settings = Settings.from_env()

    assert settings.bitrix_webhook_url == "https://box.example.invalid/webhook/"


def test_default_egov_name_similarity_threshold_is_60(monkeypatch):
    monkeypatch.delenv("EGOV_MIN_NAME_MATCH", raising=False)
    import importlib
    import eqazyna_bitrix.egov_client as egov_client

    reloaded = importlib.reload(egov_client)
    assert reloaded.EGOV_MIN_NAME_MATCH == 60
