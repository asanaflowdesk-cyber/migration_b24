from __future__ import annotations

from urllib.parse import quote


def build_search_links(bin_number: str, company_name: str, city: str | None = None) -> dict[str, str]:
    """Build human-check links. 2GIS search works better without BIN in the path."""
    clean_company = " ".join((company_name or "").split())
    place = f" {city}" if city else " Казахстан"
    two_gis_query = quote(f"{clean_company}{place}", safe="")
    web_query = quote(f"{bin_number} {clean_company} телефон контакты", safe="")
    return {
        "2gis": f"https://2gis.kz/search/{two_gis_query}",
        "google": f"https://www.google.com/search?q={web_query}",
        "yandex": f"https://yandex.kz/search/?text={web_query}",
    }
