from pathlib import Path

from src.migration import MigrationProject, migration_marker, parse_marker
from src.xlsx_reader import XlsxReader

ROOT = Path(__file__).resolve().parents[1]


def project(tmp_path):
    return MigrationProject(
        ROOT / "data/bitrix24_export.xlsx",
        ROOT / "config/migration.json",
        ROOT / "config/users.csv",
        tmp_path,
        client=None,
    )


def test_source_workbook_counts():
    with XlsxReader(ROOT / "data/bitrix24_export.xlsx") as reader:
        assert len(reader.rows("Companies")) == 601
        assert len(reader.rows("Contacts")) == 441
        assert len(reader.rows("Leads")) == 4
        assert len(reader.rows("Deals")) == 1443


def test_routing_counts(tmp_path):
    p = project(tmp_path)
    p.load_source("Deals")
    routes = [p.route_source_deal(row)[0] for row in p._source["Deals"]]
    assert routes.count("lead") == 1326
    assert routes.count("deal") == 117


def test_plan_expected_counts(tmp_path):
    plan = project(tmp_path).source_plan()
    assert plan["expected_target_leads_total"] == 1330
    assert plan["expected_target_deals_total"] == 117


def test_marker_round_trip():
    marker = migration_marker("DEAL", 123, "LEAD")
    assert parse_marker("x " + marker + " y") == ("DEAL", "123", "LEAD")
