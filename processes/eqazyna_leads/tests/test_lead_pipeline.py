from __future__ import annotations

from dataclasses import replace

import pytest

from eqazyna_bitrix.bitrix_client import BitrixError
from eqazyna_bitrix.distribution import AssignmentUser, DistributionSnapshot
from eqazyna_bitrix.lead_pipeline import LeadPipeline, LeadPipelineConfig
from eqazyna_bitrix.models import Application, CompanyEnrichment


FIELD = "UF_CRM_1785917145255"
VALUE = "ГПО Недропользователя"
FAILURE_FIELD = "UF_CRM_1785508658316"
EURASIA_REASON = "Уже работает с Евразией"
EURASIA_REASON_ID = "903"
TEST_MANAGER_IDS = (22, 23, 16, 17, 18, 38, 44, 39, 40, 19, 15)


def application(doc_number: str = "APP-1") -> Application:
    return Application(
        created_at_raw="01.08.2026 09:00:00",
        doc_number=doc_number,
        bin="123456789012",
        applicant_name="Товарищество с ограниченной ответственностью Тест Недра",
        doc_type="Заявка на разведку ТПИ",
        status="Принято",
        source_url="https://minerals.e-qazyna.kz/example",
    )


def enrichment() -> CompanyEnrichment:
    return CompanyEnrichment(
        bin="123456789012",
        name="Товарищество с ограниченной ответственностью Тест Недра",
        phone="+7 700 111 22 33",
        director="Иванов Иван Иванович",
        oked="07100",
        activity="Добыча железных руд",
        legal_address="г. Алматы, ул. Тестовая, 1",
        city="Алматы",
        region="г. Алматы",
    )


class FakeClient:
    def __init__(
        self,
        *,
        lead=None,
        contact_lead=None,
        application_lead=None,
        company=None,
        requisite=None,
        contact=None,
        global_contact=None,
        company_director=None,
        address=None,
        field_type="string",
        field_items=None,
        requisite_presets=None,
        discovered_preset=1,
        requisite_create_error=None,
        manager_loads=None,
        lead_statuses=None,
        users=None,
        departments=None,
    ):
        self.lead = lead
        self.contact_lead = contact_lead
        self.application_lead = application_lead
        self.company = company
        self.requisite = requisite
        self.contact = contact
        self.global_contact = global_contact
        self.company_director = company_director
        self.address = address
        self.field_type = field_type
        self.field_items = field_items
        self.requisite_presets = requisite_presets
        self.discovered_preset = discovered_preset
        self.requisite_create_error = requisite_create_error
        self.manager_loads = dict(manager_loads or {})
        self.lead_statuses = lead_statuses or [
            {"STATUS_ID": "NEW", "SEMANTICS": ""},
            {"STATUS_ID": "IN_PROCESS", "SEMANTICS": ""},
            {"STATUS_ID": "CONVERTED", "SEMANTICS": "S"},
            {"STATUS_ID": "JUNK", "SEMANTICS": "F"},
        ]
        self.users = {
            int(key): value for key, value in (users or {}).items()
        }
        self.departments = {
            int(key): value for key, value in (departments or {}).items()
        }

        self.created_lead_fields = None
        self.created_lead_fields_list = []
        self.updated_lead_fields = None
        self.created_company_fields = None
        self.updated_company_fields = None
        self.created_contact_fields = None
        self.updated_contact_fields = None
        self.contact_company_links = []
        self.created_requisite_fields = None
        self.updated_requisite_fields = None
        self.created_address_fields = None
        self.updated_address_fields = None
        self.timeline = []

    def get_lead_fields(self):
        meta = {"type": self.field_type, "title": "Тип лидогенерации"}
        if self.field_items is not None:
            meta["items"] = self.field_items
        return {
            FIELD: meta,
            FAILURE_FIELD: {
                "type": "enumeration",
                "title": "Причина неудачи",
                "items": [
                    {"ID": "901", "VALUE": "Клиент отказался"},
                    {"ID": "902", "VALUE": "Не дозвонились"},
                    {"ID": EURASIA_REASON_ID, "VALUE": EURASIA_REASON},
                ],
            },
        }

    def get_requisite_fields(self):
        return {
            "RQ_BIN": {},
            "RQ_INN": {},
            "RQ_COMPANY_NAME": {},
            "RQ_COMPANY_FULL_NAME": {},
            "RQ_DIRECTOR": {},
            "RQ_OKED": {},
        }

    def list_lead_statuses(self):
        return self.lead_statuses

    def count_open_leads_for_manager(self, manager_id, terminal_status_ids=None):
        return int(self.manager_loads.get(int(manager_id), 0))

    def get_user(self, user_id):
        return self.users.get(int(user_id))

    def get_department(self, department_id):
        return self.departments.get(int(department_id))

    def discover_company_requisite_preset_id(self):
        return self.discovered_preset

    def list_requisite_presets(self):
        if self.requisite_presets is not None:
            return self.requisite_presets
        return [{"ID": "3", "ENTITY_TYPE_ID": "8", "NAME": "Юр. лицо", "XML_ID": "#CRM_REQUISITE_PRESET_DEF_KZ_LEGALENTITY#", "ACTIVE": "Y"}]

    def find_lead_by_application(
        self, doc_number, bin_number, originator_id="EQAZYNA_LEAD", extra_select=None
    ):
        assert doc_number.startswith("APP-")
        assert originator_id == "EQAZYNA_LEAD"
        assert FIELD in (extra_select or [])
        return self.application_lead

    def find_latest_lead_for_company(self, company_id, extra_select=None):
        assert str(company_id) == "601"
        assert FIELD in (extra_select or [])
        assert FAILURE_FIELD in (extra_select or [])
        return self.lead

    def find_latest_lead_for_contact(self, contact_id, extra_select=None):
        assert str(contact_id) == "801"
        assert FIELD in (extra_select or [])
        assert FAILURE_FIELD in (extra_select or [])
        return self.contact_lead

    def find_latest_lead_by_bin(self, bin_number, extra_select=None):
        assert FIELD in (extra_select or [])
        assert FAILURE_FIELD in (extra_select or [])
        return self.lead

    def create_lead(self, fields):
        self.created_lead_fields = fields
        self.created_lead_fields_list.append(fields)
        return str(500 + len(self.created_lead_fields_list))

    def update_lead(self, lead_id, fields):
        assert lead_id == "77"
        self.updated_lead_fields = fields

    def find_company_by_origin(self, origin_id, originator_id="EQAZYNA"):
        assert originator_id == "EQAZYNA"
        return self.company

    def find_company_by_bin(self, bin_number, bin_field="RQ_BIN"):
        assert bin_field in {"RQ_BIN", "RQ_INN"}
        return self.company

    def get_company(self, company_id):
        return self.company

    def create_company(self, fields):
        self.created_company_fields = fields
        self.company = {"ID": "601", **fields}
        return "601"

    def update_company(self, company_id, fields):
        self.updated_company_fields = fields

    def find_company_requisite(self, company_id, bin_number, bin_field="RQ_BIN"):
        assert bin_number == "123456789012"
        return self.requisite

    def create_requisite(self, fields):
        if self.requisite_create_error:
            raise BitrixError(self.requisite_create_error)
        self.created_requisite_fields = fields
        self.requisite = {"ID": "701", **fields}
        return "701"

    def update_requisite(self, requisite_id, fields):
        self.updated_requisite_fields = fields

    def find_requisite_address(self, requisite_id, address_type_id=1):
        return self.address

    def create_address(self, fields):
        self.created_address_fields = fields

    def update_address(self, fields):
        self.updated_address_fields = fields

    def find_company_director_contact(self, company_id):
        return self.company_director

    def find_director_contact(self, company_id, last_name, name, second_name=""):
        return self.contact

    def find_director_contact_global(self, last_name, name, second_name=""):
        return self.global_contact if self.global_contact is not None else self.contact

    def create_contact(self, fields):
        self.created_contact_fields = fields
        self.contact = {"ID": "801", **fields}
        return "801"

    def update_contact(self, contact_id, fields):
        self.updated_contact_fields = fields

    def ensure_contact_company_link(self, contact_id, company_id):
        link = (str(contact_id), str(company_id))
        if link in self.contact_company_links:
            return False
        self.contact_company_links.append(link)
        return True

    def add_timeline_comment(self, entity_type, entity_id, comment):
        self.timeline.append((entity_type, entity_id, comment))
        return "900"


def pipeline(client: FakeClient, **config_kwargs) -> LeadPipeline:
    config_kwargs.setdefault("random_seed", 7)
    config_kwargs.setdefault("manager_ids", TEST_MANAGER_IDS)
    result = LeadPipeline(client, LeadPipelineConfig(**config_kwargs))
    result.validate()
    return result


def test_create_complete_crm_bundle_and_use_compact_title():
    client = FakeClient()
    result = pipeline(client, assigned_by_id="36").process(application(), enrichment())

    assert result.action == "created_lead"
    assert result.lead_id == "501"
    assert result.company_id == "601"
    assert result.contact_id == "801"
    assert result.requisite_id == "701"

    assert client.created_company_fields["TITLE"] == "ТОО Тест Недра"
    assert client.created_requisite_fields["RQ_BIN"] == "123456789012"
    assert client.created_requisite_fields["RQ_DIRECTOR"] == "Иванов Иван Иванович"
    assert client.created_contact_fields["LAST_NAME"] == "Иванов"
    assert client.created_contact_fields["NAME"] == "Иван"
    assert client.created_contact_fields["SECOND_NAME"] == "Иванович"
    assert client.created_contact_fields["COMPANY_ID"] == 601

    fields = client.created_lead_fields
    assert fields[FIELD] == VALUE
    assert fields["TITLE"] == "ТОО Тест Недра. e-Qazyna № APP-1"
    assert fields["STATUS_ID"] == "NEW"
    assert fields["ORIGIN_ID"] == "APP-1"
    assert fields["COMPANY_ID"] == 601
    assert fields["CONTACT_ID"] == 801
    assert fields["ASSIGNED_BY_ID"] == 4
    assert client.created_company_fields["ASSIGNED_BY_ID"] == 4
    assert client.created_contact_fields["ASSIGNED_BY_ID"] == 4
    assert result.assignment_reason == "technical_user_for_bitrix_distribution"
    assert "—" not in fields["TITLE"]
    assert len(client.timeline) == 1


def test_existing_migrated_company_is_found_via_rq_inn_before_creation():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGIN_ID": "",
        "ORIGINATOR_ID": "",
        "ASSIGNED_BY_ID": "22",
    }
    client = FakeClient(company=company, contact=None)
    looked_up_fields = []

    client.find_company_by_origin = lambda *args, **kwargs: None

    def find_company_by_bin(bin_number, bin_field="RQ_BIN"):
        looked_up_fields.append(bin_field)
        return company if bin_field == "RQ_INN" else None

    client.find_company_by_bin = find_company_by_bin

    result = pipeline(client).process(application(), enrichment())

    assert result.company_id == "601"
    assert client.created_company_fields is None
    assert looked_up_fields == ["RQ_BIN", "RQ_INN"]


def test_global_director_contact_is_reused_without_moving_primary_company():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
        "ASSIGNED_BY_ID": "22",
    }
    global_contact = {
        "ID": "801",
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "POST": "Руководитель",
        "COMPANY_ID": "555",
        "ASSIGNED_BY_ID": "22",
        "OPENED": "Y",
        "COMMENTS": "[[EQAZYNA_DIRECTOR:999999999999]]",
    }
    client = FakeClient(company=company, contact=None, global_contact=global_contact)

    result = pipeline(client).process(application(), enrichment())

    assert result.contact_id == "801"
    assert client.created_contact_fields is None
    assert ("801", "601") in client.contact_company_links
    assert not client.updated_contact_fields or client.updated_contact_fields.get("COMPANY_ID") != 601


def test_existing_migrated_company_requisite_and_contact_are_reused():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "COMMENTS": "[[EQAZYNA_MASTER_DATA:123456789012]]",
        "PHONE": [{"VALUE": "+7 700 111 22 33", "VALUE_TYPE": "WORK"}],
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
        "OPENED": "Y",
        "ADDRESS": "г. Алматы, ул. Тестовая, 1",
        "ADDRESS_CITY": "Алматы",
        "ADDRESS_REGION": "г. Алматы",
        "ADDRESS_PROVINCE": "г. Алматы",
        "ADDRESS_COUNTRY": "Казахстан",
    }
    requisite = {
        "ID": "701",
        "ENTITY_TYPE_ID": "4",
        "ENTITY_ID": "601",
        "PRESET_ID": "1",
        "NAME": "БИН 123456789012. Товарищество с ограниченной ответственностью Тест Недра",
        "ACTIVE": "Y",
        "ADDRESS_ONLY": "N",
        "SORT": "500",
        "XML_ID": "EQAZYNA-REQ-123456789012",
        "ORIGINATOR_ID": "EQAZYNA",
        "RQ_BIN": "123456789012",
        "RQ_COMPANY_NAME": "ТОО Тест Недра",
        "RQ_COMPANY_FULL_NAME": "Товарищество с ограниченной ответственностью Тест Недра",
        "RQ_DIRECTOR": "Иванов Иван Иванович",
        "RQ_OKED": "07100",
    }
    contact = {
        "ID": "801",
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "POST": "Руководитель",
        "COMPANY_ID": "601",
        "OPENED": "Y",
        "COMMENTS": "[[EQAZYNA_DIRECTOR:123456789012]]",
    }
    client = FakeClient(company=company, requisite=requisite, contact=contact, address={"TYPE_ID": "1"})

    result = pipeline(client).process(application(), enrichment())

    assert result.company_id == "601"
    assert result.requisite_id == "701"
    assert result.contact_id == "801"
    assert client.created_company_fields is None
    assert client.created_requisite_fields is None
    assert client.created_contact_fields is None


def test_failed_previous_company_lead_does_not_inherit_non_eurasia_failure():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ASSIGNED_BY_ID": "15",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "TITLE": "ТОО Тест Недра. e-Qazyna № APP-1",
        "STATUS_ID": "JUNK",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: "Клиент отказался",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
        "ASSIGNED_BY_ID": "999",
        "COMPANY_ID": "601",
        "COMMENTS": "Ключ заявки: eQazyna|APP-1|123456789012",
        "ORIGINATOR_ID": "EQAZYNA_LEAD",
        "ORIGIN_ID": "APP-1",
        FIELD: VALUE,
    }
    loads = {manager_id: 5 for manager_id in TEST_MANAGER_IDS}
    loads[44] = 0
    client = FakeClient(company=company, lead=previous, manager_loads=loads)

    result = pipeline(client, assigned_by_id="36").process(application("APP-2"), enrichment())

    assert result.action == "created_lead"
    assert result.assigned_by_id == 4
    assert result.assignment_reason == "technical_user_for_bitrix_distribution"
    assert result.status_id == "NEW"
    assert result.status_reason == "default_new"
    assert result.failure_reason is None
    assert result.status_reference_lead_id == "77"
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == 4
    assert client.created_lead_fields["STATUS_ID"] == "NEW"
    assert FAILURE_FIELD not in client.created_lead_fields
    assert client.created_lead_fields["ORIGIN_ID"] == "APP-2"
    assert client.created_lead_fields["TITLE"].endswith("e-Qazyna № APP-2")


def test_previous_lead_owner_is_ignored_without_director_contact_owner():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "IN_PROCESS",
        "ASSIGNED_BY_ID": "16",
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    loads = {manager_id: 5 for manager_id in TEST_MANAGER_IDS}
    loads[38] = 0
    client = FakeClient(company=company, lead=previous, manager_loads=loads)

    result = pipeline(client, assigned_by_id="36").process(application("APP-2"), enrichment())

    assert result.action == "created_lead"
    assert result.assigned_by_id == 4
    assert result.assignment_reason == "technical_user_for_bitrix_distribution"
    assert result.status_id == "NEW"

def test_existing_application_number_is_skipped_without_any_writes():
    existing = {
        "ID": "77",
        "TITLE": "ТОО Тест Недра. e-Qazyna № APP-2",
        "STATUS_ID": "IN_PROCESS",
        "ASSIGNED_BY_ID": "116",
        "COMPANY_ID": "601",
        "CONTACT_ID": "801",
        "ORIGINATOR_ID": "EQAZYNA_LEAD",
        "ORIGIN_ID": "APP-2",
    }
    client = FakeClient(application_lead=existing)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.action == "skipped_existing_application"
    assert result.lead_id == "77"
    assert result.assigned_by_id == 116
    assert client.created_lead_fields is None
    assert client.created_company_fields is None
    assert client.created_contact_fields is None
    assert client.created_requisite_fields is None


def test_two_different_application_numbers_for_same_bin_create_two_leads():
    client = FakeClient()
    parser = pipeline(client)

    first = parser.process(application("APP-1"), enrichment())
    second = parser.process(application("APP-2"), enrichment())

    assert first.action == "created_lead"
    assert second.action == "created_lead"
    assert len(client.created_lead_fields_list) == 2
    assert [row["ORIGIN_ID"] for row in client.created_lead_fields_list] == ["APP-1", "APP-2"]
    assert [row["TITLE"] for row in client.created_lead_fields_list] == [
        "ТОО Тест Недра. e-Qazyna № APP-1",
        "ТОО Тест Недра. e-Qazyna № APP-2",
    ]


def test_same_application_repeated_in_one_run_is_skipped():
    client = FakeClient()
    parser = pipeline(client)

    first = parser.process(application("APP-1"), enrichment())
    second = parser.process(application("APP-1"), enrichment())

    assert first.action == "created_lead"
    assert second.action == "skipped_duplicate_application_in_run"
    assert len(client.created_lead_fields_list) == 1


def test_no_dummy_contact_is_created_without_full_director_name():
    client = FakeClient()
    enr = enrichment()
    enr.director = "Не найден"

    result = pipeline(client).process(application(), enr)

    assert result.action == "created_lead"
    assert result.contact_id is None
    assert result.contact_action == "contact_skipped_no_valid_fio"
    assert client.created_contact_fields is None
    assert "CONTACT_ID" not in client.created_lead_fields


def test_validate_rejects_missing_custom_field_but_does_not_block_on_requisite_metadata():
    missing = FakeClient()
    missing.get_lead_fields = lambda: {}
    with pytest.raises(BitrixError, match=FIELD):
        LeadPipeline(missing, LeadPipelineConfig()).validate()

    missing_bin = FakeClient()
    missing_bin.get_requisite_fields = lambda: {"RQ_DIRECTOR": {}}
    result = LeadPipeline(missing_bin, LeadPipelineConfig())
    result.validate()
    assert result.requisite_preset_id == 1
    assert any("RQ_BIN" in warning for warning in result.validation_warnings)


def test_enumeration_field_resolves_label_to_internal_id():
    client = FakeClient(
        field_type="enumeration",
        field_items=[
            {"ID": "41", "VALUE": "Другое"},
            {"ID": "42", "VALUE": "ГПО недропользователя"},
        ],
    )

    result = pipeline(client).process(application(), enrichment())

    assert result.action == "created_lead"
    assert client.created_lead_fields[FIELD] == "42"



def test_stale_configured_preset_is_replaced_by_existing_company_requisite_preset():
    client = FakeClient(discovered_preset=1)

    result = pipeline(client, requisite_preset_id="3")

    assert result.requisite_preset_id == 1
    assert result.validation_warnings == [
        "BITRIX_REQUISITE_PRESET_ID=3 не совпадает с реально используемым "
        "в коробке PRESET_ID=1; выбран PRESET_ID=1."
    ]


def test_configured_preset_is_used_when_existing_requisites_cannot_resolve_it():
    client = FakeClient(discovered_preset=None)

    result = pipeline(client, requisite_preset_id="1")

    assert result.requisite_preset_id == 1


def test_requisite_failure_is_warning_and_does_not_cancel_lead_company_or_contact():
    client = FakeClient(requisite_create_error="preset rejected")

    result = pipeline(client).process(application(), enrichment())

    assert result.action == "created_lead"
    assert result.lead_id == "501"
    assert result.company_id == "601"
    assert result.contact_id == "801"
    assert result.requisite_id is None
    assert result.requisite_action == "requisite_error"
    assert "Реквизит компании не сохранён" in (result.warning or "")


def test_dry_run_reads_but_never_writes():
    client = FakeClient()
    result = pipeline(client, dry_run=True).process(application(), enrichment())

    assert result.action == "dry_run_create_lead"
    assert result.company_action == "dry_run_create_company"
    assert result.requisite_action == "dry_run_create_requisite"
    assert result.contact_action == "dry_run_create_contact"
    assert client.created_lead_fields is None
    assert client.created_company_fields is None
    assert client.created_contact_fields is None
    assert client.created_requisite_fields is None
    assert client.created_address_fields is None
    assert client.timeline == []




def test_director_contact_owner_is_only_assignment_source_when_owners_conflict():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ASSIGNED_BY_ID": "15",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    contact = {
        "ID": "801",
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "COMPANY_ID": "601",
        "ASSIGNED_BY_ID": "17",
    }
    contact_lead = {
        "ID": "90",
        "STATUS_ID": "IN_PROCESS",
        "ASSIGNED_BY_ID": "18",
        "CONTACT_ID": "801",
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T12:00:00+05:00",
    }
    company_lead = {
        "ID": "89",
        "STATUS_ID": "IN_PROCESS",
        "ASSIGNED_BY_ID": "16",
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T13:00:00+05:00",
    }
    client = FakeClient(
        company=company,
        contact=contact,
        contact_lead=contact_lead,
        lead=company_lead,
    )

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.action == "created_lead"
    assert result.assigned_by_id == 17
    assert result.assignment_reason == "director_contact_owner"
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == 17


def test_latest_related_non_eurasia_failure_still_creates_new_stage():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    contact = {
        "ID": "801",
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "COMPANY_ID": "601",
        "ASSIGNED_BY_ID": "17",
    }
    contact_lead = {
        "ID": "90",
        "STATUS_ID": "IN_PROCESS",
        "ASSIGNED_BY_ID": "17",
        "CONTACT_ID": "801",
        "DATE_MODIFY": "2026-08-05T12:00:00+05:00",
    }
    company_lead = {
        "ID": "91",
        "STATUS_ID": "JUNK",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: "Не дозвонились",
        "ASSIGNED_BY_ID": "16",
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T14:00:00+05:00",
    }
    client = FakeClient(
        company=company,
        contact=contact,
        contact_lead=contact_lead,
        lead=company_lead,
    )

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.assigned_by_id == 17
    assert result.assignment_reason == "director_contact_owner"
    assert result.status_id == "NEW"
    assert result.status_reason == "default_new"
    assert result.failure_reason is None
    assert result.status_reference_lead_id == "91"
    assert FAILURE_FIELD not in client.created_lead_fields


def test_newer_active_lead_means_new_stage_even_when_older_lead_failed():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    contact = {
        "ID": "801",
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "COMPANY_ID": "601",
        "ASSIGNED_BY_ID": "17",
    }
    contact_lead = {
        "ID": "92",
        "STATUS_ID": "IN_PROCESS",
        "ASSIGNED_BY_ID": "17",
        "CONTACT_ID": "801",
        "DATE_MODIFY": "2026-08-05T15:00:00+05:00",
    }
    company_lead = {
        "ID": "91",
        "STATUS_ID": "JUNK",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: "Клиент отказался",
        "ASSIGNED_BY_ID": "16",
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T14:00:00+05:00",
    }
    client = FakeClient(
        company=company,
        contact=contact,
        contact_lead=contact_lead,
        lead=company_lead,
    )

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.status_id == "NEW"
    assert result.status_reason == "default_new"
    assert result.failure_reason is None
    assert FAILURE_FIELD not in client.created_lead_fields


def test_unbound_bundle_goes_to_technical_user_regardless_of_manager_workload():
    loads = {manager_id: 8 for manager_id in TEST_MANAGER_IDS}
    loads[16] = 2
    loads[17] = 2
    client = FakeClient(manager_loads=loads)

    result = pipeline(client, random_seed=3).process(application(), enrichment())

    assert result.action == "created_lead"
    assert result.assignment_reason == "technical_user_for_bitrix_distribution"
    assert result.assigned_by_id == 4
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == result.assigned_by_id
    assert client.created_company_fields["ASSIGNED_BY_ID"] == result.assigned_by_id
    assert client.created_contact_fields["ASSIGNED_BY_ID"] == result.assigned_by_id


def test_director_contact_owner_outside_old_manager_pool_is_retained():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ASSIGNED_BY_ID": "15",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    contact = {
        "ID": "801",
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "COMPANY_ID": "601",
        "ASSIGNED_BY_ID": "999",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "IN_PROCESS",
        "ASSIGNED_BY_ID": "16",
        "CONTACT_ID": "801",
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    loads = {manager_id: 5 for manager_id in TEST_MANAGER_IDS}
    loads[44] = 0
    client = FakeClient(
        company=company,
        contact=contact,
        contact_lead=previous,
        lead=previous,
        manager_loads=loads,
    )

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.assigned_by_id == 999
    assert result.assignment_reason == "director_contact_owner"
    assert client.updated_company_fields["ASSIGNED_BY_ID"] == 999
    assert "ASSIGNED_BY_ID" not in client.updated_contact_fields


def test_dry_run_reuses_planned_contact_owner_for_same_director_and_bin():
    client = FakeClient()
    parser = pipeline(client, dry_run=True, random_seed=3)

    first = parser.process(application("APP-1"), enrichment())
    second = parser.process(application("APP-2"), enrichment())

    assert first.action == "dry_run_create_lead"
    assert second.action == "dry_run_create_lead"
    assert first.assigned_by_id == second.assigned_by_id
    assert first.assignment_reason == "technical_user_for_bitrix_distribution"
    assert second.assignment_reason == "director_contact_owner"
    assert first.company_action == "dry_run_create_company"
    assert second.company_action == "dry_run_reuse_planned_company"
    assert first.contact_action == "dry_run_create_contact"
    assert second.contact_action == "dry_run_reuse_planned_contact"
    assert first.requisite_action == "dry_run_create_requisite"
    assert second.requisite_action == "dry_run_reuse_planned_requisite"


def test_non_eurasia_enum_failure_reason_still_creates_new_stage():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "JUNK",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: "901",
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    client = FakeClient(company=company, lead=previous)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.status_id == "NEW"
    assert result.failure_reason is None
    assert FAILURE_FIELD not in client.created_lead_fields


def test_eurasia_failure_reason_label_keeps_failed_stage():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "JUNK",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: EURASIA_REASON,
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    client = FakeClient(company=company, lead=previous)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.status_id == "JUNK"
    assert result.status_reason == "existing_eurasia_client_exception"
    assert result.failure_reason == EURASIA_REASON_ID
    assert client.created_lead_fields[FAILURE_FIELD] == EURASIA_REASON_ID


def test_eurasia_failure_reason_enum_id_keeps_failed_stage():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "JUNK",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: EURASIA_REASON_ID,
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    client = FakeClient(company=company, lead=previous)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.status_id == "JUNK"
    assert result.status_reason == "existing_eurasia_client_exception"
    assert result.failure_reason == EURASIA_REASON_ID
    assert client.created_lead_fields[FAILURE_FIELD] == EURASIA_REASON_ID


def test_configured_default_stage_cannot_move_new_application_out_of_new():
    client = FakeClient()

    result = pipeline(client, lead_status_id="JUNK").process(application("APP-2"), enrichment())

    assert result.status_id == "NEW"
    assert result.status_reason == "default_new"
    assert client.created_lead_fields["STATUS_ID"] == "NEW"
    assert FAILURE_FIELD not in client.created_lead_fields


def test_eurasia_exception_uses_canonical_junk_not_arbitrary_old_failed_stage():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "UC_OLD_FAIL",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: EURASIA_REASON_ID,
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    client = FakeClient(company=company, lead=previous)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.status_id == "JUNK"
    assert client.created_lead_fields["STATUS_ID"] == "JUNK"
    assert client.created_lead_fields[FAILURE_FIELD] == EURASIA_REASON_ID


def test_active_lead_with_stale_eurasia_reason_does_not_trigger_failure():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "IN_PROCESS",
        "STATUS_SEMANTIC_ID": "",
        FAILURE_FIELD: EURASIA_REASON_ID,
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    client = FakeClient(company=company, lead=previous)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.status_id == "NEW"
    assert result.failure_reason is None
    assert FAILURE_FIELD not in client.created_lead_fields


def test_eurasia_reason_returned_as_single_item_list_is_recognized():
    company = {
        "ID": "601",
        "TITLE": "ТОО Тест Недра",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    previous = {
        "ID": "77",
        "STATUS_ID": "JUNK",
        "STATUS_SEMANTIC_ID": "F",
        FAILURE_FIELD: [EURASIA_REASON_ID],
        "COMPANY_ID": "601",
        "DATE_MODIFY": "2026-08-05T10:00:00+05:00",
    }
    client = FakeClient(company=company, lead=previous)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.status_id == "JUNK"
    assert result.failure_reason == EURASIA_REASON_ID
    assert client.created_lead_fields[FAILURE_FIELD] == EURASIA_REASON_ID


def test_same_director_on_another_company_has_priority_for_new_bundle_assignment():
    company = {
        "ID": "601",
        "TITLE": "ТОО Новая компания",
        "ORIGINATOR_ID": "EQAZYNA",
        "ORIGIN_ID": "123456789012",
    }
    global_contact = {
        "ID": "700",
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "POST": "Руководитель",
        "COMPANY_ID": "555",
        "ASSIGNED_BY_ID": "17",
    }
    client = FakeClient(company=company, contact=None, global_contact=global_contact)

    result = pipeline(client).process(application("APP-2"), enrichment())

    assert result.assigned_by_id == 17
    assert result.assignment_reason == "director_contact_owner"
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == 17
    assert result.contact_id == "700"
    assert client.created_contact_fields is None
    assert ("700", "601") in client.contact_company_links
    assert client.updated_company_fields["ASSIGNED_BY_ID"] == 17


def sheet_distribution(users, assignments=None):
    return DistributionSnapshot(tuple(users), dict(assignments or {}))



@pytest.mark.parametrize("director", [None, "", "Не найден", "Директор", "Иванов"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_director_routes_existing_company_bundle_to_user_4(director, dry_run):
    client = FakeClient(company={
        "ID": "601",
        "ASSIGNED_BY_ID": "77",
        "ORIGIN_ID": "123456789012",
        "ORIGINATOR_ID": "EQAZYNA",
    })
    subject = pipeline(client, dry_run=dry_run, assigned_by_id="99")

    result = subject.process(application(), replace(enrichment(), director=director))

    assert result.assigned_by_id == 4
    assert result.assignment_reason == "technical_user_for_bitrix_distribution"
    assert result.contact_id is None
    assert client.created_contact_fields is None
    if dry_run:
        assert result.action == "dry_run_create_lead"
        assert result.company_action == "dry_run_update_company"
        assert client.updated_company_fields is None
        assert client.created_lead_fields is None
    else:
        assert result.action == "created_lead"
        assert client.updated_company_fields["ASSIGNED_BY_ID"] == 4
        assert client.created_lead_fields["ASSIGNED_BY_ID"] == 4


@pytest.mark.parametrize("owner", [None, "", "0", 0, "-1", "invalid"])
def test_existing_director_without_valid_owner_routes_whole_bundle_to_4(owner):
    client = FakeClient(
        company={"ID": "601", "ASSIGNED_BY_ID": "77"},
        contact={"ID": "801", "COMPANY_ID": "601", "ASSIGNED_BY_ID": owner},
    )

    result = pipeline(client).process(application(), enrichment())

    assert result.action == "created_lead"
    assert result.assigned_by_id == 4
    assert client.updated_company_fields["ASSIGNED_BY_ID"] == 4
    assert client.updated_contact_fields["ASSIGNED_BY_ID"] == 4
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == 4


@pytest.mark.parametrize("owner", [4, 13, 999])
@pytest.mark.parametrize("with_company", [False, True])
def test_founder_owner_wins_over_manual_sheet_fix_and_old_manager_pool(owner, with_company):
    users = (AssignmentUser(11, "Old manager", 46, "Астана"),)
    client = FakeClient(
        company={"ID": "601", "ASSIGNED_BY_ID": "77"} if with_company else None,
        global_contact={
            "ID": "801", "COMPANY_ID": "555", "ASSIGNED_BY_ID": str(owner),
            "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
        },
    )
    subject = pipeline(
        client,
        distribution=sheet_distribution(users, {"123456789012": 88}),
        assigned_by_id="36",
    )

    result = subject.process(application(), replace(enrichment(), city="Астана"))

    assert result.action == "created_lead"
    assert result.assigned_by_id == owner
    assert result.assignment_reason == "director_contact_owner"
    assert result.contact_id == "801"
    assert client.created_contact_fields is None
    company_fields = client.updated_company_fields if with_company else client.created_company_fields
    assert company_fields["ASSIGNED_BY_ID"] == owner
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == owner
    assert "ASSIGNED_BY_ID" not in (client.updated_contact_fields or {})


def test_unbound_founder_does_not_use_manual_sheet_fix_or_query_old_distribution():
    client = FakeClient()

    def forbidden(*args, **kwargs):
        raise AssertionError("Old distribution must not be queried")

    client.get_user = forbidden
    client.get_department = forbidden
    client.count_open_leads_for_manager = forbidden
    subject = pipeline(
        client,
        distribution=sheet_distribution([], {"123456789012": 88}),
        assigned_by_id="99",
    )

    result = subject.process(application(), enrichment())

    assert result.assigned_by_id == 4
    assert client.created_company_fields["ASSIGNED_BY_ID"] == 4
    assert client.created_contact_fields["ASSIGNED_BY_ID"] == 4
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == 4


def test_company_specific_founder_owner_wins_over_same_name_global_contact():
    client = FakeClient(
        company={"ID": "601", "ASSIGNED_BY_ID": "77"},
        contact={"ID": "801", "COMPANY_ID": "601", "ASSIGNED_BY_ID": "13"},
        global_contact={"ID": "802", "COMPANY_ID": "555", "ASSIGNED_BY_ID": "88"},
    )

    result = pipeline(client).process(application(), enrichment())

    assert result.assigned_by_id == 13
    assert result.contact_id == "801"
    assert client.updated_company_fields["ASSIGNED_BY_ID"] == 13
    assert "ASSIGNED_BY_ID" not in client.updated_contact_fields


@pytest.mark.parametrize("dry_run", [False, True])
def test_same_founder_for_two_new_bins_keeps_existing_manager(dry_run):
    client = FakeClient(global_contact={
        "ID": "801", "COMPANY_ID": "555", "ASSIGNED_BY_ID": "13",
        "LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": "Иванович",
    })
    subject = pipeline(client, dry_run=dry_run)
    first = subject.process(application("APP-1"), enrichment())
    # A different company must be looked up independently by BIN.
    client.company = None
    second_app = replace(application("APP-2"), bin="123456789013")
    second = subject.process(second_app, replace(enrichment(), bin=second_app.bin))

    assert [first.assigned_by_id, second.assigned_by_id] == [13, 13]
    assert [first.contact_id, second.contact_id] == ["801", "801"]
    assert client.created_contact_fields is None


def test_next_new_lead_reads_founder_owner_after_bitrix_robot_handoff():
    client = FakeClient(
        company={"ID": "601", "ASSIGNED_BY_ID": "4"},
        contact={"ID": "801", "COMPANY_ID": "601", "ASSIGNED_BY_ID": "4"},
    )
    subject = pipeline(client)
    first = subject.process(application("APP-1"), enrichment())
    # Simulate the native Bitrix24 distribution and package synchronization.
    client.contact = {**client.contact, "ASSIGNED_BY_ID": "13"}
    second = subject.process(application("APP-2"), enrichment())

    assert [first.assigned_by_id, second.assigned_by_id] == [4, 13]
    assert [fields["ASSIGNED_BY_ID"] for fields in client.created_lead_fields_list] == [4, 13]
    assert client.updated_company_fields["ASSIGNED_BY_ID"] == 13
    assert "ASSIGNED_BY_ID" not in client.updated_contact_fields


@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_egov_director_preserves_existing_company_founder_binding(dry_run):
    client = FakeClient(
        company={"ID": "601", "ASSIGNED_BY_ID": "77"},
        company_director={
            "ID": "801", "POST": "Учредитель", "COMPANY_ID": "601",
            "LAST_NAME": "Иванов", "NAME": "Иван", "ASSIGNED_BY_ID": "13",
        },
    )

    result = pipeline(client, dry_run=dry_run).process(
        application(), replace(enrichment(), director=None),
    )

    assert result.assigned_by_id == 13
    assert result.assignment_reason == "director_contact_owner"
    assert result.contact_id == "801"
    assert client.created_contact_fields is None
    assert client.updated_contact_fields is None
    if not dry_run:
        assert client.updated_company_fields["ASSIGNED_BY_ID"] == 13
        assert client.created_lead_fields["ASSIGNED_BY_ID"] == 13
        assert client.created_lead_fields["CONTACT_ID"] == 801


def test_missing_egov_name_and_existing_unassigned_founder_send_bundle_to_4():
    client = FakeClient(
        company={"ID": "601", "ASSIGNED_BY_ID": "77"},
        company_director={"ID": "801", "POST": "Учредитель", "COMPANY_ID": "601"},
    )

    result = pipeline(client).process(application(), replace(enrichment(), director=None))

    assert result.assigned_by_id == 4
    assert result.contact_id == "801"
    assert client.updated_company_fields["ASSIGNED_BY_ID"] == 4
    assert client.updated_contact_fields == {"ASSIGNED_BY_ID": 4}
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == 4


def test_real_global_founder_manager_wins_over_company_contact_on_technical_user():
    client = FakeClient(
        company={"ID": "601", "ASSIGNED_BY_ID": "4"},
        contact={"ID": "801", "COMPANY_ID": "601", "ASSIGNED_BY_ID": "4"},
        global_contact={"ID": "802", "COMPANY_ID": "555", "ASSIGNED_BY_ID": "13"},
    )

    result = pipeline(client).process(application(), enrichment())

    assert result.assigned_by_id == 13
    assert result.contact_id == "801"
    assert client.updated_company_fields["ASSIGNED_BY_ID"] == 13
    assert client.updated_contact_fields["ASSIGNED_BY_ID"] == 13


def test_dry_run_reuses_one_planned_founder_contact_across_new_companies():
    client = FakeClient()
    subject = pipeline(client, dry_run=True)
    first = subject.process(application("APP-1"), enrichment())
    other = replace(application("APP-2"), bin="123456789013")

    second = subject.process(other, replace(enrichment(), bin=other.bin))

    assert first.assigned_by_id == second.assigned_by_id == 4
    assert first.contact_id == second.contact_id
    assert second.contact_action == "dry_run_reuse_planned_contact"
