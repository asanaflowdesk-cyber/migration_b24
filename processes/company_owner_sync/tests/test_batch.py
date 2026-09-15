import pytest
from sync_founder_packages import parse_contact_ids, run


def test_parse():
    assert parse_contact_ids("1,2; 3\n1") == [1, 2, 3]


@pytest.mark.parametrize("value", ["", " ", "0", "1,-2", "1 & echo x", "1.5"])
def test_bad_ids(value):
    with pytest.raises(ValueError):
        parse_contact_ids(value)


class Client:
    def __init__(self):
        self.loads = []
        self.contacts = [{"ID": str(i), "LAST_NAME": f"Фамилия{i}", "NAME": "Иван", "POST": "Учредитель", "ASSIGNED_BY_ID": str(i+10)} for i in (1,2,3)]

    def list_all(self, method, payload):
        self.loads.append(method)
        if method == "crm.contact.list":
            return self.contacts
        return []

    def call(self, method, payload):
        return next(item for item in self.contacts if int(item["ID"]) == payload["id"])


def test_batch_reads_each_table_once_and_selects_only_requested(tmp_path):
    client = Client()
    summary = run(client, tmp_path, False, source_contact_ids=[1,2])
    assert summary["packages"] == 2
    assert summary["errors"] == 0
    assert len(client.loads) == 4
    assert len(set(client.loads)) == 4


def test_missing_source_fails_closed(tmp_path):
    summary = run(Client(), tmp_path, False, source_contact_ids=[99])
    assert summary["errors"] == 1
