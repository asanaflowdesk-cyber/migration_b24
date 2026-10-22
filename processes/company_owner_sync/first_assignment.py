from __future__ import annotations

import copy
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from eqazyna_bitrix.models import TECHNICAL_USER_ID
from sync_founder_packages import normalized_id

DB_NAME = "first_assignment.sqlite3"
KINDS = {"contact", "company", "lead"}


def entity_nodes(package: dict[str, Any]) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    def add(kind: str, node: dict[str, Any]) -> None:
        item_id = normalized_id(node.get("id"))
        if item_id:
            result[f"{kind}:{item_id}"] = {"entity": kind, **node, "id": item_id}

    for node in package.get("contacts", []):
        add("contact", node)
    for company in package.get("companies", []):
        add("company", company)
        for lead in company.get("leads", []):
            add("lead", lead)
    for lead in package.get("direct_contact_leads", []):
        add("lead", lead)
    return list(result.values())


def entity_key(kind: str, item_id: Any) -> str:
    return f"{kind}:{int(item_id)}"


def package_key(package: dict[str, Any]) -> str:
    explicit = package.get("package_key")
    return str(explicit) if explicit else "founder:" + " ".join(str(package["fio"]).casefold().replace("ё", "е").split())


def complete_packages(
    packages: list[dict[str, Any]],
    snapshot: dict[str, list[dict[str, Any]]],
    skipped: list[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Add company/contact/lead-only packets without bypassing FIO conflicts."""
    result = copy.deepcopy(packages)
    linked_contact_ids = {normalized_id(row.get("CONTACT_ID")) for row in snapshot["leads"]}
    for package in result:
        linked_roots = [int(node["id"]) for node in package.get("contacts", []) if int(node["id"]) in linked_contact_ids]
        if linked_roots:
            package["authority_root_contact_ids"] = linked_roots
    used = {entity_key(n["entity"], n["id"]) for p in result for n in entity_nodes(p)}
    blocked = {normalized_id(item.get("company_id")) for item in skipped}
    leads = snapshot["leads"]

    def lead_node(row: dict[str, Any]) -> dict[str, Any]:
        return {"id": int(row["ID"]), "title": str(row.get("TITLE") or ""), "owner_id": normalized_id(row.get("ASSIGNED_BY_ID"))}

    for company in snapshot["companies"]:
        company_id = normalized_id(company.get("ID"))
        if not company_id or company_id in blocked or entity_key("company", company_id) in used:
            continue
        related = [row for row in leads if normalized_id(row.get("COMPANY_ID")) == company_id]
        if not related:
            continue
        packet = {
            "package_key": f"company:{company_id}", "fio": f"Компания {company_id}", "technical_packet_only": True,
            "contacts": [], "companies": [{
                "id": company_id, "title": str(company.get("TITLE") or ""),
                "owner_id": normalized_id(company.get("ASSIGNED_BY_ID")),
                "leads": [lead_node(row) for row in related],
            }],
        }
        result.append(packet)
        used.update(entity_key(n["entity"], n["id"]) for n in entity_nodes(packet))

    contacts = {normalized_id(row.get("ID")): row for row in snapshot["contacts"]}
    orphan_groups: dict[str, list[dict[str, Any]]] = {}
    for lead in leads:
        lead_id = normalized_id(lead.get("ID"))
        company_id = normalized_id(lead.get("COMPANY_ID"))
        if not lead_id or company_id in blocked or entity_key("lead", lead_id) in used:
            continue
        # A company with conflicting explicit FIO links is never reclassified
        # into a smaller, seemingly unambiguous packet.
        if company_id:
            continue
        contact_id = normalized_id(lead.get("CONTACT_ID"))
        key = f"contact:{contact_id}" if contact_id and contact_id in contacts else f"lead:{lead_id}"
        orphan_groups.setdefault(key, []).append(lead)
    for key, group in orphan_groups.items():
        contact_id = normalized_id(group[0].get("CONTACT_ID"))
        contact = contacts.get(contact_id)
        packet = {"package_key": key, "fio": key, "technical_packet_only": True, "companies": [], "contacts": [], "direct_contact_leads": [lead_node(row) for row in group]}
        if contact and entity_key("contact", contact_id) not in used:
            packet["contacts"] = [{"id": contact_id, "title": " ".join(str(contact.get(k) or "") for k in ("LAST_NAME", "NAME", "SECOND_NAME")).strip(), "owner_id": normalized_id(contact.get("ASSIGNED_BY_ID"))}]
        result.append(packet)
    return result


@contextmanager
def database(output_dir: Path, *, write: bool) -> Iterator[sqlite3.Connection]:
    path = output_dir / DB_NAME
    if write:
        output_dir.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30)
    else:
        connection = sqlite3.connect(":memory:")
        if path.is_file():
            with sqlite3.connect(path, timeout=30) as source:
                source.backup(connection)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS owner_events (
                seq INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL, previous_owner_id INTEGER,
                event_time REAL NOT NULL, received_at TEXT NOT NULL
            );
        """)
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _state(connection: sqlite3.Connection, key: str, default: Any) -> Any:
    row = connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _save(connection: sqlite3.Connection, key: str, value: Any) -> None:
    connection.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))


def event_cursor(output_dir: Path) -> int:
    path = output_dir / DB_NAME
    if not path.is_file():
        return 0
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30) as connection:
        return int(_state(connection, "event_cursor", 0))


def ingest_events(output_dir: Path, events: list[dict[str, Any]]) -> int:
    with database(output_dir, write=True) as connection:
        cursor = int(_state(connection, "event_cursor", 0))
        added = 0
        for event in events:
            seq = normalized_id(event.get("seq"))
            kind = str(event.get("entity") or "").lower()
            item_id = normalized_id(event.get("entity_id"))
            owner_id = normalized_id(event.get("owner_id")) or 0
            if not seq or kind not in KINDS or not item_id:
                raise ValueError("invalid_owner_event")
            try:
                stamp = float(event.get("event_ts") or 0)
            except (TypeError, ValueError):
                raise ValueError("invalid_owner_event_timestamp") from None
            if not math.isfinite(stamp) or stamp < 0:
                raise ValueError("invalid_owner_event_timestamp")
            previous = normalized_id(event.get("previous_owner_id"))
            received = str(event.get("received_at") or "")
            existing = connection.execute("SELECT entity,entity_id,owner_id,previous_owner_id FROM owner_events WHERE seq=?", (seq,)).fetchone()
            if existing and tuple(existing) != (kind, item_id, owner_id, previous):
                raise ValueError("owner_event_sequence_reused")
            result = connection.execute("INSERT OR IGNORE INTO owner_events VALUES(?,?,?,?,?,?,?)", (seq, kind, item_id, owner_id, previous, stamp, received))
            added += result.rowcount
            cursor = max(cursor, seq)
        _save(connection, "event_cursor", cursor)
        return added


def prepare_first_assignments(
    packages: list[dict[str, Any]],
    output_dir: Path,
    *,
    apply: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Commit the first manager before any CRM write; later events cannot replace it.

    Without an owner-event journal, a sole real owner can be recovered safely.
    Multiple already-assigned owners without a recorded order are a conflict.
    """
    prepared = copy.deepcopy(packages)
    conflicts: list[dict[str, Any]] = []
    with database(output_dir, write=apply) as connection:
        records = _state(connection, "packages", {})
        events_by_entity: dict[str, list[dict[str, Any]]] = {}
        for row in connection.execute("SELECT * FROM owner_events ORDER BY event_time,seq"):
            events_by_entity.setdefault(entity_key(row["entity"], row["entity_id"]), []).append(dict(row))
        for package in prepared:
            key = package_key(package)
            nodes = entity_nodes(package)
            keys = {entity_key(node["entity"], node["id"]) for node in nodes}
            matches = [name for name, record in records.items() if key in record.get("aliases", [name]) or keys.intersection(record.get("entities", []))]
            locked_owners = {records[name].get("owner_id") for name in matches if records[name].get("owner_id")}
            if len(locked_owners) > 1:
                package["first_assignment_conflict"] = "overlapping_first_assignments"
                conflicts.append({"type": "overlapping_first_assignments", "fio": package["fio"]})
                continue
            record_key = next((name for name in matches if records[name].get("owner_id")), matches[0] if matches else key)
            record = records.get(record_key)
            package_events = sorted(
                (event for node_key in keys for event in events_by_entity.get(node_key, [])),
                key=lambda event: (event["event_time"], event["seq"]),
            )
            root_ids = set(package.get("authority_root_contact_ids", []))
            contact_roots = [node for node in package.get("contacts", []) if not root_ids or int(node["id"]) in root_ids]
            roots = contact_roots or list(package.get("companies", [])) or list(package.get("direct_contact_leads", []))
            root_keys = {entity_key("contact" if node in package.get("contacts", []) else "company" if node in package.get("companies", []) else "lead", node["id"]) for node in roots}
            actual_owners = {normalized_id(node.get("owner_id")) for node in nodes} - {None, TECHNICAL_USER_ID}
            technical = (
                any(normalized_id(node.get("owner_id")) == TECHNICAL_USER_ID for node in roots)
                or (package.get("technical_packet_only") and len(actual_owners) <= 1 and any(normalized_id(node.get("owner_id")) == TECHNICAL_USER_ID for node in nodes))
                or any(entity_key(event["entity"], event["entity_id"]) in root_keys and event["previous_owner_id"] == TECHNICAL_USER_ID for event in package_events)
            )
            if record is None and not technical:
                continue
            if record is None:
                record = {"owner_id": None, "aliases": [key], "entities": [], "technical_since": datetime.now(timezone.utc).isoformat()}
            aliases = set(record.get("aliases", [])) | {key}
            known_keys = set(record.get("entities", [])) | keys
            for name in matches:
                aliases.update(records[name].get("aliases", [name]))
                known_keys.update(records[name].get("entities", []))
                if name != record_key:
                    del records[name]
            record["aliases"] = sorted(aliases)
            record["entities"] = sorted(known_keys)
            if not record.get("owner_id"):
                relevant_events = sorted(
                    (event for node_key in known_keys for event in events_by_entity.get(node_key, [])),
                    key=lambda event: (event["event_time"], event["seq"]),
                )
                candidates = [event for event in relevant_events if entity_key(event["entity"], event["entity_id"]) in known_keys and event["owner_id"] not in (0, TECHNICAL_USER_ID) and event["previous_owner_id"] in (None, TECHNICAL_USER_ID)]
                if candidates:
                    first = candidates[0]
                    record.update(owner_id=first["owner_id"], source_entity=first["entity"], source_entity_id=first["entity_id"], source_event_seq=first["seq"], source_event_ts=first["event_time"], selection="owner_event")
                else:
                    actual = {normalized_id(node.get("owner_id")) for node in nodes} - {None, TECHNICAL_USER_ID}
                    if len(actual) == 1:
                        owner = next(iter(actual))
                        first_node = next(node for node in nodes if normalized_id(node.get("owner_id")) == owner)
                        record.update(owner_id=owner, source_entity=first_node["entity"], source_entity_id=first_node["id"], selection="single_observed_manager")
                    elif len(actual) > 1:
                        package["first_assignment_conflict"] = "first_assignment_order_unknown"
                        conflicts.append({"type": "first_assignment_order_unknown", "fio": package["fio"], "company_title": "owner_ids=" + ",".join(map(str, sorted(actual)))})
            records[record_key] = record
            if record.get("owner_id"):
                package["locked_owner_id"] = int(record["owner_id"])
                package["first_assignment"] = copy.deepcopy(record)
                package["sync_package_contacts"] = True
                # All contacts, including the former authority, are targets.
                package["authority_contact_ids"] = []
                package["owner_id"] = int(record["owner_id"])
                ids = [int(node["id"]) for node in package.get("contacts", [])]
                package["source_contact_id"] = min(ids) if ids else 0
        _save(connection, "packages", records)
    return prepared, conflicts
