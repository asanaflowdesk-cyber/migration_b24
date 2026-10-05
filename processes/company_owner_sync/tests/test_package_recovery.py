from copy import deepcopy
import json

from authority_state import load_authority_sources, remember_authority_sources
from package_recovery import repair_package_residuals
from queue_founder_packages import process_claim
from reconcile_company_lead_owners import resolve_authority_packages
from sync_founder_packages import build_packages


class CRM:
    def __init__(self):
        self.contacts = {
            837: {"ID": "837", "LAST_NAME": "Касылова", "NAME": "Гульмира", "SECOND_NAME": "Жакуповна", "POST": "Генеральный директор", "COMMENTS": "", "COMPANY_ID": "933", "ASSIGNED_BY_ID": "69", "DATE_MODIFY": "2026-10-01"},
            838: {"ID": "838", "LAST_NAME": "Касылова", "NAME": "Гульмира", "SECOND_NAME": "Жакуповна", "POST": "Учредитель", "COMMENTS": "", "COMPANY_ID": "", "ASSIGNED_BY_ID": "72", "DATE_MODIFY": "2026-10-05"},
        }
        self.companies = {933: {"ID": "933", "TITLE": "A", "ASSIGNED_BY_ID": "72"}, 934: {"ID": "934", "TITLE": "B", "ASSIGNED_BY_ID": "71"}}
        self.leads = {
            2624: {"ID": "2624", "TITLE": "L1", "CONTACT_ID": "837", "COMPANY_ID": "933", "ASSIGNED_BY_ID": "72", "STATUS_ID": "NEW"},
            2625: {"ID": "2625", "TITLE": "L2", "CONTACT_ID": "837", "COMPANY_ID": "933", "ASSIGNED_BY_ID": "72", "STATUS_ID": "UC_P6PL43"},
            2626: {"ID": "2626", "TITLE": "L3", "CONTACT_ID": "", "COMPANY_ID": "934", "ASSIGNED_BY_ID": "71", "STATUS_ID": "CONVERTED"},
            2627: {"ID": "2627", "TITLE": "L4", "CONTACT_ID": "837", "COMPANY_ID": "", "ASSIGNED_BY_ID": "72", "STATUS_ID": "JUNK"},
        }
        self.requisites = [{"ID": "1", "ENTITY_ID": "934", "RQ_DIRECTOR": "Касылова Гульмира Жакуповна"}]
        self.writes = []
        self.drop_lead_write = False
        self.change_source = False

    def list_all(self, method, payload):
        data = {"company": self.companies, "contact": self.contacts, "lead": self.leads, "requisite": self.requisites}[method.split(".")[1]]
        return deepcopy(list(data.values()) if isinstance(data, dict) else data)

    def call(self, method, payload):
        if method == "batch":
            raise RuntimeError("serial test transport")
        entity = method.split(".")[1]
        data = {"company": self.companies, "contact": self.contacts, "lead": self.leads}[entity]
        return deepcopy(data[int(payload["id"])])

    def write(self, entity, item_id, fields):
        assert set(fields) == {"ASSIGNED_BY_ID"}
        self.writes.append((entity, int(item_id), dict(fields)))
        if self.change_source:
            self.contacts[837]["ASSIGNED_BY_ID"] = "70"
            self.change_source = False
        if entity == "lead" and self.drop_lead_write:
            self.drop_lead_write = False
            return
        {"company": self.companies, "contact": self.contacts, "lead": self.leads}[entity][int(item_id)].update(fields)

    def update_company(self, item_id, fields):
        self.write("company", item_id, fields)

    def update_contact(self, item_id, fields):
        self.write("contact", item_id, fields)

    def update_lead(self, item_id, fields):
        self.write("lead", item_id, fields)


def assert_owner(crm, owner):
    assert {int(row["ASSIGNED_BY_ID"]) for collection in (crm.contacts, crm.companies, crm.leads) for row in collection.values()} == {owner}


def test_lost_event_repairs_entire_package_from_lead_linked_contact(tmp_path):
    crm = CRM()
    statuses = {key: value["STATUS_ID"] for key, value in crm.leads.items()}
    summary = repair_package_residuals(crm, tmp_path)
    assert summary["status"] == "DONE"
    assert summary["updated"] == 7
    assert_owner(crm, 69)
    assert {key: value["STATUS_ID"] for key, value in crm.leads.items()} == statuses
    count = len(crm.writes)
    assert repair_package_residuals(crm, tmp_path)["residual_packages"] == 0
    assert len(crm.writes) == count


def test_unlinked_duplicate_event_cannot_return_package_to_rop(tmp_path):
    crm = CRM()
    results, failures = process_claim(crm, tmp_path, {"claim_id": "test", "items": [{"contact_id": 838, "version": 1}]})
    assert failures == 0
    assert results[0]["outcome"] == "ignored"
    assert crm.writes == []
    repair_package_residuals(crm, tmp_path)
    assert_owner(crm, 69)


def test_source_change_during_repair_converges_to_fresh_owner(tmp_path):
    crm = CRM()
    crm.change_source = True
    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 70)


def test_unverified_write_is_partial_and_next_pass_repairs_residuals(tmp_path):
    crm = CRM()
    crm.drop_lead_write = True
    assert repair_package_residuals(crm, tmp_path)["status"] == "PARTIAL"
    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 69)


def test_competing_linked_sources_need_event_authority(tmp_path):
    crm = CRM()
    crm.leads[2625]["CONTACT_ID"] = "838"
    packages, _ = build_packages(list(crm.companies.values()), list(crm.leads.values()), list(crm.contacts.values()), crm.requisites)
    resolved, skipped = resolve_authority_packages(packages, list(crm.contacts.values()), list(crm.leads.values()))
    assert resolved == []
    assert skipped[0]["type"] == "director_contacts_have_different_owners"
    remember_authority_sources(tmp_path, [{"status": "DONE", "fio": packages[0]["fio"], "contact_id": 837}])
    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 69)


def test_event_authority_survives_worker_restart(tmp_path):
    crm = CRM()
    result, failures = process_claim(crm, tmp_path, {"claim_id": "test", "items": [{"contact_id": 837, "version": 1}]})
    assert failures == 0
    assert result[0]["success"]
    sources = load_authority_sources(tmp_path)
    assert list(sources.values()) == [837]
    crm.leads[2624]["ASSIGNED_BY_ID"] = "72"
    assert repair_package_residuals(crm, tmp_path)["status"] == "DONE"
    assert_owner(crm, 69)
    assert json.loads((tmp_path / "recovery.json").read_text())["summary"]["updated"] == 1
