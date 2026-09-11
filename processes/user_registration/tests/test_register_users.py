from __future__ import annotations

import sys

import register_users_from_excel as registration


def input_user(row: int, email: str) -> registration.InputUser:
    return registration.InputUser(
        row_number=row,
        last_name="Иванов",
        name="Иван",
        second_name="Иванович",
        email=email,
        department="Поддержка",
        department_id="19",
    )


class FakeClient:
    add_result = "101"

    @classmethod
    def from_env(cls):
        return cls()

    def list_all(self, method, _params):
        if method == "department.get":
            return [{"ID": "19", "NAME": "Поддержка"}]
        if method == "user.get":
            return []
        raise AssertionError(method)

    def call(self, method, _params):
        assert method == "user.add"
        return self.add_result


class FakeSheetClient:
    @classmethod
    def from_env(cls, *_args, **_kwargs):
        return cls()


def test_created_user_id_accepts_nested_positive_id() -> None:
    assert registration.created_user_id({"result": {"user": {"ID": "42"}}}) == "42"
    assert registration.created_user_id({"result": {"ok": True}}) == ""


def test_duplicate_full_name_with_another_email_is_rejected(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(
        registration,
        "read_users_from_sheet",
        lambda *_: [input_user(2, "first@example.kz"), input_user(3, "second@example.kz")],
    )
    monkeypatch.setattr(registration, "user_list_name_rows", lambda *_: {})
    monkeypatch.setattr(registration, "GoogleSheetsClient", FakeSheetClient)
    monkeypatch.setattr(registration, "BitrixClient", FakeClient)
    monkeypatch.setattr(registration, "write_reports", lambda actions, *_: captured.extend(actions))
    monkeypatch.setattr(sys, "argv", ["register_users_from_excel.py", "--mode", "dry_run"])

    assert registration.main() == 1
    assert [action.status for action in captured] == ["DRY_RUN", "ERROR"]
    assert captured[1].message == "Повтор ФИО в new_users_add с другим email"


def test_apply_rejects_success_response_without_user_id(monkeypatch) -> None:
    class InvalidResultClient(FakeClient):
        add_result = {"ok": True}

    captured = []
    monkeypatch.setattr(registration, "read_users_from_sheet", lambda *_: [input_user(2, "user@example.kz")])
    monkeypatch.setattr(registration, "user_list_name_rows", lambda *_: {})
    monkeypatch.setattr(registration, "write_user_back", lambda *args, **kwargs: None)
    monkeypatch.setattr(registration, "GoogleSheetsClient", FakeSheetClient)
    monkeypatch.setattr(registration, "BitrixClient", InvalidResultClient)
    monkeypatch.setattr(registration, "write_reports", lambda actions, *_: captured.extend(actions))
    monkeypatch.setattr(sys, "argv", ["register_users_from_excel.py", "--mode", "apply"])

    assert registration.main() == 1
    assert captured[0].status == "ERROR"
    assert "не вернул положительный ID" in captured[0].message


def test_reads_new_users_add_columns_and_splits_fio() -> None:
    class Sheet:
        def read_rows(self, _sheet_name):
            return [
                [
                    "NEW_id",
                    "Филиал",
                    "Ф.И.О",
                    "Должность",
                    "электронный адрес",
                    "внутренний номер",
                    "мобильный +",
                ],
                [
                    "",
                    "46",
                    "Ибраева Асель Шохановна",
                    "менеджер по страхованию",
                    "Assel.Ibrayeva@theeurasia.kz",
                    "4319",
                    "7-705-781-91-93",
                ],
            ]

    result = registration.read_users_from_sheet(Sheet(), "new_users_add")

    assert len(result) == 1
    assert (result[0].last_name, result[0].name, result[0].second_name) == (
        "Ибраева",
        "Асель",
        "Шохановна",
    )
    assert result[0].department_id == "46"
    assert result[0].position == "менеджер по страхованию"


def test_writeback_updates_new_id_and_distribution_user_list() -> None:
    class Sheet:
        def __init__(self):
            self.updates = None

        def batch_update(self, updates):
            self.updates = updates

        def append_row(self, *_args):
            raise AssertionError("existing user_list row must be updated, not appended")

    sheet = Sheet()
    row = input_user(2, "user@example.kz")
    row.department_id = "46"
    registration.write_user_back(
        sheet,
        input_sheet="new_users_add",
        user_list_sheet="user_list",
        user_list_rows={registration.normalize(row.full_name): 2},
        row=row,
        user_id="101",
        department_name="УП г. Астана",
    )

    assert sheet.updates == [
        ("'new_users_add'!A2", [[101]]),
        ("'user_list'!A2", [[101]]),
        ("'user_list'!C2:D2", [[46, "УП г. Астана"]]),
    ]


def test_apply_creates_user_with_sheet_fields_and_writes_id_back(monkeypatch) -> None:
    class CapturingClient(FakeClient):
        fields = None

        def list_all(self, method, _params):
            if method == "department.get":
                return [{"ID": "46", "NAME": "УП г. Астана"}]
            if method == "user.get":
                return []
            raise AssertionError(method)

        def call(self, method, params):
            assert method == "user.add"
            type(self).fields = params
            return "101"

    row = registration.InputUser(
        row_number=2,
        last_name="Ибраева",
        name="Асель",
        second_name="Шохановна",
        email="Assel.Ibrayeva@theeurasia.kz",
        department="",
        department_id="46",
        position="менеджер по страхованию",
        work_phone="4319",
        mobile_phone="7-705-781-91-93",
    )
    writes = []
    captured = []
    monkeypatch.setattr(registration, "read_users_from_sheet", lambda *_: [row])
    monkeypatch.setattr(registration, "user_list_name_rows", lambda *_: {})
    monkeypatch.setattr(registration, "write_user_back", lambda *args, **kwargs: writes.append(kwargs))
    monkeypatch.setattr(registration, "GoogleSheetsClient", FakeSheetClient)
    monkeypatch.setattr(registration, "BitrixClient", CapturingClient)
    monkeypatch.setattr(registration, "write_reports", lambda actions, *_: captured.extend(actions))
    monkeypatch.setattr(sys, "argv", ["register_users_from_excel.py", "--mode", "apply"])

    assert registration.main() == 0
    assert CapturingClient.fields["UF_DEPARTMENT"] == [46]
    assert CapturingClient.fields["UF_PHONE_INNER"] == "4319"
    assert CapturingClient.fields["PERSONAL_MOBILE"] == "7-705-781-91-93"
    assert CapturingClient.fields["WORK_POSITION"] == "менеджер по страхованию"
    assert writes[0]["user_id"] == "101"
    assert captured[0].status == "OK"
