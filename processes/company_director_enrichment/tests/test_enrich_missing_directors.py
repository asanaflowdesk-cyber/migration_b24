from enrich_missing_directors import (
    build_candidates,
    choose_director,
    extract_labeled_fio,
    fio_key,
    html_to_text,
    normalize_fio,
    page_has_bin,
    valid_fio,
)


def test_adata_example_extracts_director():
    html = """
    <html><body>
      <h1>ТОО \"БОЛАШАҚ ГЕОТЕХ\", БИН 251040009938</h1>
      <div>Основная информация</div>
      <div>БИН 251040009938</div>
      <div>Руководитель ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА Дата регистрации 08-10-2025</div>
    </body></html>
    """
    text = html_to_text(html)
    assert page_has_bin(text, "251040009938")
    assert extract_labeled_fio(text, ["Руководитель"]) == "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА"


def test_kompra_hidden_director_is_not_accepted():
    text = html_to_text("<div>БИН 251040009938</div><div>Первый руководитель Нет данных Дата актуальности 31.10.2025</div>")
    assert extract_labeled_fio(text, ["Первый руководитель"]) == ""


def test_ba_director_label_extracts_fio():
    text = html_to_text("<div>БИН 010140000450</div><div>Руководитель компании ЖЕҢІС ДАУРЕН ҚАЙРАТҰЛЫ Проверено 18.08.2026</div>")
    assert extract_labeled_fio(text, ["Руководитель компании"]) == "ЖЕҢІС ДАУРЕН ҚАЙРАТҰЛЫ"


def test_choose_single_exact_source():
    decision = choose_director([
        {"source": "adata", "url": "https://example/1", "director": "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА", "status": "found"},
        {"source": "kompra", "url": "https://example/2", "director": "", "status": "no_director"},
    ])
    assert decision["status"] == "accepted"
    assert decision["source"] == "adata"
    assert decision["confidence"] == "single_source"


def test_two_sources_resolve_third_source_conflict():
    decision = choose_director([
        {"source": "adata", "url": "a", "director": "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА", "status": "found"},
        {"source": "kompra", "url": "k", "director": "ИВАНОВА АННА ИВАНОВНА", "status": "found"},
        {"source": "ba_prg", "url": "b", "director": "Оспанова Назым Батырбековна", "status": "found"},
    ])
    assert decision["status"] == "accepted"
    assert fio_key(decision["director"]) == fio_key("ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА")
    assert decision["confidence"] == "confirmed"


def test_two_conflicting_sources_are_blocked():
    decision = choose_director([
        {"source": "adata", "url": "a", "director": "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА", "status": "found"},
        {"source": "kompra", "url": "k", "director": "ИВАНОВА АННА ИВАНОВНА", "status": "found"},
    ])
    assert decision["status"] == "source_conflict"


def test_candidate_requires_missing_director_and_exact_bin():
    snapshot = {
        "companies": [
            {"ID": "10", "TITLE": "Без директора", "ASSIGNED_BY_ID": "27", "ORIGIN_ID": ""},
            {"ID": "20", "TITLE": "Уже есть", "ASSIGNED_BY_ID": "27", "ORIGIN_ID": ""},
        ],
        "requisites": [
            {"ID": "100", "ENTITY_ID": "10", "RQ_INN": "251040009938", "RQ_DIRECTOR": ""},
            {"ID": "200", "ENTITY_ID": "20", "RQ_INN": "010140000450", "RQ_DIRECTOR": "Иванов Иван Иванович"},
        ],
        "contacts": [],
    }
    candidates, skipped = build_candidates(snapshot)
    assert [(row["company_id"], row["bin"]) for row in candidates] == [(10, "251040009938")]
    assert any(row["company_id"] == 20 and row["status"] == "director_already_present" for row in skipped)


def test_director_contact_already_present_blocks_external_enrichment():
    snapshot = {
        "companies": [{"ID": "10", "TITLE": "Компания", "ASSIGNED_BY_ID": "27", "ORIGIN_ID": "251040009938"}],
        "requisites": [],
        "contacts": [{
            "ID": "500", "COMPANY_ID": "10", "LAST_NAME": "Оспанова", "NAME": "Назым",
            "SECOND_NAME": "Батырбековна", "POST": "Руководитель", "COMMENTS": "", "ASSIGNED_BY_ID": "27",
        }],
    }
    candidates, skipped = build_candidates(snapshot)
    assert candidates == []
    assert skipped[0]["status"] == "director_already_present"


def test_fio_validation_is_strict_enough_for_directory_labels():
    assert valid_fio("ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА")
    assert normalize_fio(" Оспанова   Назым Батырбековна ") == "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА"
    assert not valid_fio("Нет данных")
