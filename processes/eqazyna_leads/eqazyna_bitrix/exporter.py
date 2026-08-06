from __future__ import annotations

from pathlib import Path
from typing import Iterable

import xlsxwriter

from .models import ProcessResult


COLUMNS = [
    ("created_at_raw", "Дата заявки"),
    ("doc_number", "Номер заявки"),
    ("bin", "БИН"),
    ("applicant_name", "Заявитель e-Qazyna"),
    ("doc_type", "Тип документа"),
    ("status", "Статус заявки"),
    ("egov_name", "Название eGov"),
    ("legal_address", "Юридический адрес"),
    ("region", "Регион"),
    ("city", "Город"),
    ("director", "Руководитель"),
    ("activity", "Деятельность"),
    ("oked", "ОКЭД"),
    ("registration_date", "Дата регистрации"),
    ("phone", "Телефон eGov"),
    ("egov_name_score", "Совпадение названия eGov, %"),
    ("egov_oked_tpi", "ОКЭД ТПИ"),
    ("egov_match_reason", "Результат сопоставления eGov"),
    ("egov_error", "Ошибка eGov"),
    ("action", "Действие Bitrix24"),
    ("lead_id", "Bitrix Lead ID"),
    ("assigned_by_id", "Ответственный ID"),
    ("assignment_reason", "Правило ответственного"),
    ("status_id", "Стадия лида"),
    ("status_reason", "Правило стадии"),
    ("failure_reason", "Наследованная причина неудачи"),
    ("status_reference_lead_id", "Лид-источник стадии"),
    ("warning", "Предупреждение"),
    ("error", "Ошибка"),
    ("application_key", "Ключ заявки"),
    ("source_url", "Источник e-Qazyna"),
]


def write_xlsx(results: Iterable[ProcessResult], output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = xlsxwriter.Workbook(str(path))
    worksheet = workbook.add_worksheet("eqazyna_leads")

    header_format = workbook.add_format(
        {"bold": True, "bg_color": "#EDEDED", "border": 1, "text_wrap": True}
    )
    text_format = workbook.add_format({"text_wrap": True, "valign": "top"})
    link_format = workbook.add_format(
        {"font_color": "blue", "underline": True, "text_wrap": True, "valign": "top"}
    )

    for column_index, (_, title) in enumerate(COLUMNS):
        worksheet.write(0, column_index, title, header_format)
        worksheet.set_column(column_index, column_index, 18)

    worksheet.set_column(3, 3, 38)
    worksheet.set_column(7, 7, 48)
    worksheet.set_column(17, 18, 38)
    worksheet.set_column(23, 24, 42)
    worksheet.set_column(26, 26, 46)

    last_row = 0
    for row_index, result in enumerate(results, start=1):
        last_row = row_index
        data = result.as_dict()
        for column_index, (key, _) in enumerate(COLUMNS):
            value = data.get(key)
            if key == "source_url" and value:
                worksheet.write_url(
                    row_index,
                    column_index,
                    str(value),
                    link_format,
                    string=str(value),
                )
            else:
                worksheet.write(row_index, column_index, value if value is not None else "", text_format)

    worksheet.autofilter(0, 0, max(last_row, 1), len(COLUMNS) - 1)
    worksheet.freeze_panes(1, 0)
    workbook.close()
    return path
