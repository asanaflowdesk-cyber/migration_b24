from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode

try:
    import truststore
except ImportError:  # pragma: no cover
    truststore = None
else:
    truststore.inject_into_ssl()

import requests

LOG = logging.getLogger(__name__)


class BitrixError(RuntimeError):
    def __init__(self, method: str, code: str, description: str, payload: Any = None):
        super().__init__(f"{method}: {code}: {description}")
        self.method = method
        self.code = code
        self.description = description
        self.payload = payload


def _query_pairs(prefix: str, value: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{prefix}[{key}]" if prefix else str(key)
            pairs.extend(_query_pairs(child, nested))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            pairs.extend(_query_pairs(f"{prefix}[{index}]", nested))
    elif value is not None:
        if isinstance(value, bool):
            value = "Y" if value else "N"
        pairs.append((prefix, str(value)))
    return pairs


def batch_command(method: str, params: Mapping[str, Any]) -> str:
    return f"{method}?{urlencode(_query_pairs('', params), doseq=True)}"


def _extract_items(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [dict(x) if isinstance(x, dict) else {"VALUE": x} for x in result]
    if isinstance(result, dict):
        for key in ("items", "users", "departments", "categories", "tasks", "messages", "result"):
            value = result.get(key)
            if isinstance(value, list):
                return [dict(x) if isinstance(x, dict) else {"VALUE": x} for x in value]
        return [dict(result)]
    return []


@dataclass
class BatchResult:
    success: dict[str, Any]
    errors: dict[str, dict[str, Any]]


class BitrixClient:
    def __init__(self, webhook_url: str, timeout: int = 60, retries: int = 6, delay: float = 0.05):
        webhook_url = webhook_url.strip()
        if not webhook_url:
            raise ValueError("Bitrix webhook URL is empty")
        self.base = webhook_url.rstrip("/") + "/"
        # REST 3.0 task methods use /rest/api/{user}/{token}/ instead of
        # the classic /rest/{user}/{token}/ endpoint.
        self.api_v3_base = re.sub(r"/rest/(\d+)/([^/]+)/$", r"/rest/api/\1/\2/", self.base)
        if self.api_v3_base == self.base:
            self.api_v3_base = self.base.replace("/rest/", "/rest/api/", 1)
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "b24-cloud-box-migration/4.0"})

    @classmethod
    def from_env(cls, variable: str = "TARGET_BITRIX_WEBHOOK_URL") -> "BitrixClient":
        return cls(
            os.environ.get(variable, ""),
            timeout=int(os.getenv("HTTP_TIMEOUT_SECONDS", "60")),
            retries=int(os.getenv("MAX_RETRIES", "6")),
            delay=float(os.getenv("REQUEST_DELAY_SECONDS", "0.05")),
        )

    @staticmethod
    def _error_parts(data: Mapping[str, Any]) -> tuple[str, str]:
        error = data.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or "ERROR")
            description = str(error.get("message") or error.get("description") or code)
            validation = error.get("validation")
            if isinstance(validation, list) and validation:
                details = "; ".join(
                    f"{item.get('field', '?')}: {item.get('message', '')}"
                    for item in validation
                    if isinstance(item, Mapping)
                )
                if details:
                    description = f"{description}; {details}"
            return code, description
        code = str(error or "ERROR")
        description = str(data.get("error_description") or error or "Unknown Bitrix error")
        return code, description

    def _raw_url(self, method: str, params: Mapping[str, Any] | None, *, base: str, json_suffix: bool) -> dict[str, Any]:
        params = dict(params or {})
        url = base + method + (".json" if json_suffix else "")
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.post(url, json=params, timeout=self.timeout)
                try:
                    data = response.json()
                except Exception as exc:
                    raise requests.HTTPError(f"HTTP {response.status_code}; invalid JSON: {response.text[:500]}") from exc
                if not isinstance(data, dict):
                    raise requests.HTTPError(f"HTTP {response.status_code}; unexpected JSON: {data!r}")
                if "error" in data:
                    code, desc = self._error_parts(data)
                    if code in {"QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT", "OVERLOAD_LIMIT"} and attempt < self.retries:
                        wait = min(60.0, 2.0 ** attempt)
                        LOG.warning("%s throttled: %s. retry in %.1fs", method, code, wait)
                        time.sleep(wait)
                        continue
                    raise BitrixError(method, code, desc, data)
                if response.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {response.status_code}: {data}")
                if response.status_code >= 400:
                    raise requests.HTTPError(f"HTTP {response.status_code}: {data}")
                if self.delay:
                    time.sleep(self.delay)
                return data
            except BitrixError:
                raise
            except Exception as exc:
                last = exc
                if attempt >= self.retries:
                    break
                wait = min(30.0, 1.5 ** attempt)
                LOG.warning("%s request failed (%s), retry in %.1fs", method, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"{method} failed after retries: {last}")

    def raw(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._raw_url(method, params, base=self.base, json_suffix=True)

    def raw_v3(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._raw_url(method, params, base=self.api_v3_base, json_suffix=False)

    def call(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.raw(method, params).get("result")

    def call_v3(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        return self.raw_v3(method, params).get("result")

    def download(self, url: str) -> tuple[bytes, Mapping[str, str]]:
        """Download a file URL returned by Bitrix, with retries."""
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                response.raise_for_status()
                return response.content, response.headers
            except Exception as exc:
                last = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(20.0, 1.5 ** attempt))
        raise RuntimeError(f"file download failed after retries: {last}")

    def list_all(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        max_pages: int = 10000,
    ) -> list[dict[str, Any]]:
        base = dict(params or {})
        start: Any = base.pop("start", 0)
        rows: list[dict[str, Any]] = []
        seen_fingerprints: set[str] = set()
        for _page in range(1, max_pages + 1):
            payload = dict(base)
            payload["start"] = start
            raw = self.raw(method, payload)
            items = _extract_items(raw.get("result"))
            fingerprint = json.dumps(items[:2], ensure_ascii=False, sort_keys=True, default=str) + f"|{len(items)}"
            if fingerprint in seen_fingerprints:
                LOG.warning("%s repeated page detected at start=%s", method, start)
                break
            seen_fingerprints.add(fingerprint)
            rows.extend(items)
            next_value = raw.get("next")
            if next_value in (None, "", False):
                break
            start = next_value
        return rows

    def batch(self, commands: Mapping[str, tuple[str, Mapping[str, Any]]], *, halt: int = 0) -> BatchResult:
        if not commands:
            return BatchResult({}, {})
        if len(commands) > 50:
            raise ValueError("Bitrix batch supports at most 50 commands")
        cmd = {key: batch_command(method, params) for key, (method, params) in commands.items()}
        result = self.call("batch", {"halt": halt, "cmd": cmd}) or {}
        success = result.get("result", {}) if isinstance(result, dict) else {}
        errors = result.get("result_error", {}) if isinstance(result, dict) else {}
        normalized_errors: dict[str, dict[str, Any]] = {}
        for key, value in (errors or {}).items():
            if isinstance(value, dict):
                normalized_errors[str(key)] = value
            else:
                normalized_errors[str(key)] = {"error": "BATCH_ERROR", "error_description": str(value)}
        return BatchResult(dict(success or {}), normalized_errors)

    def batch_chunks(
        self,
        commands: Iterable[tuple[str, str, Mapping[str, Any]]],
        *,
        size: int = 40,
        max_encoded_chars: int = 120_000,
        single_call_threshold: int = 75_000,
    ) -> Iterable[tuple[dict[str, Any], dict[str, dict[str, Any]]]]:
        chunk: dict[str, tuple[str, Mapping[str, Any]]] = {}
        encoded_size = 0

        def flush() -> tuple[dict[str, Any], dict[str, dict[str, Any]]] | None:
            nonlocal chunk, encoded_size
            if not chunk:
                return None
            result = self.batch(chunk)
            chunk = {}
            encoded_size = 0
            return result.success, result.errors

        for key, method, params in commands:
            command_size = len(batch_command(method, params))
            if command_size >= single_call_threshold:
                ready = flush()
                if ready:
                    yield ready
                try:
                    yield {key: self.call(method, params)}, {}
                except BitrixError as exc:
                    yield {}, {key: {"error": exc.code, "error_description": exc.description}}
                except Exception as exc:
                    yield {}, {key: {"error": "REQUEST_ERROR", "error_description": str(exc)}}
                continue
            if chunk and (len(chunk) >= size or encoded_size + command_size > max_encoded_chars):
                ready = flush()
                if ready:
                    yield ready
            chunk[key] = (method, params)
            encoded_size += command_size
        ready = flush()
        if ready:
            yield ready
