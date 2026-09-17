import enrich_missing_directors as base
from enrich_missing_directors_v3 import ACTIVE_SOURCES, enrich_candidates_two_sources


def _candidate() -> dict:
    return {
        "company_id": 10,
        "title": "Тест",
        "owner_id": 27,
        "bin": "251040009938",
        "requisite_ids": [100],
        "status": "candidate",
    }


def test_only_adata_and_kompra_are_active_sources():
    assert ACTIVE_SOURCES == ("adata", "kompra")
    assert "ba_prg" not in ACTIVE_SOURCES


def test_one_source_is_enough(monkeypatch):
    def fake_results(bin_number: str, timeout: int):
        assert bin_number == "251040009938"
        return [
            {
                "source": "adata",
                "url": "https://adata/example",
                "director": "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА",
                "status": "found",
                "http": 200,
            },
            {
                "source": "kompra",
                "url": "https://kompra/example",
                "director": "",
                "status": "no_director",
                "http": 200,
            },
        ]

    monkeypatch.setattr(base, "base_source_results", fake_results)
    rows = enrich_candidates_two_sources([_candidate()], workers=2, timeout=3)
    assert rows[0]["status"] == "accepted"
    assert rows[0]["source"] == "adata"
    assert rows[0]["confidence"] == "single_source"
    assert "ba_prg_status" not in rows[0]


def test_two_different_directors_are_blocked(monkeypatch):
    def fake_results(_bin_number: str, _timeout: int):
        return [
            {
                "source": "adata",
                "url": "a",
                "director": "ОСПАНОВА НАЗЫМ БАТЫРБЕКОВНА",
                "status": "found",
                "http": 200,
            },
            {
                "source": "kompra",
                "url": "k",
                "director": "ИВАНОВА АННА ИВАНОВНА",
                "status": "found",
                "http": 200,
            },
        ]

    monkeypatch.setattr(base, "base_source_results", fake_results)
    rows = enrich_candidates_two_sources([_candidate()], workers=2, timeout=3)
    assert rows[0]["status"] == "source_conflict"
