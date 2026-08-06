from __future__ import annotations

import os
from dataclasses import dataclass


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(slots=True)
class Settings:
    egov_api_key: str | None
    bitrix_webhook_url: str | None
    bitrix_verify_ssl: bool
    bitrix_ca_bundle: str | None
    eqazyna_request_timeout: int = 60
    eqazyna_max_retries: int = 4
    eqazyna_retry_base_sleep_seconds: float = 3.0
    bitrix_request_timeout: int = 45
    egov_request_timeout: int = 25
    polite_delay_seconds: float = 0.2
    bitrix_polite_delay_seconds: float = 0.05
    egov_polite_delay_seconds: float = 0.05

    @property
    def bitrix_tls_verify(self) -> bool | str:
        return self.bitrix_ca_bundle or self.bitrix_verify_ssl

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            egov_api_key=os.getenv("EGOV_API_KEY") or None,
            bitrix_webhook_url=(
                os.getenv("TARGET_BITRIX_WEBHOOK_URL")
                or os.getenv("BITRIX_WEBHOOK_URL")
                or None
            ),
            bitrix_verify_ssl=env_bool("BITRIX_VERIFY_SSL", True),
            bitrix_ca_bundle=os.getenv("BITRIX_CA_BUNDLE") or None,
            eqazyna_request_timeout=int(os.getenv("EQAZYNA_REQUEST_TIMEOUT", "60")),
            eqazyna_max_retries=int(os.getenv("EQAZYNA_MAX_RETRIES", "4")),
            eqazyna_retry_base_sleep_seconds=float(
                os.getenv("EQAZYNA_RETRY_BASE_SLEEP_SECONDS", "3")
            ),
            bitrix_request_timeout=int(os.getenv("BITRIX_REQUEST_TIMEOUT", "45")),
            egov_request_timeout=int(os.getenv("EGOV_REQUEST_TIMEOUT", "25")),
            polite_delay_seconds=float(
                os.getenv("EQAZYNA_POLITE_DELAY_SECONDS", "0.2")
            ),
            bitrix_polite_delay_seconds=float(
                os.getenv("BITRIX_POLITE_DELAY_SECONDS", "0.05")
            ),
            egov_polite_delay_seconds=float(
                os.getenv("EGOV_POLITE_DELAY_SECONDS", "0.05")
            ),
        )
