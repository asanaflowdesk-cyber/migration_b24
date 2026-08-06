from eqazyna_bitrix.scraper import parse_applications


def test_parse_text_fallback():
    html = """
    Дата создания Номер документа ИИН/БИН заявителя Наименование заявителя Тип документа Статус заявки
    21.05.2026 14:42:23 42480-NEA 260140012851 Товарищество с ограниченной ответственностью "Жетісу Минерал Ресорс"Заявка на разведку ТПИ Отправлено на рассмотрение
    21.05.2026 14:35:23 42479-NOA 060440012256 ТОО "Другой"Отчетность ЛКУ Отправлено на рассмотрение
    """
    rows = parse_applications(html, "https://example.com", doc_types=["Заявка на разведку ТПИ"])
    assert len(rows) == 2
    first = rows[0]
    assert first.doc_number == "42480-NEA"
    assert first.bin == "260140012851"
    assert first.doc_type == "Заявка на разведку ТПИ"
    assert first.status == "Отправлено на рассмотрение"

from eqazyna_bitrix.scraper import EqazynaScraper


class FakeScraperNoFallback(EqazynaScraper):
    def __init__(self):
        super().__init__(polite_delay_seconds=0)
        self.calls = []

    def fetch_page(self, page, doc_type, statuses):
        self.calls.append((page, doc_type, tuple(sorted(statuses or []))))
        if doc_type is None:
            html = """
            <table><tr><td>01.06.2026 10:00:00</td><td>1-NEA</td><td>123456789012</td><td>Test LLP</td><td>Заявка на разведку ТПИ</td><td>Принято</td></tr></table>
            """
            return html, "https://example.test/unfiltered"
        return "<html><body>empty filtered page</body></html>", "https://example.test/filtered"


def test_scraper_does_not_use_unfiltered_fallback_when_filters_active():
    scraper = FakeScraperNoFallback()

    rows = scraper.scrape(
        pages=1,
        doc_type="Заявка на разведку ТПИ",
        statuses=["Принято"],
        stop_on_empty_page=True,
    )

    assert rows == []
    assert all(call[1] is not None for call in scraper.calls)
    assert len(scraper.calls) == 1


class _FakeResponse:
    encoding = "utf-8"
    apparent_encoding = "utf-8"

    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _FlakySession:
    def __init__(self):
        self.headers = {}
        self.calls = 0
        self.timeouts = []

    def get(self, url, timeout, allow_redirects):
        self.calls += 1
        self.timeouts.append(timeout)
        if self.calls < 3:
            from requests import ReadTimeout

            raise ReadTimeout("temporary timeout")
        return _FakeResponse(
            """
            <h1>Реестр заявок</h1>
            <table><tr><td>01.06.2026 10:00:00</td><td>1-NEA</td><td>123456789012</td><td>Test LLP</td><td>Заявка на разведку ТПИ</td><td>Принято</td></tr></table>
            """
        )


def test_fetch_page_retries_transient_read_timeout(monkeypatch):
    session = _FlakySession()
    scraper = EqazynaScraper(
        timeout=60,
        max_retries=4,
        retry_base_sleep_seconds=0,
        session=session,
    )
    monkeypatch.setattr("eqazyna_bitrix.scraper.time.sleep", lambda _: None)

    html, url = scraper.fetch_page(
        1,
        "Заявка на разведку ТПИ",
        ["Отправлено на рассмотрение", "Принято"],
    )

    assert "1-NEA" in html
    assert session.calls == 3
    assert session.timeouts == [(15, 60), (15, 60), (15, 60)]
    assert "flDocType=%D0%A2%D0%BF%D0%B8%D0%97%D0%B0%D1%8F%D0%B2%D0%BA%D0%B0%D0%9D%D0%B0%D0%A0%D0%B0%D0%B7%D0%B2%D0%B5%D0%B4%D0%BA%D1%83" in url
