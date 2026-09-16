from package_reconciler import process_package


class Client:
    def __init__(self):
        self.source_owner = 17
        self.company_owner = 9
        self.lead_owner = 9
        self.fail_lead_once = True

    def call(self, method, payload):
        if method == "crm.contact.get":
            return {"ID": "100", "POST": "Учредитель", "ASSIGNED_BY_ID": self.source_owner}
        if method == "crm.company.get":
            return {"ID": str(payload["id"]), "ASSIGNED_BY_ID": self.company_owner}
        if method == "crm.lead.get":
            return {"ID": str(payload["id"]), "ASSIGNED_BY_ID": self.lead_owner}
        raise AssertionError(method)

    def get_user(self, user_id):
        return {"ID": str(user_id), "NAME": f"User{user_id}", "LAST_NAME": "Test"}

    def update_company(self, item_id, fields):
        self.company_owner = int(fields["ASSIGNED_BY_ID"])

    def update_lead(self, item_id, fields):
        if self.fail_lead_once:
            self.fail_lead_once = False
            return
        self.lead_owner = int(fields["ASSIGNED_BY_ID"])

    def update_contact(self, item_id, fields):
        raise AssertionError("source contact must not be rewritten")


def package():
    return {
        "fio": "Иванов Иван",
        "owner_id": 17,
        "source_contact_id": 100,
        "contacts": [{"id": 100, "title": "Иванов Иван", "owner_id": 17}],
        "companies": [
            {
                "id": 10,
                "title": "ТОО А",
                "owner_id": 9,
                "leads": [{"id": 20, "title": "Заявка 20", "owner_id": 9}],
            }
        ],
    }


def test_failed_first_write_is_reconciled_and_only_remainder_is_retried(tmp_path):
    client = Client()
    snapshots = []
    result = process_package(
        client,
        tmp_path,
        package(),
        source_contact_id=100,
        operation_id="op-1",
        claim_id="claim-1",
        attempt=1,
        progress=lambda operation, items: snapshots.append((dict(operation), list(items))),
        max_reconcile_rounds=3,
    )

    assert result["success"] is True
    assert result["status"] == "DONE"
    assert client.company_owner == 17
    assert client.lead_owner == 17
    assert result["operation"]["remaining"] == 0
    assert result["operation"]["updated"] == 2
    assert result["operation"]["from_owner_ids"] == "9"
    assert result["operation"]["to_owner_id"] == 17
    assert snapshots
    assert (tmp_path / "operation.json").exists()


def test_third_party_owner_change_becomes_manual_review(tmp_path):
    client = Client()
    client.fail_lead_once = False
    client.company_owner = 33
    source_package = package()
    source_package["companies"][0]["owner_id"] = 9

    result = process_package(
        client,
        tmp_path,
        source_package,
        source_contact_id=100,
        operation_id="op-2",
        claim_id="claim-2",
        attempt=1,
        max_reconcile_rounds=1,
    )

    assert result["success"] is False
    assert result["status"] == "MANUAL_REVIEW"
    assert result["operation"]["conflicts"] == 1
    company = next(item for item in result["items"] if item["entity"] == "company")
    assert company["status"] == "CONFLICT"
    assert company["actual_owner_id"] == 33
