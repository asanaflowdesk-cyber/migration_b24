from __future__ import annotations

import pytest

from eqazyna_bitrix.bitrix_client import BitrixError
from eqazyna_bitrix.lead_pipeline import LeadPipeline, LeadPipelineConfig
from eqazyna_bitrix.models import Application, CompanyEnrichment


FIELD = "UF_CRM_1785917145255"
VALUE = "ГПО Недропользователя"


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
        company=None,
        requisite=None,
        contact=None,
        address=None,
        field_type="string",
        field_items=None,
        requisite_presets=None,
    ):
        self.lead = lead
        self.company = company
        self.requisite = requisite
        self.contact = contact
        self.address = address
        self.field_type = field_type
        self.field_items = field_items
        self.requisite_presets = requisite_presets

        self.created_lead_fields = None
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
        return {FIELD: meta}

    def get_requisite_fields(self):
        return {
            "RQ_BIN": {},
            "RQ_COMPANY_NAME": {},
            "RQ_COMPANY_FULL_NAME": {},
            "RQ_DIRECTOR": {},
            "RQ_OKED": {},
        }

    def list_requisite_presets(self):
        if self.requisite_presets is not None:
            return self.requisite_presets
        return [{"ID": "1", "ENTITY_TYPE_ID": "4", "ACTIVE": "Y"}]

    def find_lead_by_origin(self, origin_id, originator_id="EQAZYNA_LEAD", extra_select=None):
        assert origin_id == "123456789012"
        assert originator_id == "EQAZYNA_LEAD"
        assert FIELD in (extra_select or [])
        return self.lead

    def create_lead(self, fields):
        self.created_lead_fields = fields
        return "501"

    def update_lead(self, lead_id, fields):
        assert lead_id == "77"
        self.updated_lead_fields = fields

    def find_company_by_origin(self, origin_id, originator_id="EQAZYNA"):
        assert origin_id == "123456789012"
        assert originator_id == "EQAZYNA"
        return self.company

    def find_company_by_bin(self, bin_number, bin_field="RQ_BIN"):
        assert bin_number == "123456789012"
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
        assert (last_name, name, second_name) == ("Иванов", "Иван", "Иванович")
        return self.contact

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
    assert fields["COMPANY_ID"] == 601
    assert fields["CONTACT_ID"] == 801
    assert fields["ASSIGNED_BY_ID"] == 36
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


def test_update_adds_new_application_but_preserves_stage_and_owner():
    first = application("APP-1")
    second = application("APP-2")
    client = FakeClient(
        lead={
            "ID": "77",
            "TITLE": "Старый заголовок",
            "STATUS_ID": "IN_PROCESS",
            "ASSIGNED_BY_ID": "116",
            "COMPANY_TITLE": "Старое название",
            "COMMENTS": f"Ключ заявки: {first.application_key}",
            "ORIGINATOR_ID": "EQAZYNA_LEAD",
            "ORIGIN_ID": second.bin,
            FIELD: "",
        }
    )

    result = pipeline(
        client,
        assigned_by_id="36",
        overwrite_assigned_by_on_update=False,
    ).process(second, enrichment())

    assert result.action == "existing_lead_new_application_added"
    assert result.assigned_by_id == 116
    assert client.updated_lead_fields[FIELD] == VALUE
    assert "STATUS_ID" not in client.updated_lead_fields
    assert "ASSIGNED_BY_ID" not in client.updated_lead_fields
    assert second.application_key in client.updated_lead_fields["COMMENTS"]


def test_legacy_migrated_lead_is_canonicalised_to_bin_marker():
    app = application("APP-2")
    client = FakeClient(
        lead={
            "ID": "77",
            "TITLE": "e-Qazyna № APP-1",
            "STATUS_ID": "IN_PROCESS",
            "ASSIGNED_BY_ID": "116",
            "COMMENTS": "Ключ заявки: eQazyna|APP-1|123456789012",
            "ORIGINATOR_ID": "EQAZYNA",
            "ORIGIN_ID": "eQazyna|APP-1|123456789012",
            FIELD: VALUE,
        }
    )

    result = pipeline(client).process(app, enrichment())

    assert result.action == "existing_lead_new_application_added"
    assert client.updated_lead_fields["ORIGINATOR_ID"] == "EQAZYNA_LEAD"
    assert client.updated_lead_fields["ORIGIN_ID"] == app.bin


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


def test_validate_rejects_missing_custom_field_or_requisite_bin_field():
    missing = FakeClient()
    missing.get_lead_fields = lambda: {}
    with pytest.raises(BitrixError, match=FIELD):
        LeadPipeline(missing, LeadPipelineConfig()).validate()

    missing_bin = FakeClient()
    missing_bin.get_requisite_fields = lambda: {"RQ_DIRECTOR": {}}
    with pytest.raises(BitrixError, match="RQ_BIN"):
        LeadPipeline(missing_bin, LeadPipelineConfig()).validate()


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



def test_missing_configured_requisite_preset_falls_back_to_active_company_preset():
    client = FakeClient(
        requisite_presets=[
            {
                "ID": "7",
                "NAME": "Организация Казахстан",
                "ENTITY_TYPE_ID": "4",
                "ACTIVE": "Y",
            }
        ]
    )

    result = pipeline(client, requisite_preset_id="3")

    assert result.requisite_preset_id == 7
    assert result.validation_warnings == [
        "PRESET_ID=3 в коробке не найден; автоматически выбран шаблон "
        "PRESET_ID=7 (Организация Казахстан)."
    ]


def test_existing_configured_requisite_preset_is_used_without_warning():
    client = FakeClient(
        requisite_presets=[
            {"ID": "3", "ENTITY_TYPE_ID": "4", "ACTIVE": "Y"},
            {"ID": "7", "ENTITY_TYPE_ID": "4", "ACTIVE": "Y"},
        ]
    )

    result = pipeline(client, requisite_preset_id="3")

    assert result.requisite_preset_id == 3
    assert result.validation_warnings == []

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
