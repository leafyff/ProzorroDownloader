"""Швидкі перевірки ядра без звернень до мережі.

Запуск:  .venv\\Scripts\\python -m tests.test_core
"""
from __future__ import annotations

import io
import sys
import tempfile
from datetime import date
from pathlib import Path

if __package__ in (None, ""):                       # прямий запуск файлу
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import SearchPreset, Settings
from app.core.classifiers import expand_prefixes, label_for, significant_prefix
from app.core.db import Database
from app.core.downloader import (
    FileDownloader, document_extension, is_signature, normalize_extensions, tender_folder,
)
from app.core.extract import iter_documents, latest_versions, parse_tender
from app.core.market import class_codes, parse_product
from app.core.pipeline import Pipeline
from app.core.resolver import IndexBuilder, tender_id_of
from app.paths import safe_name

FAILED: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> None:
    print(f"  {'✓' if condition else '✗'} {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


def test_classifiers() -> None:
    print("=== класифікатор ДК021 ===")
    check("порожній список", expand_prefixes([]) == [])
    # 17 — єдиний двоцифровий розділ, якого в ДК021 немає.
    check("невідомий префікс", expand_prefixes(["17"]) == [])
    check("рідкісний код", expand_prefixes(["99999"]) == ["99999999-9"])
    check("сміття на вході", expand_prefixes(["", "  ", None]) == [])
    check("розділ 30 розгортається", len(expand_prefixes(["30"])) == 401)
    check("значущий префікс коду", significant_prefix("30213300-8") == "302133")
    check("значущий префікс розділу", significant_prefix("30000000-9") == "30")
    check("підпис відомої гілки", label_for("777").startswith("77700000-7 —"))
    check("підпис невідомої гілки", label_for("17") == "17")


def test_safe_names() -> None:
    print("\n=== безпечні імена файлів ===")
    check("заборонені символи", safe_name('a<b>c:d"e/f\\g|h?i*j') == "a_b_c_d_e_f_g_h_i_j")
    check("зарезервоване ім'я", safe_name("CON.txt") == "_CON.txt")
    check("порожнє ім'я", safe_name("") == "_")
    # Windows мовчки відкидає крапки в кінці імені — прибираємо їх самі, щоб
    # записаний у базі шлях збігався з реальним файлом на диску.
    check("крапки по краях", safe_name("  ...назва...  ") == "назва")
    long_name = safe_name("x" * 300 + ".pdf", max_len=40)
    check("обрізання зі збереженням розширення",
          len(long_name) <= 40 and long_name.endswith(".pdf"))


def test_extract() -> None:
    print("\n=== розбір картки закупівлі ===")
    check("порожня картка не падає", parse_tender({})["row"]["uuid"] == "")
    check("картка без документів", parse_tender({"id": "x"})["docs"] == [])

    tender = {
        "id": "t1", "tenderID": "UA-2026-01-01-000001-a",
        "documents": [
            {"url": "https://x/1", "title": "a.pdf", "datePublished": "2026-01-01", "id": "d1"},
            {"url": "https://x/2", "title": "a.pdf", "datePublished": "2026-01-02", "id": "d1"},
        ],
        "bids": [{
            "id": "b1", "tenderers": [{"name": "ТОВ", "identifier": {"id": "123"}}],
            "documents": [{"url": "https://x/3", "title": "b.pdf"}],
            "financialDocuments": [{"url": "https://x/4", "title": "c.pdf"}],
        }],
        "awards": [{
            "id": "a1", "suppliers": [{"name": "ТОВ", "identifier": {"id": "123"}}],
            "documents": [{"url": "https://x/5", "title": "d.pdf"}],
        }],
    }
    docs = list(iter_documents(tender))
    check("знайдено всі файли", len(docs) == 5, len(docs))
    check("області визначено",
          sorted({d["scope"] for d in docs}) == ["award", "bid", "tender"])
    check("власника пропозиції визначено",
          {d["owner_edrpou"] for d in docs if d["scope"] == "bid"} == {"123"})
    check("контейнери правильні",
          sorted({d["container"] for d in docs}) == [
              "awards[0].documents", "bids[0].documents",
              "bids[0].financialDocuments", "documents"])
    check("ключі документів унікальні", len({d[0] for d in parse_tender(tender)["docs"]}) == 5)
    # Дві версії одного doc_id у тому самому контейнері згортаються в останню.
    versions = latest_versions([dict(d) for d in docs])
    check("лишається остання версія", len(versions) == 4, len(versions))

    # У картці закупівлі постачальник указаний лише в рішенні про переможця,
    # а договір посилається на нього через awardID.
    with_contract = {
        "id": "t2", "tenderID": "UA-2026-01-01-000002-a",
        "awards": [{"id": "aw1", "status": "active",
                    "suppliers": [{"name": "ТОВ Постачальник",
                                   "identifier": {"id": "12345678"}}]}],
        "contracts": [{"id": "c1", "contractID": "UA-2026-01-01-000002-a-a1",
                       "awardID": "aw1", "status": "active",
                       "value": {"amount": 1000, "currency": "UAH"}}],
    }
    parsed = parse_tender(with_contract)
    contract = parsed["contracts"][0]
    check("ЄДРПОУ постачальника підтягнуто з нагороди", contract[8] == "12345678", contract[8])
    check("назву постачальника теж підтягнуто", contract[7] == "ТОВ Постачальник")
    # Якщо постачальник таки вказаний у самому договорі — беремо саме його.
    with_contract["contracts"][0]["suppliers"] = [
        {"name": "ТОВ Інший", "identifier": {"id": "87654321"}}]
    check("власний постачальник договору має пріоритет",
          parse_tender(with_contract)["contracts"][0][8] == "87654321")


def test_index_chunks() -> None:
    print("\n=== межі відрізків індексу ===")
    chunks = IndexBuilder.month_chunks(date(2025, 1, 15), date(2025, 3, 10))
    check("три місячні відрізки", len(chunks) == 3)
    check("початок не раніше заданого", chunks[0][0] == date(2025, 1, 15))
    check("кінець не пізніше заданого", chunks[-1][1] == date(2025, 3, 10))
    check("один день", IndexBuilder.month_chunks(date(2025, 5, 5), date(2025, 5, 5))
          == [(date(2025, 5, 5), date(2025, 5, 5))])
    check("номер закупівлі з номера договору",
          tender_id_of("UA-2026-08-13-007779-a-a1") == "UA-2026-08-13-007779-a")
    check("номер без суфікса не змінюється",
          tender_id_of("UA-2026-08-13-007779-a") == "UA-2026-08-13-007779-a")


def test_market() -> None:
    print("\n=== картки товарів е-каталогу ===")
    # Каталог класифікує товари на рівні класу, закупівлі — на рівні коду.
    check("коди підіймаються до класу",
          class_codes(["30213300-8", "30213100-6"]) == ["30210000-4"],
          class_codes(["30213300-8", "30213100-6"]))
    check("порожній перелік", class_codes([]) == [])
    check("сміття не ламає", class_codes(["", "12", None]) == [])

    card = {
        "id": "p1", "status": "active",
        "title": "Ноутбук Acer TravelMate TMP215-55",
        "description": "Ноутбук 15.6\" FHD IPS",
        "categoryTitle": "Ноутбуки",
        "owner": "uss.gov.ua",
        "images": ["https://x/1.png", "https://x/2.png"],
        "identifier": {"id": "4711474580108", "scheme": "EAN-13"},
        "classification": {"scheme": "ДК021",
                           "description": "ДК 021:2015: 30210000-4 — Машини для обробки даних"},
        "latestPrice": {"lowerQuartile": 38235, "upperQuartile": 42900,
                        "currency": "UAH", "valueAddedTaxIncluded": False,
                        "date": "2026-02-10T10:00:04+02:00"},
        "requirementResponses": [
            {"requirement": "Бренд", "values": ["ACER"], "unitName": ""},
            {"requirement": "Діагональ екрану", "values": [15.6], "unitName": "дюйм"},
            {"requirement": "Мова розкладки", "values": ["англійська", "українська"],
             "unitName": ""},
            {"requirement": "Сенсорний екран", "values": [False], "unitName": ""},
        ],
    }
    row, specs = parse_product(card)
    check("бренд витягнуто з характеристик", row["brand"] == "ACER", row["brand"])
    check("штрихкод", row["barcode"] == "4711474580108")
    check("код ДК021 з опису класифікації", row["cpv"] == "30210000-4", row["cpv"])
    check("ціновий діапазон", (row["price_low"], row["price_high"]) == (38235.0, 42900.0))
    check("фото полічені", row["n_images"] == 2)
    check("характеристики полічені", row["n_specs"] == 4)
    check("довжина опису", row["description_len"] == len(card["description"]))
    check("характеристик у довгому форматі", len(specs) == 4)
    numeric = next(s for s in specs if s[1] == "Діагональ екрану")
    check("числове значення окремо", numeric[3] == 15.6 and numeric[4] == "дюйм")
    multi = next(s for s in specs if s[1] == "Мова розкладки")
    check("кілька значень через кому", multi[2] == "англійська, українська", multi[2])
    check("логічне значення як текст",
          next(s for s in specs if s[1] == "Сенсорний екран")[2] == "False")
    # Стисла картка з пошуку доповнює детальну, якщо в тій чогось бракує.
    row2, _ = parse_product({"id": "p2"}, {"title": "З пошуку", "status": "inactive"})
    check("дані з пошуку підставляються",
          row2["title"] == "З пошуку" and row2["status"] == "inactive")


def test_paths(tmp: Path) -> None:
    print("\n=== розкладання файлів по теках ===")
    db = Database(tmp / "test.db")
    root = tmp / "out"
    dl = FileDownloader(None, db, root)
    folder = tender_folder(root, "UA-2026-01-01-000001-a", "Ноутбуки/партія №1", "2026-01-15")
    check("тека за місяцем", folder.parent.name == "2026-01")
    check("слеш у назві прибрано", "/" not in folder.name)

    doc = {"scope": "bid", "owner_name": "ТОВ Х", "owner_edrpou": "111", "title": "акт.pdf"}
    first = dl.target_path(dict(doc), folder)
    second = dl.target_path(dict(doc), folder)
    check("однакові назви розведено", first != second, f"{first.name} / {second.name}")
    check("ЄДРПОУ у назві теки учасника", "111" in first.parent.name, first.parent.name)

    recorded = {"scope": "bid", "title": "акт.pdf",
                "local_path": str(folder / "Пропозиція" / "старе.pdf")}
    check("збережений шлях перевикористано",
          dl.target_path(recorded, folder).name == "старе.pdf")
    foreign = {"scope": "bid", "title": "акт.pdf", "local_path": r"D:\інша\тека\файл.pdf"}
    check("шлях поза текою завантажень ігнорується",
          dl.target_path(foreign, folder).name != "файл.pdf")
    check("розширення з MIME",
          dl._file_name({"title": "документ", "format": "application/pdf"}) == "документ.pdf")
    db.close()


def test_file_filter() -> None:
    print("\n=== фільтр типів файлів ===")
    check("підпис за MIME", is_signature({"title": "sign", "format": "application/pkcs7-signature"}))
    check("підпис за назвою", is_signature({"title": "sign.p7s", "format": ""}))
    check("підпис у назві документа", is_signature({"title": "Витяг_МВС.p7s"}))
    check("PDF не підпис", not is_signature({"title": "договір.pdf", "format": "application/pdf"}))
    check("архів не підпис", not is_signature({"title": "пропозиція.zip"}))
    check("розширення з назви", document_extension({"title": "Договір №5.PDF"}) == ".pdf")
    check("розширення з MIME", document_extension(
        {"title": "Договір", "format": "application/pdf"}) == ".pdf")
    check("без розширення", document_extension({"title": "Договір", "format": ""}) == "")
    check("нормалізація переліку",
          normalize_extensions(["PDF", ".docx", " xls ", "*.rtf", ""])
          == {".pdf", ".docx", ".xls", ".rtf"})
    check("порожній перелік", normalize_extensions(None) == set())


def test_widgets() -> None:
    print("\n=== поля вводу ===")
    from PySide6.QtCore import QDate
    from PySide6.QtGui import QValidator
    from PySide6.QtWidgets import QApplication

    from app.ui.widgets.common import EDRPOU_RE, DateRange, EdrpouList, MoneyEdit

    app = QApplication.instance() or QApplication([])

    money = MoneyEdit("від")
    check("порожнє поле — без обмеження", money.value() is None)
    money.setText("1 234 567")
    check("сума з пробілами", money.value() == 1234567.0, money.value())
    money.setText("50000,00")
    check("сума з комою", money.value() == 5000000.0, money.value())
    money.set_value(1234567)
    formatted = money.text()
    digits = "".join(ch for ch in formatted if ch.isdigit())
    check("форматування з групами",
          digits == "1234567" and "," not in formatted and len(formatted) == 9,
          repr(formatted))
    check("сформатоване читається назад", money.value() == 1234567.0)
    # Головне, заради чого поле переписане: після форматування текст мусить
    # лишатися прийнятним для валідатора, інакше ввід блокується.
    state, _, _ = money.validator().validate(formatted, len(formatted))
    check("сформатований текст лишається валідним",
          state == QValidator.State.Acceptable, state.name)
    for typed in ("5", "50000", "1 000", "1"):
        state, _, _ = money.validator().validate(typed, len(typed))
        check(f"ввід {typed!r} приймається", state == QValidator.State.Acceptable, state.name)
    money.set_value(None)
    check("скидання у порожнє", money.text() == "")

    check("ЄДРПОУ юрособи", EDRPOU_RE.findall("код 41263186 ТОВ") == ["41263186"])
    check("РНОКПП ФОП", EDRPOU_RE.findall("2381311679") == ["2381311679"])
    check("кілька кодів", EDRPOU_RE.findall("41263186, 44996712") == ["41263186", "44996712"])
    check("довгий номер не ріжеться", EDRPOU_RE.findall("123456789012345678") == [])
    check("короткий номер ігнорується", EDRPOU_RE.findall("12345") == [])

    codes = EdrpouList()
    codes.input.setText("41263186 та 44996712")
    codes._add()
    check("додано обидва коди", codes.values() == ["41263186", "44996712"], codes.values())
    codes.input.setText("ТОВ Ромашка")
    codes._add()
    # isVisible() у неприкріпленого віджета завжди False, тому дивимось на
    # намір показати підказку та на те, що ввід лишився на місці.
    check("непридатний ввід не з'їдається мовчки",
          codes.hint.isVisibleTo(codes) and codes.input.text() == "ТОВ Ромашка")
    codes.input.setText("41263186")
    codes._add()
    check("дублікат не додається двічі", codes.values() == ["41263186", "44996712"])
    check("підказка зникає після вдалого вводу", not codes.hint.isVisibleTo(codes))

    period = DateRange()
    period.set_values("2026-05-01", "2026-01-01")
    period.normalize()
    check("переставлені дати виправлено", period.values() == ("2026-01-01", "2026-05-01"),
          period.values())
    period.set_values("не дата", "")
    check("сміття замість дати не ламає поле",
          period.date_from.date().isValid() and period.date_to.date() == QDate.currentDate())
    app.processEvents()


def test_download_missing(tmp: Path) -> None:
    print("\n=== довантаження відсутніх файлів ===")
    db = Database(tmp / "retry.db")
    settings = Settings()
    settings.output_dir = str(tmp / "out")

    result = Pipeline(settings, SearchPreset(), db).download_missing()
    check("порожня база не падає", result.files_ok == 0 and not result.error, result.error)

    # Документи, зафіксовані під час збору без файлів, мають потрапляти в чергу.
    tender = {"id": "t3", "tenderID": "UA-2026-01-01-000003-a",
              "documents": [
                  {"id": "d1", "url": "https://x/1", "title": "умови.pdf",
                   "format": "application/pdf"},
                  {"id": "d2", "url": "https://x/2", "title": "sign.p7s",
                   "format": "application/pkcs7-signature"}]}
    parsed = parse_tender(tender)
    db.save_tender(parsed["row"], lots=[], items=[], bids=[], awards=[],
                   contracts=[], docs=parsed["docs"])
    queued = db.query("SELECT state, COUNT(*) n FROM documents GROUP BY state")
    check("документи лягли в чергу", [dict(r) for r in queued] == [{"state": "pending", "n": 2}],
          [dict(r) for r in queued])

    preset = SearchPreset()
    preset.skip_signatures = True
    pipeline = Pipeline(settings, preset, db)
    # Мережу не чіпаємо: перевіряємо лише те, що з черги береться в роботу.
    selected = pipeline._apply_file_filter(
        [dict(r) for r in db.pending_documents()])
    check("підпис відсіяно, документ лишився",
          [d["title"] for d in selected] == ["умови.pdf"], [d["title"] for d in selected])
    states = {r["state"]: r["n"] for r in
              db.query("SELECT state, COUNT(*) n FROM documents GROUP BY state")}
    check("підпис позначено відсіяним", states.get("filtered") == 1, states)

    # Відсіяне має повертатися в чергу, якщо фільтр вимкнули.
    preset.skip_signatures = False
    again = Pipeline(settings, preset, db)._apply_file_filter(
        [dict(r) for r in db.pending_documents()])
    check("після вимкнення фільтра підпис знову в черзі", len(again) == 2, len(again))
    db.close()


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    with tempfile.TemporaryDirectory(prefix="prozorro-test-") as raw:
        tmp = Path(raw)
        test_classifiers()
        test_safe_names()
        test_extract()
        test_index_chunks()
        test_file_filter()
        test_market()
        test_paths(tmp)
        test_download_missing(tmp)
        test_widgets()
    if FAILED:
        print(f"\nНЕ ПРОЙШЛО {len(FAILED)}: {', '.join(FAILED)}")
        return 1
    print("\nУСЕ ГАРАЗД")
    return 0


if __name__ == "__main__":
    sys.exit(main())
