from __future__ import annotations

import html
import re
from typing import Any


# Long legal forms are normalised only for CRM display names. The original
# source value remains in comments/requisites and is not destroyed.
_LEGAL_FORM_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "ТДО",
        (
            r"товарищество\s+с\s+дополнительной\s+ответственностью",
        ),
    ),
    (
        "ТОО",
        (
            r"товарищество\s+с\s+ограниченной\s+ответственностью",
            r"партнерство\s+с\s+ограниченной\s+ответственностью",
            r"partnership\s+with\s+limited\s+liability",
            r"limited\s+liability\s+partnership",
            r"limited\s+liability\s+company",
            r"\bllp\b",
            r"\bllc\b",
        ),
    ),
    (
        "АО",
        (
            r"акционерное\s+общество",
            r"joint\s+stock\s+company",
            r"\bjsc\b",
        ),
    ),
    (
        "ИП",
        (
            r"индивидуальн(?:ый|ого)\s+предпринимател(?:ь|я)",
            r"individual\s+entrepreneur",
            r"sole\s+proprietor",
        ),
    ),
    (
        "ЧК",
        (
            r"частная\s+компания",
            r"private\s+company",
        ),
    ),
    ("КТ", (r"коммандитное\s+товарищество",)),
    ("ПТ", (r"полное\s+товарищество",)),
    ("ПК", (r"производственный\s+кооператив",)),
    ("КХ", (r"крестьянское\s+хозяйство", r"фермерское\s+хозяйство")),
    ("ОО", (r"общественное\s+объединение",)),
    ("ОФ", (r"общественный\s+фонд",)),
    ("РГУ", (r"республиканское\s+государственное\s+учреждение",)),
    ("КГУ", (r"коммунальное\s+государственное\s+учреждение",)),
    ("ГУ", (r"государственное\s+учреждение",)),
    ("РГП", (r"республиканское\s+государственное\s+предприятие",)),
    ("КГП", (r"коммунальное\s+государственное\s+предприятие",)),
)

_SHORT_FORMS = (
    "ТДО", "ТОО", "АО", "ИП", "ЧК", "КТ", "ПТ", "ПК", "КХ", "ОО", "ОФ",
    "РГУ", "КГУ", "ГУ", "РГП", "КГП",
)

_QUOTES = '"“”«»„‟\'`’'


def _clean(value: Any) -> str:
    result = html.unescape(str(value or "")).replace("\xa0", " ")
    result = re.sub(r"\s+", " ", result).strip()
    return result


def _strip_outer_quotes(value: str) -> str:
    value = value.strip()
    while len(value) >= 2 and value[0] in _QUOTES and value[-1] in _QUOTES:
        value = value[1:-1].strip()
    return value


def short_organization_name(value: Any) -> str:
    """Return a compact CRM display name such as ``ТОО Kazmine``.

    The function deliberately changes only the display name. Full legal names
    remain available in source comments and requisites.
    """
    original = _clean(value)
    if not original:
        return ""

    working = original
    legal_form = ""

    # Existing short Russian form has priority over inferred English suffixes.
    short_match = re.match(
        rf"^\s*({'|'.join(map(re.escape, _SHORT_FORMS))})\b[\s\.,:;\-–—]*",
        working,
        flags=re.IGNORECASE,
    )
    if short_match:
        legal_form = short_match.group(1).upper()
        working = working[short_match.end():]

    for short_form, patterns in _LEGAL_FORM_PATTERNS:
        matched = False
        for pattern in patterns:
            if re.search(pattern, working, flags=re.IGNORECASE):
                working = re.sub(pattern, " ", working, flags=re.IGNORECASE)
                if not legal_form:
                    legal_form = short_form
                matched = True
        if matched and legal_form:
            # Continue through the remaining patterns so duplicated English and
            # Russian legal forms are both removed from the same source value.
            continue

    # Quotes in registry names are inconsistent and frequently unbalanced.
    # CRM display names are clearer without them; the full original stays in
    # comments and requisites.
    working = working.translate(str.maketrans({char: " " for char in _QUOTES}))
    # Common English legal suffix left after a Russian legal-form prefix.
    # Handle spellings such as ``Co., Ltd.``, ``Company Limited`` and ``Inc.``.
    working = re.sub(
        r"\b(?:(?:co(?:mpany)?\.?\s*,?\s*)?(?:ltd|limited)\.?|inc\.?|incorporated)\s*$",
        " ",
        working,
        flags=re.IGNORECASE,
    )
    working = re.sub(r"\s+", " ", working).strip(" . ,;:–—-")
    working = _strip_outer_quotes(working)

    if not working:
        return legal_form or original
    return f"{legal_form} {working}".strip() if legal_form else working


def clean_crm_title(value: Any) -> str:
    """Clean a generated CRM title and never use an en/em dash separator."""
    title = _clean(value)
    title = re.sub(r"^[\s❗⚠️‼️]+", "", title)
    title = re.sub(r"\s*[–—]\s*", ". ", title)
    title = re.sub(r"\.{2,}", ".", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    return title


_EQAZYNA_DOC_RE = re.compile(
    r"e[- ]?qazyna\s*№?\s*([0-9A-Za-zА-Яа-яЁё_-]*\d[0-9A-Za-zА-Яа-яЁё_-]*)",
    flags=re.IGNORECASE,
)


def extract_eqazyna_document_number(value: Any) -> str:
    match = _EQAZYNA_DOC_RE.search(_clean(value))
    return match.group(1) if match else ""


def extract_eqazyna_client_hint(value: Any) -> str:
    """Extract a company name from legacy e-Qazyna CRM titles."""
    title = _clean(value)
    title = re.sub(r"^[\s❗⚠️‼️]+", "", title)

    # Legacy parser: ``e-Qazyna лид — <full company name>``.
    suffix = re.match(
        r"^e[- ]?qazyna(?:\s+лид)?\s*[.\-–—:]+\s*(.+)$",
        title,
        flags=re.IGNORECASE,
    )
    if suffix:
        candidate = suffix.group(1).strip()
        if not candidate.startswith("№"):
            return short_organization_name(candidate)

    # Legacy migration: ``<full company name> — e-Qazyna № ...``.
    prefix = re.match(
        r"^(.+?)\s*[.\-–—:]+\s*e[- ]?qazyna\b",
        title,
        flags=re.IGNORECASE,
    )
    if prefix:
        return short_organization_name(prefix.group(1))
    return ""


def build_compact_crm_title(client_name: Any, source_title: Any, fallback: str = "") -> str:
    """Build ``<short client>. <short source title>`` for leads/deals."""
    raw_client = _clean(client_name)
    client = short_organization_name(raw_client)
    source = clean_crm_title(source_title) or clean_crm_title(fallback)

    # Canonical e-Qazyna reference: no emoji, no full legal name, no long dash.
    document_number = extract_eqazyna_document_number(source)
    if document_number:
        source = f"e-Qazyna № {document_number}"

    if raw_client and source:
        source = re.sub(re.escape(raw_client), client, source, flags=re.IGNORECASE)

    if client and source:
        if client.casefold() in source.casefold():
            result = source
        else:
            result = f"{client}. {source}"
    else:
        result = client or source or clean_crm_title(fallback)
    return clean_crm_title(result)[:250]


def build_eqazyna_title(company_name: Any, document_number: Any) -> str:
    company = short_organization_name(company_name)
    document = _clean(document_number)
    source = f"e-Qazyna № {document}" if document else "e-Qazyna"
    return build_compact_crm_title(company, source, source)
