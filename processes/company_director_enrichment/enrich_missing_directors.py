from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests
import xlsxwriter

from eqazyna_bitrix.bitrix_client import BitrixClient
from eqazyna_bitrix.settings import Settings


DIRECTOR_MARKER = "EQAZYNA_DIRECTOR: external_enrichment"
SOURCE_ORDER = ("adata", "kompra", "ba_prg")
SOURCE_LABEL = {"adata": "Adata", "kompra": "Kompra", "ba_prg": "Бизнес Аналитик"}
NAME_TOKEN = r"[A-Za-zА-Яа-яЁёӘәҒғҚқҢңӨөҰұҮүҺһІі'’\-]{2,}"
NAME_RE = re.compile(NAME_TOKEN)
BIN_RE = re.compile(r"^\d{12}$")
STOP_WORDS = {
    "дата", "бин", "иин", "статус", "регион", "юридический", "юридический адрес",
    "наличие", "участие", "проверено", "форма", "размер", "вид", "информация",
    "нет данных", "доступно", "рынке", "крп", "ксе", "основной", "адрес", "налоги",
}
INVALID_FIO_PHRASES = {
    "нет данных", "информация скрыта", "информация в источнике отсутствует", "не найдено",
    "доступно после входа", "руководитель", "первый руководитель",
}
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
USER_AGENT = "Mozilla/5.0 (compatible; B24DirectorEnrichment/1.0; +https://github.com/asanaflowdesk-cyber/migration_b24)"


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript", "svg"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(value: str) -> str:
    parser = VisibleTextParser()
    try:
        parser.feed(value or "")
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def normalize_id(value: Any) -> int | None:
    raw = str(value or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def normalize_bin(value: Any) -> str:
    raw = re.sub(r"\D", "", str(value or ""))
    return raw if BIN_RE.fullmatch(raw) else ""


def normalize_fio(value: Any) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip(" \t\r\n:;,.—–-"))
    return raw.upper().replace("Ё", "Е")


def fio_key(value: Any) -> str:
    return re.sub(r"[^A-ZА-ЯӘҒҚҢӨҰҮҺІ]+", " ", normalize_fio(value)).strip()


def valid_fio(value: Any) -> bool:
    raw = normalize_fio(value)
    if not raw or raw.casefold() in INVALID_FIO_PHRASES or any(char.isdigit() for char in raw):
        return False
    words = NAME_RE.findall(raw)
    return 2 <= len(words) <= 4 and " ".join(words).casefold() not in INVALID_FIO_PHRASES


def fio_parts(value: str) -> tuple[str, str, str] | None:
    words = NAME_RE.findall(normalize_fio(value).title())
    if len(words) < 2:
        return None
    return words[0], words[1], " ".join(words[2:])


def extract_labeled_fio(text: str, labels: list[str]) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    lowered = compact.casefold()
    for label in labels:
        start = lowered.find(label.casefold())
        if start < 0:
            continue
        tail = compact[start + len(label): start + len(label) + 220].lstrip(" :;-—")
        if not tail:
            continue
        lower_tail = tail.casefold()
        if any(lower_tail.startswith(phrase) for phrase in INVALID_FIO_PHRASES):
            continue
        cut = len(tail)
        for stop in STOP_WORDS | INVALID_FIO_PHRASES:
            match = re.search(rf"\b{re.escape(stop)}\b", lower_tail)
            if match and match.start() > 0:
                cut = min(cut, match.start())
        segment = tail[:cut].strip(" :;-—,.()")
        words = NAME_RE.findall(segment)
        if 2 <= len(words) <= 4:
            candidate = " ".join(words)
            if valid_fio(candidate):
                return normalize_fio(candidate)
        if len(words) > 4:
            candidate = " ".join(words[:3])
            if valid_fio(candidate):
                return normalize_fio(candidate)
    return ""


def is_director_contact(contact: dict[str, Any]) -> bool:
    post = str(contact.get("POST") or "").casefold()
    comments = str(contact.get("COMMENTS") or "")
    return (
        "руковод" in post
        or "директор" in post
        or "учред" in post
        or "EQAZYNA_DIRECTOR:" in comments
    )


def contact_fio(contact: dict[str, Any]) -> str:
    return normalize_fio(" ".join(
        str(contact.get(field) or "").strip()
        for field in ("LAST_NAME", "NAME", "SECOND_NAME")
        if str(contact.get(field) or "").strip()
    ))


def new_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-KZ,ru;q=0.9,en;q=0.6"})
    return session


def fetch_html(url: str, timeout: int) -> tuple[str, str, int, str]:
    session = new_http_session()
    last_error = ""
    for attempt in range(3):
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            if response.status_code in RETRYABLE_HTTP and attempt < 2:
                retry_after = response.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else (0.5 + attempt)
                time.sleep(min(delay, 3.0))
                continue
            if response.status_code >= 400:
                return "", str(response.url), response.status_code, f"HTTP_{response.status_code}"
            return response.text, str(response.url), response.status_code, ""
        except requests.RequestException as exc:
            last_error = type(exc).__name__
            if attempt < 2:
                time.sleep(0.5 + attempt)
    return "", url, 0, last_error or "request_failed"


def page_has_bin(text: str, bin_number: str) -> bool:
    return bool(re.search(rf"(?<!\d){re.escape(bin_number)}(?!\d)", text or ""))


def source_result(source: str, bin_number: str, timeout: int, url: str) -> dict[str, Any]:
    html, final_url, http_status, error = fetch_html(url, timeout)
    text = html_to_text(html)
    if error:
        return {"source": source, "url": final_url, "director": "", "status": error, "http": http_status}
    if not page_has_bin(text, bin_number):
        return {"source": source, "url": final_url, "director": "", "status": "bin_mismatch", "http": http_status}
    labels = {
        "adata": ["Руководитель"],
        "kompra": ["Первый руководитель", "Руководитель"],
        "ba_prg": ["Руководитель компании", "Руководитель"],
    }[source]
    director = extract_labeled_fio(text, labels)
    return {
        "source": source,
        "url": final_url,
        "director": director,
        "status": "found" if director else "no_director",
        "http": http_status,
    }


def base_source_results(bin_number: str, timeout: int) -> list[dict[str, Any]]:
    urls = {
        "adata": f"https://pk.adata.kz/counterparty/main/company/{bin_number}/basic-info",
        "kompra": f"https://kompra.kz/organization/{bin_number}",
    }
    return [source_result(source, bin_number, timeout, urls[source]) for source in ("adata", "kompra")]


def discover_ba_urls(bin_numbers: set[str], timeout: int, doc_limit: int) -> dict[str, str]:
    """Resolve canonical ba.prg.kz company URLs through the site's public sitemap.

    No search-engine scraping and no authentication bypass: only the public sitemap
    and public company pages are used. The traversal is bounded so a site change
    cannot make the workflow hang indefinitely.
    """
    wanted = set(bin_numbers)
    found: dict[str, str] = {}
    if not wanted or doc_limit <= 0:
        return found
    session = new_http_session()
    queue = ["https://ba.prg.kz/sitemaps/sitemap.xml"]
    seen: set[str] = set()
    while queue and len(seen) < doc_limit and len(found) < len(wanted):
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            if response.status_code >= 400:
                continue
            root = ET.fromstring(response.content)
        except (requests.RequestException, ET.ParseError, OSError):
            continue
        locs = [str(node.text or "").strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "loc"]
        for loc in locs:
            if not loc:
                continue
            if loc.endswith((".xml", ".xml.gz")) or "/sitemaps/" in loc and not re.search(r"/\d{12}-", loc):
                if loc not in seen and len(seen) + len(queue) < doc_limit:
                    queue.append(loc)
                continue
            for bin_number in wanted - set(found):
                if re.search(rf"/{re.escape(bin_number)}(?:-|/)", loc):
                    found[bin_number] = loc
    return found


def choose_director(results: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        if valid_fio(item.get("director")):
            groups[fio_key(item["director"])].append(item)
    if not groups:
        return {"status": "no_result", "director": "", "source": "", "url": "", "evidence": ""}
    ranked = sorted(groups.items(), key=lambda pair: (-len(pair[1]), SOURCE_ORDER.index(pair[1][0]["source"])))
    best_key, best_items = ranked[0]
    if len(groups) > 1 and (len(ranked) == 1 or len(best_items) == len(ranked[1][1])):
        return {
            "status": "source_conflict",
            "director": "",
            "source": "",
            "url": "",
            "evidence": " | ".join(f"{item['source']}={item['director']}" for items in groups.values() for item in items),
        }
    preferred = min(best_items, key=lambda item: SOURCE_ORDER.index(item["source"]))
    return {
        "status": "accepted",
        "director": normalize_fio(preferred["director"]),
        "source": preferred["source"],
        "url": preferred["url"],
        "evidence": ",".join(item["source"] for item in best_items),
        "confidence": "confirmed" if len(best_items) >= 2 else "single_source",
        "key": best_key,
    }


def clone_client(client: BitrixClient) -> BitrixClient:
    return BitrixClient(
        client.webhook_url,
        timeout=client.timeout,
        retries=client.retries,
        polite_delay_seconds=0.0,
        verify_ssl=client.verify_ssl,
    )


def load_snapshot(client: BitrixClient) -> dict[str, list[dict[str, Any]]]:
    specs = {
        "companies": ("crm.company.list", {}, ["ID", "TITLE", "ASSIGNED_BY_ID", "ORIGIN_ID"]),
        "requisites": ("crm.requisite.list", {"ENTITY_TYPE_ID": 4}, ["ID", "ENTITY_ID", "PRESET_ID", "RQ_INN", "RQ_DIRECTOR"]),
        "contacts": ("crm.contact.list", {}, ["ID", "COMPANY_ID", "LAST_NAME", "NAME", "SECOND_NAME", "POST", "COMMENTS", "ASSIGNED_BY_ID"]),
    }

    def read(name: str) -> tuple[str, list[dict[str, Any]]]:
        method, filter_, select = specs[name]
        local = clone_client(client)
        rows = local.list_all(method, {"order": {"ID": "ASC"}, "filter": filter_, "select": select})
        return name, rows

    result: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="director-snapshot") as pool:
        futures = [pool.submit(read, name) for name in specs]
        for future in as_completed(futures):
            name, rows = future.result()
            result[name] = rows
    return result


def build_candidates(snapshot: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requisites_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    contacts_by_company: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["requisites"]:
        if (company_id := normalize_id(row.get("ENTITY_ID"))) is not None:
            requisites_by_company[company_id].append(row)
    for row in snapshot["contacts"]:
        if (company_id := normalize_id(row.get("COMPANY_ID"))) is not None:
            contacts_by_company[company_id].append(row)

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for company in snapshot["companies"]:
        company_id = normalize_id(company.get("ID"))
        if company_id is None:
            continue
        requisites = requisites_by_company.get(company_id, [])
        contacts = contacts_by_company.get(company_id, [])
        req_directors = [normalize_fio(row.get("RQ_DIRECTOR")) for row in requisites if valid_fio(row.get("RQ_DIRECTOR"))]
        contact_directors = [contact_fio(row) for row in contacts if is_director_contact(row) and valid_fio(contact_fio(row))]
        if req_directors or contact_directors:
            skipped.append({"company_id": company_id, "title": company.get("TITLE", ""), "status": "director_already_present"})
            continue

        bins = {normalize_bin(row.get("RQ_INN")) for row in requisites if normalize_bin(row.get("RQ_INN"))}
        origin_bin = normalize_bin(company.get("ORIGIN_ID"))
        if not bins and origin_bin:
            bins.add(origin_bin)
        if len(bins) != 1:
            skipped.append({
                "company_id": company_id,
                "title": company.get("TITLE", ""),
                "status": "no_bin" if not bins else "bin_conflict",
                "bins": ",".join(sorted(bins)),
            })
            continue
        candidates.append({
            "company_id": company_id,
            "title": str(company.get("TITLE") or "").strip(),
            "owner_id": normalize_id(company.get("ASSIGNED_BY_ID")),
            "bin": next(iter(bins)),
            "requisite_ids": [normalize_id(row.get("ID")) for row in requisites if normalize_id(row.get("ID"))],
            "status": "candidate",
        })
    return candidates, skipped


def enrich_candidates(candidates: list[dict[str, Any]], workers: int, timeout: int) -> list[dict[str, Any]]:
    by_id = {item["company_id"]: dict(item) for item in candidates}
    source_map: dict[int, list[dict[str, Any]]] = defaultdict(list)

    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="director-web") as pool:
        futures = {pool.submit(base_source_results, item["bin"], timeout): item["company_id"] for item in candidates}
        done = 0
        for future in as_completed(futures):
            company_id = futures[future]
            done += 1
            try:
                source_map[company_id].extend(future.result())
            except Exception as exc:  # noqa: BLE001
                source_map[company_id].append({"source": "web", "url": "", "director": "", "status": type(exc).__name__, "http": 0})
            print(f"[DIRECTOR] web {done}/{len(candidates)} company={company_id}", flush=True)

    need_ba: list[dict[str, Any]] = []
    for item in candidates:
        decision = choose_director(source_map[item["company_id"]])
        if decision["status"] != "accepted":
            need_ba.append(item)

    if need_ba:
        limit = max(1, int(os.getenv("BA_SITEMAP_DOC_LIMIT", "80") or 80))
        ba_urls = discover_ba_urls({item["bin"] for item in need_ba}, timeout, limit)
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 6)), thread_name_prefix="director-ba") as pool:
            futures = {}
            for item in need_ba:
                url = ba_urls.get(item["bin"]) or f"https://ba.prg.kz/000000000-unknown/{item['bin']}-{item['bin']}/"
                futures[pool.submit(source_result, "ba_prg", item["bin"], timeout, url)] = item["company_id"]
            for future in as_completed(futures):
                company_id = futures[future]
                try:
                    source_map[company_id].append(future.result())
                except Exception as exc:  # noqa: BLE001
                    source_map[company_id].append({"source": "ba_prg", "url": "", "director": "", "status": type(exc).__name__, "http": 0})

    rows: list[dict[str, Any]] = []
    for company_id, item in by_id.items():
        decision = choose_director(source_map[company_id])
        item.update(decision)
        for source in SOURCE_ORDER:
            result = next((row for row in source_map[company_id] if row.get("source") == source), None)
            item[f"{source}_status"] = result.get("status", "not_checked") if result else "not_checked"
            item[f"{source}_director"] = result.get("director", "") if result else ""
            item[f"{source}_url"] = result.get("url", "") if result else ""
        rows.append(item)
    return rows


def fresh_company_state(client: BitrixClient, company_id: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    company = client.call("crm.company.get", {"id": company_id})
    requisites = client.list_all(
        "crm.requisite.list",
        {"order": {"ID": "ASC"}, "filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": company_id}, "select": ["ID", "ENTITY_ID", "RQ_INN", "RQ_DIRECTOR"]},
    )
    contacts = client.list_all(
        "crm.contact.list",
        {"order": {"ID": "ASC"}, "filter": {"COMPANY_ID": company_id}, "select": ["ID", "COMPANY_ID", "LAST_NAME", "NAME", "SECOND_NAME", "POST", "COMMENTS", "ASSIGNED_BY_ID"]},
    )
    return company if isinstance(company, dict) else None, requisites, contacts


def current_bin(company: dict[str, Any], requisites: list[dict[str, Any]]) -> str:
    bins = {normalize_bin(row.get("RQ_INN")) for row in requisites if normalize_bin(row.get("RQ_INN"))}
    if len(bins) == 1:
        return next(iter(bins))
    if not bins:
        return normalize_bin(company.get("ORIGIN_ID"))
    return ""


def provenance_comments(existing: str, source: str, source_url: str) -> str:
    lines = [line.rstrip() for line in str(existing or "").splitlines() if line.strip()]
    marker_lines = [
        DIRECTOR_MARKER,
        f"DIRECTOR_SOURCE: {SOURCE_LABEL.get(source, source)}",
        f"DIRECTOR_SOURCE_URL: {source_url}",
        f"DIRECTOR_CHECKED_AT: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
    ]
    lowered = "\n".join(lines).casefold()
    for line in marker_lines:
        if line.casefold() not in lowered:
            lines.append(line)
    return "\n".join(lines)


def apply_one(client: BitrixClient, row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if row.get("status") != "accepted":
        return result
    company_id = int(row["company_id"])
    director = normalize_fio(row["director"])
    parts = fio_parts(director)
    if parts is None:
        result["status"] = "invalid_director_fio"
        return result

    company, requisites, contacts = fresh_company_state(client, company_id)
    if not company:
        result["status"] = "company_missing"
        return result
    if current_bin(company, requisites) != row["bin"]:
        result["status"] = "bin_changed"
        return result
    fresh_req_directors = [normalize_fio(item.get("RQ_DIRECTOR")) for item in requisites if valid_fio(item.get("RQ_DIRECTOR"))]
    fresh_director_contacts = [item for item in contacts if is_director_contact(item) and valid_fio(contact_fio(item))]
    if fresh_req_directors or fresh_director_contacts:
        existing = fresh_req_directors + [contact_fio(item) for item in fresh_director_contacts]
        result["status"] = "already_enriched" if all(fio_key(value) == fio_key(director) for value in existing) else "fresh_director_conflict"
        result["existing_director"] = " | ".join(existing)
        return result

    owner_id = normalize_id(company.get("ASSIGNED_BY_ID"))
    if not owner_id:
        result["status"] = "company_without_owner"
        return result

    exact_contacts = [item for item in contacts if valid_fio(contact_fio(item)) and fio_key(contact_fio(item)) == fio_key(director)]
    contact_id: int | None = None
    if exact_contacts:
        existing = exact_contacts[0]
        existing_owner = normalize_id(existing.get("ASSIGNED_BY_ID"))
        if existing_owner and existing_owner != owner_id:
            result["status"] = "matching_contact_owner_conflict"
            result["contact_id"] = normalize_id(existing.get("ID")) or 0
            return result
        contact_id = normalize_id(existing.get("ID"))
        fields: dict[str, Any] = {"COMMENTS": provenance_comments(existing.get("COMMENTS", ""), row["source"], row["url"])}
        if not str(existing.get("POST") or "").strip():
            fields["POST"] = "Руководитель"
        if not existing_owner:
            fields["ASSIGNED_BY_ID"] = owner_id
        client.call("crm.contact.update", {"id": contact_id, "fields": fields})
    else:
        last_name, first_name, second_name = parts
        fields = {
            "LAST_NAME": last_name,
            "NAME": first_name,
            "SECOND_NAME": second_name,
            "POST": "Руководитель",
            "COMPANY_ID": company_id,
            "ASSIGNED_BY_ID": owner_id,
            "COMMENTS": provenance_comments("", row["source"], row["url"]),
        }
        added = client.call("crm.contact.add", {"fields": fields})
        contact_id = normalize_id(added)
        if not contact_id:
            result["status"] = "contact_create_failed"
            return result

    updated_requisites: list[int] = []
    for requisite in requisites:
        req_id = normalize_id(requisite.get("ID"))
        if not req_id or valid_fio(requisite.get("RQ_DIRECTOR")):
            continue
        req_bin = normalize_bin(requisite.get("RQ_INN"))
        if req_bin and req_bin != row["bin"]:
            continue
        client.call("crm.requisite.update", {"id": req_id, "fields": {"RQ_DIRECTOR": director}})
        updated_requisites.append(req_id)

    verify_contact = client.call("crm.contact.get", {"id": contact_id})
    if not isinstance(verify_contact, dict) or normalize_id(verify_contact.get("COMPANY_ID")) != company_id or fio_key(contact_fio(verify_contact)) != fio_key(director) or normalize_id(verify_contact.get("ASSIGNED_BY_ID")) != owner_id or not is_director_contact(verify_contact):
        result["status"] = "contact_verification_failed"
        result["contact_id"] = contact_id or 0
        return result
    if updated_requisites:
        _company, verify_requisites, _contacts = fresh_company_state(client, company_id)
        if not all(any(normalize_id(req.get("ID")) == req_id and fio_key(req.get("RQ_DIRECTOR")) == fio_key(director) for req in verify_requisites) for req_id in updated_requisites):
            result["status"] = "requisite_verification_failed"
            result["contact_id"] = contact_id or 0
            return result

    result["status"] = "updated" if updated_requisites else "updated_contact_only_no_requisite"
    result["contact_id"] = contact_id or 0
    result["requisites_updated"] = ",".join(str(value) for value in updated_requisites)
    return result


def apply_rows(client: BitrixClient, rows: list[dict[str, Any]], workers: int) -> list[dict[str, Any]]:
    accepted = [row for row in rows if row.get("status") == "accepted"]
    untouched = [row for row in rows if row.get("status") != "accepted"]
    results: list[dict[str, Any]] = []

    def work(row: dict[str, Any]) -> dict[str, Any]:
        local = clone_client(client)
        try:
            return apply_one(local, row)
        except Exception as exc:  # noqa: BLE001
            failed = dict(row)
            failed["status"] = "error"
            failed["error"] = type(exc).__name__
            return failed

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8)), thread_name_prefix="director-apply") as pool:
        future_map = {pool.submit(work, row): row for row in accepted}
        completed = 0
        for future in as_completed(future_map):
            completed += 1
            result = future.result()
            results.append(result)
            print(f"[DIRECTOR] apply {completed}/{len(accepted)} company={result['company_id']} status={result['status']}", flush=True)
    return sorted(untouched + results, key=lambda item: int(item["company_id"]))


def write_report(output_dir: Path, rows: list[dict[str, Any]], skipped: list[dict[str, Any]], apply: bool) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    details_path = output_dir / "company_director_enrichment.json"
    details_path.write_text(json.dumps({"apply": apply, "rows": rows, "skipped": skipped}, ensure_ascii=False, indent=2), encoding="utf-8")

    workbook = xlsxwriter.Workbook(output_dir / "company_director_enrichment.xlsx")
    header = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
    sheet = workbook.add_worksheet("Обогащение")
    columns = [
        ("company_id", "Компания ID"), ("title", "Компания"), ("bin", "БИН"),
        ("owner_id", "Ответственный ID"), ("director", "Найденный руководитель"),
        ("source", "Источник"), ("url", "Ссылка"), ("confidence", "Подтверждение"),
        ("status", "Результат"), ("contact_id", "Контакт ID"),
        ("requisites_updated", "Реквизиты обновлены"), ("error", "Ошибка"),
        ("adata_status", "Adata статус"), ("adata_director", "Adata руководитель"),
        ("kompra_status", "Kompra статус"), ("kompra_director", "Kompra руководитель"),
        ("ba_prg_status", "Бизнес Аналитик статус"), ("ba_prg_director", "Бизнес Аналитик руководитель"),
        ("evidence", "Совпавшие источники / конфликт"),
    ]
    for col, (_key, title) in enumerate(columns):
        sheet.write(0, col, title, header)
    for idx, row in enumerate(rows, 1):
        sheet.write_row(idx, 0, [row.get(key, "") for key, _title in columns])
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, max(len(rows), 1), len(columns) - 1)
    sheet.set_column(0, 0, 12); sheet.set_column(1, 1, 42); sheet.set_column(2, 3, 18)
    sheet.set_column(4, 7, 32); sheet.set_column(8, 11, 24); sheet.set_column(12, 18, 28)

    skip_sheet = workbook.add_worksheet("Пропуски")
    skip_columns = [("company_id", "Компания ID"), ("title", "Компания"), ("status", "Причина"), ("bins", "БИН / варианты")]
    for col, (_key, title) in enumerate(skip_columns):
        skip_sheet.write(0, col, title, header)
    for idx, row in enumerate(skipped, 1):
        skip_sheet.write_row(idx, 0, [row.get(key, "") for key, _title in skip_columns])
    skip_sheet.freeze_panes(1, 0)
    skip_sheet.autofilter(0, 0, max(len(skipped), 1), len(skip_columns) - 1)
    skip_sheet.set_column(0, 0, 12); skip_sheet.set_column(1, 1, 42); skip_sheet.set_column(2, 3, 28)
    workbook.close()

    summary = {
        "apply": apply,
        "candidates": len(rows),
        "accepted": sum(row.get("status") == "accepted" for row in rows),
        "updated": sum(str(row.get("status") or "").startswith("updated") for row in rows),
        "no_result": sum(row.get("status") == "no_result" for row in rows),
        "source_conflict": sum(row.get("status") == "source_conflict" for row in rows),
        "errors": sum(row.get("status") in {"error", "company_missing", "bin_changed", "fresh_director_conflict", "company_without_owner", "matching_contact_owner_conflict", "contact_create_failed", "contact_verification_failed", "requisite_verification_failed", "invalid_director_fio"} for row in rows),
        "skipped": len(skipped),
    }
    (output_dir / "company_director_enrichment_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Find Bitrix companies without a director and enrich them from public Kazakhstan business directories")
    parser.add_argument("--apply", action="store_true", help="Write director contact and RQ_DIRECTOR; default is dry-run")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-companies", type=int, default=0, help="0 = all candidates")
    parser.add_argument("--workers", type=int, default=int(os.getenv("DIRECTOR_ENRICH_WORKERS", "6") or 6))
    parser.add_argument("--http-timeout", type=int, default=int(os.getenv("DIRECTOR_HTTP_TIMEOUT", "12") or 12))
    args = parser.parse_args()
    if args.max_companies < 0 or args.workers <= 0 or args.http_timeout <= 0:
        parser.error("max-companies must be >= 0; workers/http-timeout must be > 0")

    settings = Settings.from_env()
    client = BitrixClient(
        settings.bitrix_webhook_url or "",
        timeout=settings.bitrix_request_timeout,
        polite_delay_seconds=settings.bitrix_polite_delay_seconds,
        verify_ssl=settings.bitrix_tls_verify,
    )
    print("[DIRECTOR] loading Bitrix companies/requisites/contacts", flush=True)
    snapshot = load_snapshot(client)
    candidates, skipped = build_candidates(snapshot)
    candidates.sort(key=lambda item: int(item["company_id"]))
    if args.max_companies:
        candidates = candidates[: args.max_companies]
    print(f"[DIRECTOR] candidates={len(candidates)} skipped={len(skipped)}", flush=True)
    rows = enrich_candidates(candidates, min(args.workers, 12), args.http_timeout)
    if args.apply:
        rows = apply_rows(client, rows, args.workers)
    summary = write_report(Path(args.output_dir), rows, skipped, args.apply)
    return 1 if args.apply and summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
