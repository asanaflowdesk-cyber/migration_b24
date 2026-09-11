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
        assert bin_field == "RQ_BIN"
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
    assert fields["ASSIGNED_BY_ID"] == 22
    assert client.created_company_fields["ASSIGNED_BY_ID"] == 22
    assert client.created_contact_fields["ASSIGNED_BY_ID"] == 22
    assert result.assignment_reason == "least_loaded_random"
    assert "—" not in fields["TITLE"]
    assert len(client.timeline) == 1


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
    assert result.assigned_by_id == 44
    assert result.assignment_reason == "least_loaded_random"
    assert result.status_id == "NEW"
    assert result.status_reason == "default_new"
    assert result.failure_reason is None
    assert result.status_reference_lead_id == "77"
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == 44
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
    assert result.assigned_by_id == 38
    assert result.assignment_reason == "least_loaded_random"
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


def test_distribution_is_random_only_between_least_loaded_managers_and_assigns_bundle():
    loads = {manager_id: 8 for manager_id in TEST_MANAGER_IDS}
    loads[16] = 2
    loads[17] = 2
    client = FakeClient(manager_loads=loads)

    result = pipeline(client, random_seed=3).process(application(), enrichment())

    assert result.action == "created_lead"
    assert result.assignment_reason == "least_loaded_random"
    assert result.assigned_by_id in {16, 17}
    assert client.created_lead_fields["ASSIGNED_BY_ID"] == result.assigned_by_id
    assert client.created_company_fields["ASSIGNED_BY_ID"] == result.assigned_by_id
    assert client.created_contact_fields["ASSIGNED_BY_ID"] == result.assigned_by_id


def test_director_contact_owner_outside_approved_pool_is_ignored():
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

    assert result.assigned_by_id == 44
    assert result.assignment_reason == "least_loaded_random"
    # Existing company owner is preserved by the parser. Company ownership is
    # reconciled separately from the earliest linked lead.
    assert "ASSIGNED_BY_ID" not in client.updated_company_fields
    assert client.updated_contact_fields["ASSIGNED_BY_ID"] == 44


def test_dry_run_reuses_planned_contact_owner_for_same_director_and_bin():
    client = FakeClient()
    parser = pipeline(client, dry_run=True, random_seed=3)

    first = parser.process(application("APP-1"), enrichment())
    second = parser.process(application("APP-2"), enrichment())

    assert first.action == "dry_run_create_lead"
    assert second.action == "dry_run_create_lead"
    assert first.assigned_by_id == second.assigned_by_id
    assert first.assignment_reason == "least_loaded_random"
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
    assert client.created_contact_fields["ASSIGNED_BY_ID"] == 17
    # Existing company ownership remains protected from parser-side rewrites.
    assert not client.updated_company_fields or "ASSIGNED_BY_ID" not in client.updated_company_fields


def sheet_distribution(users, assignments=None):
    return DistributionSnapshot(tuple(users), dict(assignments or {}))


def sheet_client(users, departments, **kwargs):
    bitrix_users = {
        user.user_id: {
            "ID": str(user.user_id),
            "ACTIVE": "Y",
            "UF_DEPARTMENT": [user.department_id],
        }
        for user in users
    }
    for department in departments.values():
        head = int(str(department.get("UF_HEAD") or 0))
        if head and head not in bitrix_users:
            bitrix_users[head] = {
                "ID": str(head),
                "ACTIVE": "Y",
                "UF_DEPARTMENT": [int(department["ID"])],
            }
    return FakeClient(users=bitrix_users, departments=departments, **kwargs)


def test_company_fix_has_absolute_priority_and_does_not_consume_capacity():
    users = [
        AssignmentUser(11, "Manager", 10, "Алматинский филиал"),
        AssignmentUser(12, "Second", 10, "Алматинский филиал"),
        AssignmentUser(99, "Head", 10, "Алматинский филиал", "РОП"),
    ]
    departments = {10: {"ID": "10", "NAME": "Алматинский филиал", "UF_HEAD": "99"}}
    client = sheet_client(users, departments)
    client.users[77] = {"ID": "77", "ACTIVE": "Y", "UF_DEPARTMENT": [50]}
    config = LeadPipelineConfig(
        dry_run=True,
        random_seed=1,
        distribution=sheet_distribution(users, {"123456789012": 77}),
    )
    subject = LeadPipeline(client, config)
    subject.validate()

    fixed_result = subject.process(application(), enrichment())
    same_founder_app = replace(application("APP-3"), bin="123456789014")
    same_founder_result = subject.process(
        same_founder_app,
        replace(enrichment(), bin=same_founder_app.bin),
    )
    other_app = replace(application("APP-2"), bin="123456789013")
    other_result = subject.process(
        other_app,
        replace(enrichment(), bin=other_app.bin, director="Петров Петр Петрович"),
    )

    assert fixed_result.assigned_by_id == 77
    assert fixed_result.assignment_reason == "company_fix"
    assert same_founder_result.assigned_by_id == 77
    assert same_founder_result.assignment_reason == "same_founder_in_run"
    assert other_result.assigned_by_id in {11, 12}
    assert other_result.assignment_reason == "non_astana_least_loaded_no_rop_limit"


def test_founder_binding_has_priority_over_astana_address():
    users = [
        AssignmentUser(11, "Almaty manager", 10, "Алматинский филиал"),
        AssignmentUser(19, "Almaty head", 10, "Алматинский филиал", "РОП"),
        AssignmentUser(21, "Astana manager", 46, "УП г. Астана"),
        AssignmentUser(22, "Astana manager 2", 46, "УП г. Астана"),
        AssignmentUser(29, "Astana head", 46, "УП г. Астана", "РОП"),
    ]
    departments = {
        10: {"ID": "10", "NAME": "Алматинский филиал", "UF_HEAD": "19"},
        46: {"ID": "46", "NAME": "УП г. Астана", "UF_HEAD": "29"},
    }
    contact = {"ID": "801", "ASSIGNED_BY_ID": "11"}
    client = sheet_client(users, departments, contact=contact)
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(
            dry_run=True,
            distribution=sheet_distribution(users),
        ),
    )
    subject.validate()

    result = subject.process(
        application(),
        replace(
            enrichment(),
            legal_address="г. Астана, район Алматы, ул. Тестовая, 1",
            city="Астана",
            region="г. Астана",
        ),
    )

    assert result.assigned_by_id == 11
    assert result.assignment_reason == "active_director_owner"


def test_astana_without_founder_binding_uses_only_department_46():
    users = [
        AssignmentUser(11, "Other manager", 10, "Другой филиал"),
        AssignmentUser(19, "Other head", 10, "Другой филиал", "РОП"),
        AssignmentUser(21, "Astana manager", 46, "УП г. Астана"),
        AssignmentUser(29, "Astana head", 46, "УП г. Астана", "РОП"),
    ]
    departments = {
        10: {"ID": "10", "NAME": "Другой филиал"},
        46: {"ID": "46", "NAME": "УП г. Астана"},
    }
    client = sheet_client(users, departments)
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(dry_run=True, distribution=sheet_distribution(users)),
    )
    subject.validate()

    result = subject.process(
        application(),
        replace(enrichment(), legal_address="г. Астана", city="Астана", region="г. Астана"),
    )

    assert result.assigned_by_id == 21
    assert result.assignment_reason == "astana_new_founder_least_loaded"


def test_owner_absent_from_user_list_goes_to_astana_head():
    users = [
        AssignmentUser(11, "Manager", 46, "УП г. Астана"),
        AssignmentUser(12, "Manager 2", 46, "УП г. Астана"),
        AssignmentUser(99, "Head", 46, "УП г. Астана", "РОП"),
    ]
    departments = {46: {"ID": "46", "NAME": "УП г. Астана", "UF_HEAD": "99"}}
    contact = {"ID": "801", "ASSIGNED_BY_ID": "77"}
    client = sheet_client(users, departments, contact=contact)
    client.users[77] = {"ID": "77", "ACTIVE": "N", "UF_DEPARTMENT": [46]}
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(dry_run=True, distribution=sheet_distribution(users)),
    )
    subject.validate()

    result = subject.process(application(), enrichment())

    assert result.assigned_by_id == 99
    assert result.assignment_reason == "missing_owner_to_department_head"


def test_single_user_in_branch_receives_all_new_founders_without_limit():
    users = [AssignmentUser(11, "Only manager", 10, "Алматинский филиал")]
    departments = {10: {"ID": "10", "NAME": "Алматинский филиал", "UF_HEAD": ""}}
    client = sheet_client(users, departments)
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(dry_run=True, distribution=sheet_distribution(users)),
    )
    subject.validate()

    results = []
    for index in range(3):
        app = replace(application(f"APP-{index + 1}"), bin=f"12345678901{index}")
        info = replace(
            enrichment(),
            bin=app.bin,
            director=f"Фамилия{index} Имя{index} Отчество{index}",
        )
        results.append(subject.process(app, info))

    assert [result.assigned_by_id for result in results] == [11, 11, 11]
    assert {result.assignment_reason for result in results} == {"single_scope_user_no_limit"}


def test_astana_each_manager_gets_one_new_founder_then_overflow_goes_to_head():
    users = [
        AssignmentUser(11, "Manager", 46, "УП г. Астана"),
        AssignmentUser(12, "Manager 2", 46, "УП г. Астана"),
        AssignmentUser(99, "Head", 46, "УП г. Астана", "РОП"),
    ]
    departments = {46: {"ID": "46", "NAME": "УП г. Астана", "UF_HEAD": "99"}}
    client = sheet_client(users, departments)
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(
            dry_run=True,
            random_seed=3,
            distribution=sheet_distribution(users),
        ),
    )
    subject.validate()

    results = []
    for index in range(3):
        app = replace(application(f"APP-{index + 1}"), bin=f"12345678901{index}")
        info = replace(
            enrichment(),
            bin=app.bin,
            director=f"Фамилия{index} Имя{index} Отчество{index}",
            legal_address="г. Астана",
            city="Астана",
            region="г. Астана",
        )
        results.append(subject.process(app, info))

    assert {results[0].assigned_by_id, results[1].assigned_by_id} == {11, 12}
    assert results[2].assigned_by_id == 99
    assert results[2].assignment_reason == "astana_overflow_to_rop"


def test_non_astana_department_with_multiple_users_does_not_require_rop():
    users = [
        AssignmentUser(31, "Taldyk manager 1", 14, "УП г. Талдыкорган"),
        AssignmentUser(32, "Taldyk manager 2", 14, "УП г. Талдыкорган"),
        AssignmentUser(33, "Taldyk manager 3", 14, "УП г. Талдыкорган"),
    ]
    departments = {14: {"ID": "14", "NAME": "УП г. Талдыкорган", "UF_HEAD": ""}}
    client = sheet_client(users, departments)
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(
            dry_run=True,
            random_seed=2,
            distribution=sheet_distribution(users),
        ),
    )

    subject.validate()
    results = []
    for index in range(5):
        app = replace(application(f"APP-TALDYK-{index}"), bin=f"1234567890{index:02d}")
        info = replace(
            enrichment(),
            bin=app.bin,
            director=f"Тестов{index} Тест{index} Тестович{index}",
            legal_address="г. Талдыкорган",
            city="Талдыкорган",
            region="область Жетісу",
        )
        results.append(subject.process(app, info))

    assert {result.assigned_by_id for result in results} <= {31, 32, 33}
    assert {result.assignment_reason for result in results} == {
        "non_astana_least_loaded_no_rop_limit"
    }


def test_missing_non_astana_owner_falls_back_to_department_manager_not_rop():
    users = [
        AssignmentUser(31, "Taldyk manager 1", 14, "УП г. Талдыкорган"),
        AssignmentUser(32, "Taldyk manager 2", 14, "УП г. Талдыкорган"),
        AssignmentUser(33, "Taldyk manager 3", 14, "УП г. Талдыкорган"),
    ]
    departments = {14: {"ID": "14", "NAME": "УП г. Талдыкорган", "UF_HEAD": ""}}
    contact = {"ID": "801", "ASSIGNED_BY_ID": "77"}
    client = sheet_client(
        users,
        departments,
        contact=contact,
        manager_loads={31: 3, 32: 0, 33: 5},
    )
    client.users[77] = {"ID": "77", "ACTIVE": "N", "UF_DEPARTMENT": [14]}
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(dry_run=True, distribution=sheet_distribution(users)),
    )

    subject.validate()
    result = subject.process(application(), enrichment())

    assert result.assigned_by_id == 32
    assert result.assignment_reason == "missing_owner_to_department_least_loaded"


def test_same_founder_keeps_manager_across_different_bins_without_second_capacity():
    users = [
        AssignmentUser(11, "Manager", 10, "Алматинский филиал"),
        AssignmentUser(12, "Manager 2", 10, "Алматинский филиал"),
        AssignmentUser(99, "Head", 10, "Алматинский филиал", "РОП"),
    ]
    departments = {10: {"ID": "10", "NAME": "Алматинский филиал", "UF_HEAD": "99"}}
    client = sheet_client(users, departments)
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(
            dry_run=True,
            random_seed=5,
            distribution=sheet_distribution(users),
        ),
    )
    subject.validate()

    first = subject.process(application("APP-1"), enrichment())
    second_app = replace(application("APP-2"), bin="123456789013")
    second = subject.process(second_app, replace(enrichment(), bin=second_app.bin))
    third_app = replace(application("APP-3"), bin="123456789014")
    third = subject.process(
        third_app,
        replace(enrichment(), bin=third_app.bin, director="Петров Петр Петрович"),
    )

    assert second.assigned_by_id == first.assigned_by_id
    assert second.assignment_reason == "same_founder_in_run"
    assert third.assigned_by_id in ({11, 12} - {first.assigned_by_id})


def test_random_tie_break_is_applied_only_after_minimum_bitrix_load():
    users = [
        AssignmentUser(11, "Busy manager", 10, "Филиал"),
        AssignmentUser(12, "Free manager", 10, "Филиал"),
        AssignmentUser(99, "Head", 10, "Филиал", "РОП"),
    ]
    departments = {10: {"ID": "10", "NAME": "Филиал"}}
    client = sheet_client(
        users,
        departments,
        manager_loads={11: 8, 12: 2, 99: 0},
    )
    subject = LeadPipeline(
        client,
        LeadPipelineConfig(dry_run=True, distribution=sheet_distribution(users)),
    )
    subject.validate()

    result = subject.process(application(), enrichment())

    assert result.assigned_by_id == 12
    assert result.assignment_reason == "non_astana_least_loaded_no_rop_limit"
