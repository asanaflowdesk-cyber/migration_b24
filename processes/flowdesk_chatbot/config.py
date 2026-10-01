from __future__ import annotations

from typing import Any


def leaf(route: str, fields: list[str] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"route": route}
    if fields:
        result["fields"] = fields
    return result


DETAIL_QUESTIONS = {
    "name_fio": "Введите название / ФИО:",
    "bin_iin": "Введите БИН / ИИН:",
    "doc_num": "Введите № договора:",
    "doc_date": "Введите дату договора:",
    "approv_list_num": "Введите № ЛС:",
    "reason": "Укажите причину:",
    "card_num": "Введите № СКД:",
    "card_position": "Укажите СКД должность:",
    "card_branch": "Укажите СКД подразделение:",
    "period": "Укажите период:",
}


# Пока известна только одна из пяти стартовых кнопок.
# Остальные 4 добавим сюда, когда будут точные названия.
# Если кнопка должна временно идти сразу в ветку 3:
# "Название": "branch_3"
REQUEST_TYPES = {
    "2.1 Запрос": "request_tree",
}


# Ветка 3:
# Суть обращения -> Вид страхования -> Продукт -> Подпродукт -> Документ
#
# Сюда надо подставить реальные продукты и подпродукты.
# Структура уже правильная: список продуктов зависит от вида страхования.
PRODUCT_TREE = {
    "Обязательное": {
        # "Продукт": ["Подпродукт 1", "Подпродукт 2"],
    },
    "Вмененное": {
        # "Продукт": ["Подпродукт 1", "Подпродукт 2"],
    },
    "Добровольное": {
        # "Продукт": ["Подпродукт 1", "Подпродукт 2"],
    },
}


# Ветка 4.
# Если для тупика инструкция есть -> показываем её и завершаем сценарий.
# Если инструкции пока нет -> временно отправляем пользователя в ветку 3.
INSTRUCTIONS: dict[tuple[str, ...], str] = {
    # Пример:
    # ("Юристы", "Согласование документа", "Договор страхования"):
    #     "Текст инструкции",
}


REQUEST_TREE = {
    "Юристы": {
        "Согласование документа": {
            "__prompt__": "Пожалуйста, укажите, какой документ вы хотите согласовать:",
            "Договор страхования": leaf("instruction"),
            "Доп соглашение": leaf("instruction"),
            "Тендерная тех. спецификация": leaf("instruction"),
            "Другое": leaf("branch_3"),
        },
        "Корректировка договора": leaf("instruction"),
        "Расторжение договора": {
            "Стандартное": leaf("instruction"),
            "По условиям": leaf(
                "details",
                ["name_fio", "bin_iin", "doc_num", "doc_date", "reason"],
            ),
        },
        "Выписать доверенность": leaf("instruction"),
        "Соглашение о конфиденциальности": leaf("branch_3"),
        "Заключить Договор": {
            "ГПХ": leaf("instruction"),
            "ТД": leaf("instruction"),
            "Аренды/субаренды": leaf("instruction"),
            "Другое": leaf("branch_3"),
        },
        "Нотариальное заверение": leaf("details", ["name_fio", "bin_iin"]),
        "Анкета опросник": leaf("branch_3"),
        "Другое": leaf("branch_3"),
    },

    "Статисты": {
        "Действие с договором": {
            "Несданные договоры": leaf("branch_3"),
            "Аннулирование договора": leaf("instruction"),
            "Расторжение договора": leaf("instruction"),
            "Подгрузка договора": leaf("instruction"),
            "Смена менеджера по договору": leaf("instruction"),
            "Другое": leaf("branch_3"),
        },
        "Корректировка / ПОД/ФТ": leaf("instruction"),
        "Бонусы/Премия": leaf("branch_3"),
        "Служебная записка": leaf("instruction"),
        "Другое": leaf("branch_3"),
    },

    "Андеррайтинг": {
        "Согласовать тариф": leaf(
            "details",
            ["bin_iin", "approv_list_num", "reason"],
        ),
        "Согласовать риски": leaf(
            "details",
            ["bin_iin", "approv_list_num", "reason"],
        ),
        "Согласовать франшизу": leaf(
            "details",
            ["bin_iin", "approv_list_num", "reason"],
        ),
        "Котировка (Смена менеджера)": leaf(
            "details",
            [
                "name_fio",
                "bin_iin",
                "doc_num",
                "doc_date",
                "approv_list_num",
                "reason",
            ],
        ),
        "Другое": leaf("branch_3"),
    },

    "Кадры": {
        "Прием сотрудника": leaf("instruction"),
        "Расторжение договора": leaf("instruction"),
        "Перевод сотрудника": leaf("instruction"),
        "Отпуск сотрудника": leaf("instruction"),
        "Выход в/из декрета": leaf("instruction"),
        "Присвоение категории": leaf("details", ["name_fio"]),
        "Справка с места": leaf("details", ["name_fio"]),
        "Другое": leaf("branch_3"),
    },

    "АХО": {
        "СКД Карта": {
            "Выпуск СКД карты": leaf(
                "details",
                ["name_fio", "bin_iin", "card_position", "card_branch"],
            ),
            "Редактирование СКД карты": leaf(
                "details",
                ["name_fio", "reason", "card_num"],
            ),
            "Другое": leaf("branch_3"),
        },
        "Договора с Тех. персоналом": {
            "Заключение": leaf("instruction"),
            "Изменение": leaf("instruction"),
            "Расторжение": leaf("instruction"),
            "Другое": leaf("branch_3"),
        },
        "ТМЦ": {
            "Заявка на ТМЦ": leaf("instruction"),
            "Списание ТМЦ": leaf("instruction"),
            "Счет на оплату ТМЦ": leaf("instruction"),
            "Другое": leaf("branch_3"),
        },
        "Ремонт": leaf("instruction"),
        "Переезд": leaf("instruction"),
        "Другое": leaf("branch_3"),
    },

    "Бухгалтерия": {
        "Счет на оплату": leaf("instruction"),
        "Возврат ДС": leaf("instruction"),
        "Списание ТМЦ": leaf("instruction"),
        "Справки": leaf("branch_3"),
        "Счета фактуры": leaf("instruction"),
        "Другое": leaf("branch_3"),
    },

    "IT": {
        "Настроить доступ в системы": leaf("instruction"),
        "Сообщение о сбое": leaf("instruction"),
        "Приобретение техники": leaf("instruction"),
        "Ошибка оператора (скоринг/е-агент)": leaf("instruction"),
        "Другое": leaf("branch_3"),
    },

    "Тендера": {
        "Справки": leaf("branch_3"),
        "Пруденциальный норматив": leaf("details", ["period"]),
        "Другое": leaf("branch_3"),
    },

    "Маркетологи": {
        "Заказ визиток": leaf("branch_3"),
        "Фирменная продукция / Вывески": leaf("instruction"),
        "Другое": leaf("branch_3"),
    },

    "Канцелярия": {
        "Входящее": leaf("instruction"),
        "Исходящее": leaf("instruction"),
        "Доверенности": leaf("branch_3"),
        "Другое": leaf("branch_3"),
    },

    "Предконтроль": leaf("details", ["bin_iin", "approv_list_num"]),
}
