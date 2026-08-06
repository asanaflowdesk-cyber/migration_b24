from __future__ import annotations

import pytest

from eqazyna_bitrix.bitrix_client import BitrixError
from eqazyna_bitrix.lead_pipeline import LeadPipeline, LeadPipelineConfig
from eqazyna_bitrix.models import Application, CompanyEnrichment


FIELD = "UF_CRM_1785917145255"
VALUE = "ГПО недропользователя"


def application(doc_number: str = "APP-1") -> Application:
    return Application(
        created_at_raw="01.08.2026 09:00:00",
        doc_number=doc_number,
        bin="123456789012",
        applicant_name="ТОО Тест Недра",
        doc_type="Заявка на разведку ТПИ",
        status="Принято",
        source_url="https://minerals.e-qazyna.kz/example",
    )


def enrichment() -> CompanyEnrichment:
    return CompanyEnrichment(
        bin="123456789012",
        name="ТОО Тест Недра",
        phone="+7 700 111 22 33",
        director="Иванов Иван Иванович",
        oked="07100",
        activity="Добыча железных руд",
    )


class FakeClient:
    def __init__(self, lead=None, field_type="string", field_items=None):
        self.lead = lead
        self.field_type = field_type
        self.field_items = field_items
        self.created_fields = None
        self.updated_fields = None
        self.timeline = []

    def get_lead_fields(self):
        meta = {"type": self.field_type, "title": "Тип лидогенерации"}
        if self.field_items is not None:
            meta["items"] = self.field_items
        return {FIELD: meta}

    def find_lead_by_origin(self, origin_id, originator_id="EQAZYNA_LEAD", extra_select=None):
        assert origin_id == "123456789012"
        assert originator_id == "EQAZYNA_LEAD"
        assert FIELD in (extra_select or [])
        return self.lead

    def create_lead(self, fields):
        self.created_fields = fields
        return "501"

    def update_lead(self, lead_id, fields):
        assert lead_id == "77"
        self.updated_fields = fields
        return True

    def add_timeline_comment(self, entity_type, entity_id, comment):
        self.timeline.append((entity_type, entity_id, comment))
        return "900"


def test_create_lead_writes_exact_generation_field_and_no_deal_entities():
    client = FakeClient()
    pipeline = LeadPipeline(client, LeadPipelineConfig(assigned_by_id="36"))

    pipeline.validate()
    result = pipeline.process(application(), enrichment())

    assert result.action == "created_lead"
    assert result.lead_id == "501"
    assert client.created_fields[FIELD] == VALUE
    assert client.created_fields["TITLE"] == "ТОО Тест Недра. e-Qazyna № APP-1"
    assert client.created_fields["COMPANY_TITLE"] == "ТОО Тест Недра"
    assert "—" not in client.created_fields["TITLE"]
    assert client.created_fields["ORIGINATOR_ID"] == "EQAZYNA_LEAD"
    assert client.created_fields["ORIGIN_ID"] == "123456789012"
    assert client.created_fields["STATUS_ID"] == "NEW"
    assert client.created_fields["ASSIGNED_BY_ID"] == 36
    assert len(client.timeline) == 1


def test_update_adds_new_application_but_preserves_status_and_owner():
    first = application("APP-1")
    second = application("APP-2")
    existing_comments = f"Старые данные\nКлюч заявки: {first.application_key}"
    client = FakeClient(
        {
            "ID": "77",
            "TITLE": "Старый заголовок",
            "STATUS_ID": "IN_PROCESS",
            "ASSIGNED_BY_ID": "116",
            "COMPANY_TITLE": "Старое название",
            "COMMENTS": existing_comments,
            FIELD: "",
        }
    )
    pipeline = LeadPipeline(
        client,
        LeadPipelineConfig(assigned_by_id="36", overwrite_assigned_by_on_update=False),
    )

    result = pipeline.process(second, enrichment())

    assert result.action == "existing_lead_new_application_added"
    assert result.assigned_by_id == 116
    assert client.updated_fields[FIELD] == VALUE
    assert "STATUS_ID" not in client.updated_fields
    assert "ASSIGNED_BY_ID" not in client.updated_fields
    assert second.application_key in client.updated_fields["COMMENTS"]
    assert len(client.timeline) == 1


def test_repeated_application_is_not_appended_or_commented_again():
    app = application("APP-1")
    enr = enrichment()
    from eqazyna_bitrix.formatter import build_lead_comment, build_lead_title

    comments = build_lead_comment(app, enr)
    client = FakeClient(
        {
            "ID": "77",
            "TITLE": build_lead_title(app, enr),
            "STATUS_ID": "QUALIFIED",
            "ASSIGNED_BY_ID": "116",
            "COMPANY_TITLE": enr.name,
            "COMMENTS": comments,
            "PHONE": [{"VALUE": enr.phone, "VALUE_TYPE": "WORK"}],
            "ORIGINATOR_ID": "EQAZYNA_LEAD",
            "ORIGIN_ID": app.bin,
            FIELD: VALUE,
        }
    )
    pipeline = LeadPipeline(client, LeadPipelineConfig())

    result = pipeline.process(app, enr)

    assert result.action == "existing_lead_unchanged"
    assert client.updated_fields is None
    assert client.timeline == []



def test_legacy_migrated_lead_is_canonicalised_to_one_bin_marker():
    app = application("APP-2")
    client = FakeClient(
        {
            "ID": "77",
            "TITLE": "e-Qazyna № APP-1",
            "STATUS_ID": "IN_PROCESS",
            "ASSIGNED_BY_ID": "116",
            "COMPANY_TITLE": "ТОО Тест Недра",
            "COMMENTS": "Ключ заявки: eQazyna|APP-1|123456789012",
            "ORIGINATOR_ID": "EQAZYNA",
            "ORIGIN_ID": "eQazyna|APP-1|123456789012",
            FIELD: VALUE,
        }
    )
    pipeline = LeadPipeline(client, LeadPipelineConfig())

    result = pipeline.process(app, enrichment())

    assert result.action == "existing_lead_new_application_added"
    assert client.updated_fields["ORIGINATOR_ID"] == "EQAZYNA_LEAD"
    assert client.updated_fields["ORIGIN_ID"] == app.bin


def test_validate_rejects_missing_or_unsupported_field():
    missing = FakeClient()
    missing.get_lead_fields = lambda: {}
    with pytest.raises(BitrixError, match=FIELD):
        LeadPipeline(missing, LeadPipelineConfig()).validate()

    unsupported = FakeClient(field_type="employee")
    with pytest.raises(BitrixError, match="неподдерживаемый"):
        LeadPipeline(unsupported, LeadPipelineConfig()).validate()


def test_enumeration_field_resolves_label_to_internal_id():
    client = FakeClient(
        field_type="enumeration",
        field_items=[
            {"ID": "41", "VALUE": "Другое"},
            {"ID": "42", "VALUE": "ГПО недропользователя"},
        ],
    )
    pipeline = LeadPipeline(client, LeadPipelineConfig())

    pipeline.validate()
    result = pipeline.process(application(), enrichment())

    assert result.action == "created_lead"
    assert client.created_fields[FIELD] == "42"


def test_enumeration_field_rejects_missing_or_ambiguous_label():
    missing = FakeClient(
        field_type="enumeration",
        field_items=[{"ID": "41", "VALUE": "Другое"}],
    )
    with pytest.raises(BitrixError, match="не найдено"):
        LeadPipeline(missing, LeadPipelineConfig()).validate()

    ambiguous = FakeClient(
        field_type="enumeration",
        field_items=[
            {"ID": "41", "VALUE": "ГПО недропользователя"},
            {"ID": "42", "VALUE": "гпо НЕДРОПОЛЬЗОВАТЕЛЯ"},
        ],
    )
    with pytest.raises(BitrixError, match="несколько"):
        LeadPipeline(ambiguous, LeadPipelineConfig()).validate()


def test_dry_run_never_writes():
    client = FakeClient()
    pipeline = LeadPipeline(client, LeadPipelineConfig(dry_run=True))

    result = pipeline.process(application(), enrichment())

    assert result.action == "dry_run_create_lead"
    assert client.created_fields is None
    assert client.updated_fields is None
    assert client.timeline == []
