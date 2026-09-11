from __future__ import annotations

import pytest

from eqazyna_bitrix.distribution import (
    DistributionError,
    GoogleSheetDistributionSource,
    branch_key,
)


class Response:
    def __init__(self, text: str):
        self.content = text.encode("utf-8")

    def raise_for_status(self):
        return None


class Session:
    def __init__(self, sheets):
        self.sheets = sheets

    def get(self, url, timeout):
        name = url.rsplit("sheet=", 1)[1]
        return Response(self.sheets[name])


def source(users_csv: str, fixes_csv: str) -> GoogleSheetDistributionSource:
    return GoogleSheetDistributionSource(
        "sheet-id",
        session=Session({"user_list": users_csv, "Company_fix": fixes_csv}),
    )


def test_loads_users_and_company_fixes_from_exact_columns():
    result = source(
        "user_id,full_name,DepartmentID,name,role\n11,Иванова Анна,10,Алматинский филиал,менеджер по страхованию\n",
        "responsible_id,full_name,ORIGIN_ID,TITLE\n77,Петров Петр,123 456 789 012,ТОО Тест\n",
    ).load()

    assert result.users[0].user_id == 11
    assert result.users[0].department_id == 10
    assert result.users[0].role == "менеджер по страхованию"
    assert result.company_assignments == {"123456789012": 77}


def test_conflicting_company_fix_is_blocking():
    with pytest.raises(DistributionError, match="разные ответственные"):
        source(
            "user_id,full_name,DepartmentID,name,role\n11,Иванова Анна,10,Алматинский филиал,менеджер по страхованию\n",
            "responsible_id,full_name,ORIGIN_ID,TITLE\n77,A,123456789012,X\n78,B,123456789012,X\n",
        ).load()


def test_empty_user_list_is_blocking_instead_of_using_stale_ids():
    with pytest.raises(DistributionError, match="не содержит участников"):
        source(
            "user_id,full_name,DepartmentID,name,role\n",
            "responsible_id,full_name,ORIGIN_ID,TITLE\n",
        ).load()


def test_missing_headers_are_blocking_even_when_sheet_has_no_data():
    with pytest.raises(DistributionError, match="обязательные столбцы"):
        source(
            "id,full_name,DepartmentID,name,role\n",
            "responsible_id,full_name,ORIGIN_ID,TITLE\n",
        ).load()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("УП г. Астана", "astana"),
        ("Мангистауская область, г. Актау", "aktau"),
        ("Управление продаж Петропавловского филиала", "petropavl"),
    ],
)
def test_branch_detection(value, expected):
    assert branch_key(value) == expected
