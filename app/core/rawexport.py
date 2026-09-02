"""Повне вивантаження зібраних даних одним файлом.

Тут навмисно немає жодних підсумків, часток і рейтингів — лише сирі рядки,
придатні для власного аналізу у зведених таблицях. Кожен аркуш — одна сутність,
пов'язана з рештою через номер закупівлі або ЄДРПОУ.
"""
from __future__ import annotations

from typing import Any, Sequence

from ..config import METHOD_LABELS, STATUS_LABELS
from . import rejections as rj
from .db import Database
from .extract import SCOPE_LABELS

Sheet = tuple[Sequence[str], list[list[Any]]]

#: Пояснення до кожного аркуша — потрапляє першим аркушем книги.
SHEET_NOTES = {
    "Закупівлі": "Одна закупівля — один рядок. Ключ для зв'язку з іншими аркушами — «Номер закупівлі».",
    "Лоти": "Лоти закупівель. У закупівлі без поділу на лоти цього рядка не буде.",
    "Номенклатура": "Позиції закупівель: товар, код ДК021, кількість і ціна за одиницю. "
                    "Основа для аналізу товару та торгових марок.",
    "Пропозиції": "Хто подавався і з якою сумою. Видно лише там, де процедура розкриває пропозиції.",
    "Переможці": "Рішення про визначення переможця. Для відхилених — підстава, її категорія "
                 "та пояснення замовника.",
    "Договори": "Укладені договори з ЄДРПОУ постачальника й сумою.",
    "Відміни закупівель": "Закупівлі, які замовник відмінив: підстава з переліку та обґрунтування.",
    "Документи": "Реєстр усіх файлів закупівель: тип, чий файл, стан завантаження та шлях на диску.",
    "Товари каталогу": "Картки товарів Prozorro Market: бренд, штрихкод, ціновий діапазон, фото, повнота картки.",
    "Характеристики товарів": "Технічні параметри товарів у довгому форматі: товар × характеристика × значення.",
}


def _label(mapping: dict, value) -> str:
    return mapping.get(str(value or ""), str(value or ""))


def tenders(db: Database) -> Sheet:
    headers = ["Номер закупівлі", "Дата оприлюднення", "Предмет закупівлі", "Опис",
               "Коди ДК021", "Статус", "Процедура", "Категорія предмета",
               "Очікувана вартість", "Валюта", "ПДВ включено",
               "Замовник", "ЄДРПОУ замовника", "Регіон", "Населений пункт",
               "Лотів", "Пропозицій", "Файлів",
               "Початок подання", "Кінець подання", "Остання зміна", "Посилання"]
    rows = []
    for r in db.query("SELECT * FROM tenders ORDER BY date_created DESC"):
        rows.append([
            r["tender_id"], (r["date_created"] or "")[:10], r["title"], r["description"],
            r["cpv_list"], _label(STATUS_LABELS, r["status"]),
            _label(METHOD_LABELS, r["method_type"]), r["main_category"],
            r["value_amount"], r["value_currency"], "так" if r["vat_included"] else "ні",
            r["pe_name"], r["pe_edrpou"], r["pe_region"], r["pe_locality"],
            r["n_lots"], r["n_bids"], r["n_docs"],
            (r["tender_start"] or "")[:10], (r["tender_end"] or "")[:10],
            (r["date_modified"] or "")[:10],
            f"https://prozorro.gov.ua/tender/{r['tender_id']}",
        ])
    return headers, rows


def lots(db: Database) -> Sheet:
    headers = ["Номер закупівлі", "Лот", "Опис лоту", "Статус лоту",
               "Очікувана вартість лоту", "Валюта"]
    rows = [[r["tender_id"], r["title"], r["description"], r["status"],
             r["value_amount"], r["currency"]]
            for r in db.query("""
                SELECT t.tender_id, l.* FROM lots l
                JOIN tenders t ON t.uuid = l.tender_uuid
                ORDER BY t.date_created DESC""")]
    return headers, rows


def items(db: Database) -> Sheet:
    """Номенклатура закупівель — найцінніший аркуш для аналізу товару.

    Саме тут лежить опис позиції з назвою моделі й торговою маркою, код ДК021
    на рівні конкретного товару та ціна за одиницю. Без цього аркуша аналітика
    бачить лише узагальнений предмет закупівлі й не може сказати, з чим саме
    конкурент виходить на торги.
    """
    headers = ["Номер закупівлі", "Дата", "Позиція", "Код ДК021", "Назва коду",
               "Кількість", "Одиниця", "Очікувана вартість лоту", "Ціна за одиницю",
               "Замовник", "ЄДРПОУ замовника", "Регіон"]
    rows = []
    for r in db.query("""
            SELECT t.tender_id, t.date_created, t.pe_name, t.pe_edrpou, t.pe_region,
                   i.description, i.cpv, i.cpv_name, i.quantity, i.unit,
                   COALESCE(l.value_amount, t.value_amount) AS lot_value
            FROM items i
            JOIN tenders t ON t.uuid = i.tender_uuid
            LEFT JOIN lots l ON l.id = i.lot_id
            ORDER BY t.date_created DESC"""):
        quantity = r["quantity"] or 0
        lot_value = r["lot_value"] or 0
        unit_price = round(lot_value / quantity, 2) if quantity and lot_value else None
        rows.append([
            r["tender_id"], (r["date_created"] or "")[:10], r["description"],
            r["cpv"], r["cpv_name"], r["quantity"], r["unit"],
            lot_value or None, unit_price,
            r["pe_name"], r["pe_edrpou"], r["pe_region"],
        ])
    return headers, rows


def bids(db: Database) -> Sheet:
    headers = ["Номер закупівлі", "Дата закупівлі", "Учасник", "ЄДРПОУ учасника",
               "Регіон учасника", "Сума пропозиції", "Валюта", "Статус пропозиції",
               "Дата подання"]
    rows = [[r["tender_id"], (r["date_created"] or "")[:10], r["bidder_name"],
             r["bidder_edrpou"], r["bidder_region"], r["value_amount"], r["currency"],
             r["status"], (r["date"] or "")[:10]]
            for r in db.query("""
                SELECT t.tender_id, t.date_created, b.* FROM bids b
                JOIN tenders t ON t.uuid = b.tender_uuid
                ORDER BY t.date_created DESC""")]
    return headers, rows


def awards(db: Database) -> Sheet:
    """Рішення про переможця — разом із підставою, якщо переможця відхилили.

    Категорію дописуємо тут заради зведених таблиць користувача; аналітика
    її не читає, а класифікує текст сама, щоб зміна довідника причин діяла й
    на вже вивантажених книгах.
    """
    headers = ["Номер закупівлі", "Дата закупівлі", "Переможець", "ЄДРПОУ переможця",
               "Сума", "Валюта", "Статус рішення", "Дата рішення",
               "Причина відмови", "Категорія причини", "Пояснення відмови"]
    rows = []
    for r in db.query("""
            SELECT t.tender_id, t.date_created, a.* FROM awards a
            JOIN tenders t ON t.uuid = a.tender_uuid
            ORDER BY t.date_created DESC"""):
        rejected = rj.is_rejected(r["status"])
        reason = (r["reason"] or "") if rejected else ""
        explanation = (r["explanation"] or "") if rejected else ""
        rows.append([
            r["tender_id"], (r["date_created"] or "")[:10], r["supplier_name"],
            r["supplier_edrpou"], r["value_amount"], r["currency"], r["status"],
            (r["date"] or "")[:10],
            reason, rj.classify(reason, explanation) if rejected else "", explanation,
        ])
    return headers, rows


def cancellations(db: Database) -> Sheet:
    headers = ["Номер закупівлі", "Дата закупівлі", "Замовник", "ЄДРПОУ замовника",
               "Очікувана вартість", "Дата відміни", "Статус відміни",
               "Підстава відміни", "Код підстави", "Обґрунтування", "Лот"]
    rows = [[r["tender_id"], (r["date_created"] or "")[:10], r["pe_name"], r["pe_edrpou"],
             r["value_amount"], (r["date"] or "")[:10], r["status"],
             rj.cancel_label(r["reason_type"]), r["reason_type"], r["reason"], r["lot_id"]]
            for r in db.query("""
                SELECT t.tender_id, t.date_created, t.pe_name, t.pe_edrpou, t.value_amount, c.*
                FROM cancellations c
                JOIN tenders t ON t.uuid = c.tender_uuid
                ORDER BY t.date_created DESC""")]
    return headers, rows


def contracts(db: Database) -> Sheet:
    headers = ["Номер закупівлі", "Номер договору", "Постачальник", "ЄДРПОУ постачальника",
               "Сума договору", "Валюта", "Статус договору", "Дата підписання",
               "Замовник", "ЄДРПОУ замовника", "Регіон", "Коди ДК021"]
    rows = [[r["tender_id"], r["contract_id"], r["supplier_name"], r["supplier_edrpou"],
             r["value_amount"], r["currency"], r["status"], (r["date_signed"] or "")[:10],
             r["pe_name"], r["pe_edrpou"], r["pe_region"], r["cpv_list"]]
            for r in db.query("""
                SELECT t.tender_id, t.pe_name, t.pe_edrpou, t.pe_region, t.cpv_list, c.*
                FROM contracts c
                JOIN tenders t ON t.uuid = c.tender_uuid
                ORDER BY c.date_signed DESC""")]
    return headers, rows


def documents(db: Database) -> Sheet:
    headers = ["Номер закупівлі", "Розділ", "Чий файл", "ЄДРПОУ власника", "Назва файлу",
               "Тип документа", "Формат", "Дата оприлюднення", "Розмір, байт", "Стан",
               "Шлях на диску", "Посилання"]
    states = {"ok": "завантажено", "pending": "у черзі", "filtered": "відсіяно фільтром",
              "skipped": "пропущено", "error": "помилка"}
    rows = [[r["tender_id"], SCOPE_LABELS.get(r["scope"], r["scope"]), r["owner_name"],
             r["owner_edrpou"], r["title"], r["doc_type"], r["format"],
             (r["date_published"] or "")[:10], r["size"],
             states.get(r["state"], r["state"]), r["local_path"], r["url"]]
            for r in db.query(
                "SELECT * FROM documents ORDER BY tender_id DESC, scope, title")]
    return headers, rows


def products(db: Database) -> Sheet:
    headers = ["Товар", "Бренд", "Категорія", "Код ДК021", "Назва коду",
               "Штрихкод", "Тип коду", "Опис",
               "Ціна, нижній квартиль", "Ціна, верхній квартиль", "Валюта", "ПДВ включено",
               "Дата ціни", "Майданчик", "Постачальник", "Статус",
               "Фото, шт", "Характеристик, шт", "Довжина опису", "Посилання на фото",
               "Створено", "Змінено", "Діє до", "Посилання на картку"]
    rows = [[r["title"], r["brand"], r["category"], r["cpv"], r["cpv_name"],
             r["barcode"], r["barcode_scheme"], r["description"],
             r["price_low"], r["price_high"], r["price_currency"],
             "так" if r["price_vat"] else "ні", r["price_date"],
             r["marketplace"], r["vendor"], r["status"],
             r["n_images"], r["n_specs"], r["description_len"], r["images"],
             r["date_created"], r["date_modified"], r["expiration_date"], r["url"]]
            for r in db.query("SELECT * FROM products ORDER BY brand, title")]
    return headers, rows


def product_specs(db: Database) -> Sheet:
    headers = ["Товар", "Бренд", "Категорія", "Штрихкод",
               "Характеристика", "Значення", "Число", "Одиниця"]
    rows = [[r["title"], r["brand"], r["category"], r["barcode"],
             r["name"], r["value"], r["number"], r["unit"]]
            for r in db.query("""
                SELECT p.title, p.brand, p.category, p.barcode, s.*
                FROM product_specs s
                JOIN products p ON p.id = s.product_id
                ORDER BY p.brand, p.title, s.name""")]
    return headers, rows


#: Порядок аркушів у книзі.
BUILDERS = {
    "Закупівлі": tenders,
    "Лоти": lots,
    "Номенклатура": items,
    "Пропозиції": bids,
    "Переможці": awards,
    "Договори": contracts,
    "Відміни закупівель": cancellations,
    "Документи": documents,
    "Товари каталогу": products,
    "Характеристики товарів": product_specs,
}


def build_sheets(db: Database) -> dict[str, Sheet]:
    """Усі аркуші сирих даних. Порожні сутності пропускаються."""
    sheets: dict[str, Sheet] = {}
    contents: list[list[Any]] = []
    for name, builder in BUILDERS.items():
        headers, rows = builder(db)
        if not rows:
            continue
        sheets[name] = (headers, rows)
        contents.append([name, len(rows), SHEET_NOTES.get(name, "")])
    if contents:
        sheets = {"Зміст": (["Аркуш", "Рядків", "Що містить"], contents), **sheets}
    return sheets
