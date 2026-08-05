from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from .models import CompanyEnrichment
from .region import detect_city, detect_region

EGOV_URL = os.getenv("EGOV_API_URL", "https://data.egov.kz/api/v4/gbd_ul/v1")
EGOV_RESULT_SIZE = int(os.getenv("EGOV_RESULT_SIZE", "20"))
EGOV_MIN_NAME_MATCH = int(os.getenv("EGOV_MIN_NAME_MATCH", "75"))
# OKED/TPI is diagnostic only. It does NOT participate in trusted-match filtering.
ALLOWED_OKED_PREFIXES = tuple(
    p.strip() for p in os.getenv("EGOV_ALLOWED_OKED_PREFIXES", "05,07,08,09").split(",") if p.strip()
)


@dataclass(slots=True)
class CandidateScore:
    record: dict[str, Any]
    enrichment: CompanyEnrichment
    name_score: int
    oked_tpi: bool
    oked_reason: str
    bin_matches: bool


@dataclass(slots=True)
class EgovClient:
    api_key: str | None
    timeout: int = 30
    polite_delay_seconds: float = 0.05
    session: requests.Session | None = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "FlowDesk-eQazyna/1.0 (+GitHub Actions)",
            }
        )

    def get_company(self, bin_number: str, applicant_name: str | None = None) -> CompanyEnrichment:
        """Return trusted company enrichment from data.egov.kz.

        Business rule:
        - e-Qazyna BIN must match eGov BIN exactly;
        - e-Qazyna applicant name must match eGov Russian company name >= EGOV_MIN_NAME_MATCH.

        OKED/activity is kept only as a diagnostic/info field and does not block matching.
        If no trusted match exists, caller still creates Bitrix objects from e-Qazyna data,
        but eGov address/director/phone must not overwrite CRM data.
        """
        bin_number = (bin_number or "").strip()
        applicant_name = (applicant_name or "").strip()
        if not bin_number:
            return CompanyEnrichment(bin=bin_number, error="empty_bin")
        if not self.api_key:
            return CompanyEnrichment(bin=bin_number, error="EGOV_API_KEY is empty")

        source = _source_for_bin(bin_number)
        attempts: list[dict[str, Any]] = []
        last_payload: Any = None
        try:
            response = self.session.get(
                EGOV_URL,
                params={"apiKey": self.api_key, "source": json.dumps(source, ensure_ascii=False)},
                timeout=self.timeout,
            )
            attempts.append({"endpoint": _safe_endpoint(EGOV_URL), "status_code": response.status_code, "source": source})
            if response.status_code in {401, 403}:
                return CompanyEnrichment(
                    bin=bin_number,
                    raw={"attempts": attempts, "response_preview": _sanitize_error(_safe_text(response.text), self.api_key)},
                    error=f"egov_forbidden_{response.status_code}",
                )
            if response.status_code >= 400:
                return CompanyEnrichment(
                    bin=bin_number,
                    raw={"attempts": attempts, "response_preview": _sanitize_error(_safe_text(response.text), self.api_key)},
                    error=f"egov_http_{response.status_code}",
                )
            data = _safe_json(response)
            last_payload = data
            records = _records(data)
            exact_records = [r for r in records if _record_bin_matches(bin_number, r)]
            candidates = _candidate_previews(bin_number, applicant_name, exact_records or records)
            selected = _select_trusted_record(bin_number, applicant_name, exact_records)
            time.sleep(self.polite_delay_seconds)
            if selected:
                enr = selected.enrichment
                enr.error = None
                enr.match_name_score = selected.name_score
                enr.match_oked_tpi = selected.oked_tpi
                enr.match_reason = selected.oked_reason
                enr.raw = {
                    **enr.raw,
                    "match": {
                        "name_score": selected.name_score,
                        "oked_tpi_info": selected.oked_tpi,
                        "oked_reason_info": selected.oked_reason,
                        "min_name_match": EGOV_MIN_NAME_MATCH,
                        "oked_filter_used": False,
                        "applicant_name": applicant_name,
                    },
                    "candidate_count": len(exact_records),
                    "candidates_preview": candidates,
                }
                return enr

            reason = "not_found"
            if exact_records:
                reason = (
                    "trusted_match_not_found: accepted match requires exact BIN + "
                    f"name_score>={EGOV_MIN_NAME_MATCH}; OKED is diagnostic only"
                )
            elif records:
                reason = "rejected_bin_mismatch: eGov returned records but none matched BIN exactly"
            return CompanyEnrichment(
                bin=bin_number,
                raw={
                    **_trim_raw(last_payload),
                    "attempts": attempts,
                    "candidate_count": len(exact_records),
                    "candidates_preview": candidates,
                    "applicant_name": applicant_name,
                    "min_name_match": EGOV_MIN_NAME_MATCH,
                    "oked_filter_used": False,
                },
                error=reason,
            )
        except requests.Timeout:
            return CompanyEnrichment(bin=bin_number, raw={"attempts": attempts}, error="egov_timeout")
        except requests.RequestException as exc:
            return CompanyEnrichment(bin=bin_number, raw={"attempts": attempts}, error=_sanitize_error(f"egov_request_error: {exc}", self.api_key))
        except Exception as exc:  # noqa: BLE001
            return CompanyEnrichment(bin=bin_number, raw={"attempts": attempts}, error=_sanitize_error(f"egov_error: {exc}", self.api_key))


def _source_for_bin(bin_number: str) -> dict[str, Any]:
    # Matches the official API examples: source={"size": N, "query": {"bool": {"must": [{"match": {"bin": "..."}}]}}}
    return {"size": max(1, EGOV_RESULT_SIZE), "query": {"bool": {"must": [{"match": {"bin": bin_number}}]}}}


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw_text": _safe_text(response.text)}


def _safe_text(value: str | None, limit: int = 1000) -> str:
    value = value or ""
    value = value.replace("\n", " ").replace("\r", " ").strip()
    return value[:limit] + ("..." if len(value) > limit else "")


def _safe_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitize_error(text: str, api_key: str | None) -> str:
    if not text:
        return text
    if api_key:
        text = text.replace(api_key, "***")
    return re.sub(r"apiKey=[^&\s]+", "apiKey=***", text)


def _trim_raw(value: Any, limit: int = 3000) -> dict[str, Any]:
    if value is None:
        return {}
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        text = text[:limit] + "..."
    return {"raw_preview": text}


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    records: list[dict[str, Any]] = []
    for key in ("result", "data", "items", "records", "rows"):
        value = data.get(key)
        if isinstance(value, list):
            records.extend([x for x in value if isinstance(x, dict)])
        elif isinstance(value, dict):
            records.extend(_records(value))
    hits = data.get("hits")
    if isinstance(hits, dict):
        hit_list = hits.get("hits")
        if isinstance(hit_list, list):
            for hit in hit_list:
                if isinstance(hit, dict):
                    source = hit.get("_source")
                    records.append(source if isinstance(source, dict) else hit)
    normalized_keys = {str(k).lower() for k in data.keys()}
    if normalized_keys & {"bin", "nameru", "addressru", "director", "okedru", "datereg", "rq_bin"}:
        records.append(data)
    return _dedupe_records(records)


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for record in records:
        nested = record.get("_source") if isinstance(record.get("_source"), dict) else record
        key = json.dumps(nested, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return unique


def _pick(record: dict[str, Any], keys: list[str]) -> str | None:
    lower = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return str(record[key]).strip()
        lk = key.lower()
        if lk in lower and lower[lk] not in (None, ""):
            return str(lower[lk]).strip()
    return None


def _extract_record_bin(record: dict[str, Any]) -> str | None:
    nested = record.get("_source") if isinstance(record.get("_source"), dict) else record
    return _pick(nested, ["bin", "BIN", "iinBin", "iin_bin", "bin_iin", "IIN_BIN", "RQ_BIN", "БИН"])


def _record_bin_matches(bin_number: str, record: dict[str, Any]) -> bool:
    found = _extract_record_bin(record)
    if not found:
        return False
    digits = "".join(ch for ch in found if ch.isdigit())
    return digits == bin_number


def _parse_company_record(bin_number: str, record: dict[str, Any]) -> CompanyEnrichment:
    nested = record.get("_source") if isinstance(record.get("_source"), dict) else record
    name = _pick(nested, ["nameru", "name_ru", "NameRu", "full_name_ru", "org_name", "organization_name", "company_name", "name"])
    legal_address = _pick(nested, ["addressru", "legal_address", "address_ru", "AddressRu", "jur_address", "address", "reg_address"])
    director = _pick(nested, ["director", "rukovoditel", "boss", "first_head", "fio_rukovoditel", "head"])
    activity = _pick(nested, ["okedru", "oked_name_ru", "okedNameRu", "activity_ru", "activity", "main_activity", "vid_deyatelnosti", "okedkz"])
    oked = _pick(nested, ["oked", "oked_code", "OKED", "ОКЭД", "Код ОКЭД"])
    registration_date = _pick(nested, ["datereg", "date_reg", "registration_date", "reg_date", "date_registration"])
    phone = _extract_phone(legal_address or "")
    return CompanyEnrichment(
        bin=bin_number,
        name=name,
        legal_address=legal_address,
        director=director,
        activity=activity,
        oked=oked,
        registration_date=registration_date,
        phone=phone,
        region=detect_region(legal_address),
        city=detect_city(legal_address),
        raw=_trim_raw(nested),
    )


def _extract_phone(text: str) -> str | None:
    if not text:
        return None
    match = re.search(r"(?:\+?7|8)[\s\-\(\)]*\d{3}[\s\-\(\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}", text)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    return match.group(0).strip()


def _select_trusted_record(bin_number: str, applicant_name: str, records: list[dict[str, Any]]) -> CandidateScore | None:
    candidates = _score_candidates(bin_number, applicant_name, records)
    # Trusted match is based only on exact BIN and company-name similarity.
    # OKED/TPI is informational only and must not block enrichment.
    trusted = [c for c in candidates if c.bin_matches and c.name_score >= EGOV_MIN_NAME_MATCH]
    if not trusted:
        return None
    trusted.sort(key=lambda c: (c.name_score, c.oked_tpi), reverse=True)
    return trusted[0]


def _score_candidates(bin_number: str, applicant_name: str, records: list[dict[str, Any]]) -> list[CandidateScore]:
    result: list[CandidateScore] = []
    for record in _dedupe_records(records):
        enr = _parse_company_record(bin_number, record)
        name_score = _name_similarity(applicant_name, enr.name or "")
        oked_tpi, oked_reason = _is_tpi_activity(enr.oked, enr.activity, record)
        result.append(CandidateScore(record, enr, name_score, oked_tpi, oked_reason, _record_bin_matches(bin_number, record)))
    result.sort(key=lambda c: (c.bin_matches, c.name_score, c.oked_tpi), reverse=True)
    return result


def _candidate_previews(bin_number: str, applicant_name: str, records: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    return [
        {
            "bin_matches": c.bin_matches,
            "bin": _extract_record_bin(c.record),
            "name": c.enrichment.name,
            "name_score": c.name_score,
            "oked": c.enrichment.oked,
            "activity": c.enrichment.activity,
            "oked_tpi": c.oked_tpi,
            "oked_reason": c.oked_reason,
            "address": c.enrichment.legal_address,
        }
        for c in _score_candidates(bin_number, applicant_name, records)[:limit]
    ]


def _name_similarity(a: str, b: str) -> int:
    na = _normalize_company_name(a)
    nb = _normalize_company_name(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 100
    if na in nb or nb in na:
        shorter = min(len(na), len(nb))
        longer = max(len(na), len(nb))
        return max(90, int(shorter / longer * 100))
    return int(round(SequenceMatcher(None, na, nb).ratio() * 100))


def _normalize_company_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").lower().replace("ё", "е")
    value = re.sub(r"[\"“”«»'`’\.,\-_–—№]", " ", value)
    value = re.sub(r"[^0-9a-zа-яәіңғүұқөһ\s]", " ", value, flags=re.IGNORECASE)
    stop_phrases = [
        "товарищество с ограниченной ответственностью",
        "партнерство с ограниченной ответственностью",
        "общество с ограниченной ответственностью",
        "акционерное общество",
        "индивидуальный предприниматель",
        "жауапкершілігі шектеулі серіктестігі",
        "жеке компаниясы",
        "limited liability partnership",
        "limited liability company",
        "private company",
        "товарищество",
        "партнерство",
        "общество",
        "компаниясы",
        "серіктестігі",
        "сериктестиги",
        "too", "тоо", "llp", "llc", "ltd", "limited", "private", "company", "inc", "co",
    ]
    for phrase in stop_phrases:
        value = re.sub(rf"\b{re.escape(phrase)}\b", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _is_tpi_activity(oked: str | None, activity: str | None, record: dict[str, Any] | None = None) -> tuple[bool, str]:
    record = record or {}
    nested = record.get("_source") if isinstance(record.get("_source"), dict) else record
    code_text = " ".join(str(x or "") for x in [oked, _pick(nested, ["oked", "oked_code", "OKED", "ОКЭД", "Код ОКЭД"])])
    for match in re.finditer(r"\b(\d{2})(?:[\.]?\d{0,4})?\b", code_text):
        prefix = match.group(1)
        if prefix in ALLOWED_OKED_PREFIXES:
            return True, f"oked_prefix_{prefix}"
        if prefix == "06":
            return False, "oked_prefix_06_oil_gas_not_tpi"

    ru_activity = _pick(nested, ["okedru", "oked_name_ru", "okedNameRu", "activity_ru", "activity", "main_activity"]) or activity or ""
    text = _normalize_for_keyword_search(ru_activity)
    if any(word in text for word in ["нефть", "нефт", "газ", "природного газа", "сырой нефти"]):
        return False, "activity_oil_gas_not_tpi"

    positive_any = [
        "уголь", "угля", "руда", "руд", "металл", "редк", "драгоцен", "полезн ископ",
        "прочих полезных ископаемых", "минерал", "карьер", "камень", "песок", "глина", "гравий",
        "щебень", "соль", "недр", "горнодобыва", "горных работ", "геолог", "разведк",
    ]
    if any(word in text for word in positive_any):
        if any(word in text for word in ["добыч", "разработ", "предоставление услуг", "геолог", "разведк", "недр"]):
            return True, "activity_keywords_tpi"
    if "добыч" in text and any(word in text for word in ["ископ", "руд", "металл", "минерал"]):
        return True, "activity_dobycha_tpi"
    return False, "oked_activity_not_tpi"


def _normalize_for_keyword_search(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-яәіңғүұқөһ\s]", " ", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()
