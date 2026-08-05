from types import SimpleNamespace

from eqazyna_bitrix.models import Application, CompanyEnrichment, ProcessResult
import eqazyna_bitrix.main as main_module


class FakeSettings:
    request_timeout = 1
    eqazyna_request_timeout = 1
    bitrix_request_timeout = 1
    egov_request_timeout = 1
    polite_delay_seconds = 0
    bitrix_polite_delay_seconds = 0
    egov_polite_delay_seconds = 0
    egov_api_key = None
    bitrix_webhook_url = "https://bitrix.example.invalid/webhook/"
    bitrix_tls_verify = True

    @classmethod
    def from_env(cls):
        return cls()


class FakeScraper:
    failed_pages = []
    page_logs = []

    def __init__(self, *args, **kwargs):
        pass

    def scrape(self, *args, **kwargs):
        return [
            Application(
                created_at_raw="01.06.2026 10:00:00",
                doc_number="1-NEA",
                bin="123456789012",
                applicant_name="Test LLP",
                doc_type="Заявка на разведку ТПИ",
                status="Принято",
                source_url="https://example.test",
            )
        ]


class FakeEgov:
    def __init__(self, *args, **kwargs):
        pass

    def get_company(self, bin_number, name):
        return CompanyEnrichment(bin=bin_number, name=name)


class FakeClient:
    def __init__(self, *args, **kwargs):
        pass


class FakeLeadPipeline:
    instances = []

    def __init__(self, client, config):
        self.client = client
        self.config = config
        self.calls = 0
        self.validated = False
        FakeLeadPipeline.instances.append(self)

    def validate(self):
        self.validated = True

    def process(self, app, enrichment):
        self.calls += 1
        return ProcessResult(app, enrichment, action="dry_run_create_lead", lead_id="DRY_RUN")


def test_main_processes_each_application_once(monkeypatch, tmp_path):
    FakeLeadPipeline.instances.clear()
    monkeypatch.setattr(main_module, "Settings", FakeSettings)
    monkeypatch.setattr(main_module, "EqazynaScraper", FakeScraper)
    monkeypatch.setattr(main_module, "EgovClient", FakeEgov)
    monkeypatch.setattr(main_module, "BitrixClient", FakeClient)
    monkeypatch.setattr(main_module, "LeadPipeline", FakeLeadPipeline)
    monkeypatch.setattr(main_module, "write_xlsx", lambda results, path: path)
    monkeypatch.setattr(
        main_module,
        "parse_args",
        lambda: SimpleNamespace(
            pages=1,
            page_start=1,
            page_list=None,
            doc_type="Заявка на разведку ТПИ",
            statuses="Принято",
            min_created_date=None,
            out=str(tmp_path / "log.xlsx"),
            json_out=str(tmp_path / "log.json"),
            no_egov=False,
            push_bitrix=True,
            dry_run=True,
            lead_status_id="NEW",
            assigned_by_id="36",
            overwrite_assigned_by_on_update=False,
            lead_generation_field="UF_CRM_1785917145255",
            lead_generation_value="ГПО недропользователя",
            originator_id="EQAZYNA_LEAD",
            source_id="OTHER",
            source_description="e-Qazyna Minerals — ГПО недропользователи",
            skip_field_validation=False,
            strict_page_errors=False,
            max_consecutive_page_errors=5,
        ),
    )

    assert main_module.main() == 0
    assert len(FakeLeadPipeline.instances) == 1
    assert FakeLeadPipeline.instances[0].validated is True
    assert FakeLeadPipeline.instances[0].calls == 1
