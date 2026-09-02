"""Рушій аналітики: з книги вивантаження — у готовий звіт.

Порядок роботи повторює порядок, у якому ринок узагалі можна зрозуміти:

1. **Очищення.** Спершу перевіряємо, чи можна вірити числам: валюта, нулі,
   дублікати, неможливі співвідношення «договір/очікувана вартість» і
   статистичні викиди за стійким z-показником. Тільки після цього рахуємо суми.
2. **Ринок.** Скільки грошей, хто їх бере, наскільки ринок концентрований,
   як він рухається по місяцях і де в цьому всьому наші ТОВ.
3. **Конкуренти.** Портрет кожного помітного гравця: товар, ТМ, процедури,
   цінова поведінка, географія, замовники — і чому саме ми програли або
   не виходили на його закупівлі.
4. **Постачання.** Які сигнали про канал товару взагалі є у відкритих даних:
   ексклюзивність ТМ, авторизаційні листи, картки е-каталогу.
5. **Порівняння.** Сильні й слабкі сторони наших ТОВ проти конкурентів за
   вимірюваними ознаками — з чесною позначкою там, де цифр уже не досить.

Усе рахується локально й без мережі: на вході — лише книга Excel.
"""
from __future__ import annotations

import math
import re
import threading
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Iterable, Sequence

from ..config import KNOWN_COMPANIES
from . import brands as tm
from .classifiers import by_prefix, cpv_name
from .products import product_group
from .benchmark import BenchmarkMixin
from .forecast import DEFAULT_HORIZON
from .outlook import OutlookMixin
from .players import PlayersMixin
from .rejections import RejectionMixin
from .report import (
    Block, ChartData, Report, Series, Sheet,
    compact, count, gini, hhi, hhi_verdict, median, median_of, money, pct,
    quantile_of, robust_z, share,
)
from .xlsxload import Dataset


class Cancelled(Exception):
    """Аналіз зупинено на вимогу користувача."""


#: Договори в цих станах вважаємо реальними грошима ринку.
LIVE_CONTRACTS = ("active", "terminated")
#: Рішення про переможця, яке ще чинне.
LIVE_AWARDS = ("active",)

#: Скільки конкурентів розбираємо поіменно.
TOP_COMPETITORS = 20
#: Скільки позицій показуємо у рейтингах і колових діаграмах.
TOP_ROWS = 15
#: Скільки кроків має повний аналіз. Смуга поступу на сторінці аналітики
#: склеює його з читанням книги, тож число має бути одне на обидва боки —
#: інакше відсоток стрибає, щойно з'явиться новий крок або новий аркуш.
ANALYSIS_STEPS = 10

#: Поріг стійкого z-показника, за яким значення вважається викидом.
Z_OUTLIER = 3.5
#: Менші групи для порівняння сум занадто дрібні — беремо загальний розподіл.
MIN_GROUP = 8
#: Скільки звичайних точок лишаємо на хмарі «ціна × кількість».
SCATTER_POINTS = 4000
#: Межі, у яких сума договорів закупівлі вважається підтвердженою її
#: очікуваною вартістю. Поза ними число суперечить саме собі: або замовник
#: заплатив утричі більше, ніж планував, або договір — на копійки від плану.
RATIO_HIGH = 3.0
RATIO_LOW = 0.05

_SPLIT = re.compile(r"[\s,;:/()\[\]«»\"]+")
_CYR_WORD = re.compile(r"^[А-Яа-яЇїІіЄєҐґ'’\-]+$")

#: Організаційно-правова форма: як її пише реєстр → як її пишуть люди.
#: Довші форми стоять першими, інакше «АКЦІОНЕРНЕ ТОВАРИСТВО» відкусило б
#: хвіст у «ПРИВАТНОГО АКЦІОНЕРНОГО ТОВАРИСТВА», а «ТОВ» — початок
#: «ТОВАРИСТВА».
ORG_FORMS: tuple[tuple[str, str], ...] = (
    ("ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ", "ТОВ"),
    ("ТОВАРИСТВО З ДОДАТКОВОЮ ВІДПОВІДАЛЬНІСТЮ", "ТДВ"),
    ("КОМУНАЛЬНЕ НЕКОМЕРЦІЙНЕ ПІДПРИЄМСТВО", "КНП"),
    ("ПРИВАТНЕ АКЦІОНЕРНЕ ТОВАРИСТВО", "ПрАТ"),
    ("ПУБЛІЧНЕ АКЦІОНЕРНЕ ТОВАРИСТВО", "ПАТ"),
    ("КОМУНАЛЬНЕ ПІДПРИЄМСТВО", "КП"),
    ("ДЕРЖАВНЕ ПІДПРИЄМСТВО", "ДП"),
    ("ПРИВАТНЕ ПІДПРИЄМСТВО", "ПП"),
    ("АКЦІОНЕРНЕ ТОВАРИСТВО", "АТ"),
    ("ФІЗИЧНА ОСОБА-ПІДПРИЄМЕЦЬ", "ФОП"),
)

#: Пробіли й дефіси в реєстрі гуляють: «ФІЗИЧНА ОСОБА-ПІДПРИЄМЕЦЬ»,
#: «ФІЗИЧНА ОСОБА – ПІДПРИЄМЕЦЬ» і «ФІЗИЧНА  ОСОБА ПІДПРИЄМЕЦЬ» — одна форма.
_GAP = r"[\s\-–—]+"

#: М'який перенос і пробіли нульової ширини. У реєстрі вони трапляються
#: посеред слова («ВІДПОВІДА» + м'який перенос + «ЛЬНІСТЮ») і мовчки ламають
#: будь-який збіг: очима назва звичайна, а для пошуку це вже інше слово.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200d\ufeff]")


def _form_pattern(full: str) -> str:
    """Регулярка однієї форми — з поправкою на те, як її пишуть насправді.

    Між словами допускаємо будь-які пробіли й дефіси, а іменник і прикметник
    беремо в обох відмінках: у назвах філій форма стоїть у родовому —
    «філія "Енергоремтранс" ПУБЛІЧНОГО АКЦІОНЕРНОГО ТОВАРИСТВА "Укрзалізниця"».
    """
    words = []
    for word in re.split(r"[\s\-]+", full):
        if word.endswith("О"):        # ТОВАРИСТВО → ТОВАРИСТВА
            words.append(re.escape(word[:-1]) + "[ОА]")
        elif word.endswith("Е"):      # АКЦІОНЕРНЕ → АКЦІОНЕРНОГО
            words.append(re.escape(word[:-1]) + "(?:Е|ОГО)")
        else:
            words.append(re.escape(word))
    return _GAP.join(words)


#: Повна форма будь-де в назві — саме її ми й скорочуємо. Кожен варіант сидить
#: у власній іменованій групі: за нею одразу видно, на що міняти збіг.
_FULL_FORM = re.compile(
    "|".join(rf"(?P<f{n}>\b{_form_pattern(full)}\b)"
             for n, (full, _short) in enumerate(ORG_FORMS)),
    re.IGNORECASE)
_SHORT_OF = {f"f{n}": short for n, (_full, short) in enumerate(ORG_FORMS)}

#: Форма на початку назви — у підписах графіків її прибирають зовсім.
#: Спершу повні форми, далі абревіатури від довших до коротших: «ПрАТ» має
#: збігтися раніше, ніж «АТ».
ORG_FORM = re.compile(
    "^(?:" + "|".join([*(_form_pattern(full) for full, _short in ORG_FORMS),
                       *sorted((short for _full, short in ORG_FORMS),
                               key=len, reverse=True),
                       "ФІЗИЧНА" + _GAP + "ОСОБА"])
    + r")\b[\s.'\"«»-]*", re.IGNORECASE)

#: Де в книзі лежать назви компаній: логічна таблиця → її колонки. Лише сюди
#: назва приходить із реєстру, далі вона тільки переписується з рядка в рядок.
NAME_COLUMNS: dict[str, tuple[str, ...]] = {
    "tenders": ("buyer",),
    "items": ("buyer",),
    "bids": ("name",),
    "awards": ("name",),
    "contracts": ("name", "buyer"),
    "products": ("vendor",),
}


def short_org(name: str) -> str:
    """«ТОВАРИСТВО З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ "КОМЕЛ"» → «ТОВ "КОМЕЛ"».

    Форму власності лишаємо — вона відрізняє ТОВ від ФОП і державного
    підприємства, — але пишемо її так, як пишуть люди: у таблиці звіту повна
    назва з'їдає колонку, нічого не додаючи, бо однакова майже в усіх.
    Решту назви не чіпаємо: скорочення має бути передбачуваним.
    """
    text = _INVISIBLE.sub("", str(name or "")).strip()
    if not text:
        return ""
    return _FULL_FORM.sub(lambda m: _SHORT_OF.get(m.lastgroup or "", m.group(0)), text)


@lru_cache(maxsize=4096)
def cpv_group(code: str) -> tuple[str, str]:
    """``30213300-8`` → ``('30210000-4', 'Персональні комп'ютери')``.

    Кодів у вибірці небагато, а звертань — по одному на кожну угоду й позицію,
    тож результат кешується.
    """
    digits = "".join(ch for ch in str(code or "").split("-")[0] if ch.isdigit())
    if len(digits) < 2:
        return "", ""
    key = digits[:4].rstrip("0") or digits[:2]
    found = by_prefix().get(key) or by_prefix().get(digits[:2])
    return found if found else (digits[:4] + "0000", "")


def product_type(text: str) -> str:
    """Родова назва товару з опису позиції.

    Опис позиції — це «Ноутбук Vinga Iron S140 15.6"»: спочатку тип товару
    кирилицею, далі модель латиницею та цифрами. Тому беремо слова до першого
    латинського чи цифрового токена — цього досить, щоб згрупувати позиції
    у «Ноутбуки», «Монітори», «Багатофункціональні пристрої».
    """
    words: list[str] = []
    for token in _SPLIT.split(str(text or "").strip()):
        if not token:
            continue
        if not _CYR_WORD.match(token):
            break
        words.append(token)
        if len(words) >= 3:
            break
    name = " ".join(words).strip(" -–—")
    if not name:
        name = str(text or "")[:40]
    return name[:1].upper() + name[1:].lower() if name else ""


class Analyzer(PlayersMixin, BenchmarkMixin, OutlookMixin, RejectionMixin):
    """Один прохід аналізу над однією книгою."""

    def __init__(self, data: Dataset, own_edrpou: Iterable[str] = (),
                 tracked: Iterable[str] = (), drop_outliers: bool = True,
                 top_competitors: int = TOP_COMPETITORS,
                 on_progress: Callable[[str, int, int], None] | None = None,
                 cancel_event: "threading.Event | None" = None,
                 horizon: int = DEFAULT_HORIZON):
        self.data = data
        self.cancel_event = cancel_event
        self.horizon = max(1, int(horizon))
        self.own = {str(code).strip() for code in own_edrpou if str(code).strip()}
        self.tracked = {str(code).strip() for code in tracked if str(code).strip()}
        self.drop_outliers = drop_outliers
        self._top_competitors = max(1, int(top_competitors))
        self.on_progress = on_progress
        self.report = Report(source=str(data.path),
                             generated=datetime.now().strftime("%d.%m.%Y %H:%M"))
        self._brand_cache: dict[str, list[str]] = {}
        self._type_cache: dict[str, str] = {}
        self._group_cache: dict[str, str] = {}
        self._loss_cache: dict[str, Sheet] = {}
        self.market_discount: float | None = None

    # --- службове ---------------------------------------------------------

    def _step(self, text: str, done: int, total: int = ANALYSIS_STEPS) -> None:
        """Повідомляє про поступ і перевіряє, чи не просили зупинитись.

        Перевірка стоїть саме тут, між етапами: аналіз довгий, і без неї
        закриття вікна чекало б на його завершення.
        """
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled("аналіз зупинено")
        if self.on_progress:
            self.on_progress(text, done, total)

    def _brands_of(self, text: str) -> list[str]:
        """Розпізнані ТМ із кешем: описи позицій масово повторюються.

        Текст беремо цілком, без обрізання. В ІТ-закупівлях опис позиції — це
        сторінка технічних вимог, і виробник у ній згадується наприкінці
        («…гарантія 36 місяців, виробник Lenovo»). Обрізання опису рівно там
        і ховало б ТМ, заради якої весь розбір і робиться.
        """
        key = text if isinstance(text, str) else str(text or "")
        found = self._brand_cache.get(key)
        if found is None:
            found = tm.detect(key)
            self._brand_cache[key] = found
        return found

    def _product_type(self, text: str) -> str:
        """Назва товару так, як її написали в документі (без об'єднання)."""
        key = str(text or "")[:120]
        kind = self._type_cache.get(key)
        if kind is None:
            kind = product_type(key)
            self._type_cache[key] = kind
        return kind

    def _product_group(self, text: str) -> str:
        """Родовий тип товару: «Миші», «Мишка» й «Миша дротова» — це «Миша».

        Об'єднанням відає :mod:`app.core.products`; тут лише кеш, бо той самий
        опис трапляється в десятках позицій.

        Коли й довідник, і морфологія мовчать (опис без жодного кириличного
        слова — «Post пост карта»), лишається дослівна назва. Так об'єднана
        таблиця вміщає **рівно ті самі** позиції, що й таблиця «як у
        документах»: об'єднання не має нічого втрачати дорогою.
        """
        key = str(text or "")[:200]
        kind = self._group_cache.get(key)
        if kind is None:
            kind = product_group(key) or self._product_type(key)
            self._group_cache[key] = kind
        return kind

    def _group_of(self, code: str) -> str:
        return cpv_group(code)[0]

    def _group_pair(self, code: str) -> tuple[str, str]:
        return cpv_group(code)

    def is_ours(self, edrpou: str) -> bool:
        return edrpou in self.own

    def _visible_ranking(self, ranking: Sequence[tuple[str, Any]], limit: int
                         ) -> tuple[list[tuple[str, Any]], set[int]]:
        """Видима частина рейтингу, у якій наші ТОВ є завжди.

        Повертає відрізок рейтингу й номери позицій, які треба підсвітити.
        Наше ТОВ, що не влізло в топ-N, дописується в хвіст — на позицію
        N+1 і далі. Без цього графік мовчки ховав би саме ту компанію,
        заради якої аналітику й відкривають: варто нашому ЄДРПОУ опинитися
        нижчим за межу показу, і на «Топі постачальників» від нього не
        лишалося ані смуги, ані підсвітки — а порожнє місце на графіку
        читається як «нас на цьому ринку немає».

        Порядок хвоста — той самий, що й у рейтингу, тож наші ТОВ між собою
        лишаються за спаданням суми.
        """
        line = list(ranking[:limit])
        line += [row for row in ranking[limit:] if self.is_ours(row[0])]
        return line, {i for i, row in enumerate(line) if self.is_ours(row[0])}

    def _rank_label(self, edrpou: str, limit: int) -> str:
        """Підпис смуги: назва, а для дописаних у хвіст — ще й номер місця.

        Смуга, що стоїть 16-ю в «топ-15», інакше читалася б як 16-те місце
        ринку. Номер знімає це питання просто на графіку, не змушуючи шукати
        його в таблиці.
        """
        rank = self.rank_of.get(edrpou, 0)
        if rank <= limit:
            return self._short_name(edrpou)
        return f"{self._short_name(edrpou, 27)} (№{rank})"

    # --- 0. підготовка ----------------------------------------------------

    def _shorten_org_forms(self) -> None:
        """Скорочує організаційно-правову форму в назвах компаній.

        Робимо це один раз тут, на вході: далі назва розходиться по таблицях,
        плитках, підписах графіків і згадках у тексті, і жодне з тих місць не
        має знати, як компанія записана в реєстрі. Заразом однаково
        записуються ті самі замовники: у книзі один і той самий заклад буває
        і «ТОВАРИСТВОМ З ОБМЕЖЕНОЮ ВІДПОВІДАЛЬНІСТЮ», і просто «ТОВ».
        """
        short: dict[str, str] = {}
        for table, columns in NAME_COLUMNS.items():
            for row in self.data.table(table):
                for column in columns:
                    name = row.get(column)
                    if not name:
                        continue
                    fixed = short.get(name)
                    if fixed is None:
                        fixed = short[name] = short_org(name)
                    if fixed != name:
                        row[column] = fixed

    def prepare(self) -> None:
        data = self.data
        self._shorten_org_forms()
        self.tenders = {row["tender_id"]: row for row in data.tenders if row["tender_id"]}

        self.items_by_tender: dict[str, list[dict]] = defaultdict(list)
        for row in data.items:
            if row["tender_id"]:
                self.items_by_tender[row["tender_id"]].append(row)

        self.bids_by_tender: dict[str, list[dict]] = defaultdict(list)
        for row in data.bids:
            if row["tender_id"] and row["edrpou"]:
                self.bids_by_tender[row["tender_id"]].append(row)

        self.docs_by_tender: dict[str, list[dict]] = defaultdict(list)
        for row in data.documents:
            if row["tender_id"]:
                self.docs_by_tender[row["tender_id"]].append(row)

        # Назви компаній збираємо з усіх джерел: у договорах вони найповніші,
        # але учасник, який ніколи не вигравав, є лише у пропозиціях.
        self.names: dict[str, str] = {}
        for rows in (data.bids, data.awards, data.contracts):
            for row in rows:
                code, name = row.get("edrpou"), row.get("name")
                if code and name and len(name) > len(self.names.get(code, "")):
                    self.names[code] = name
        for row in data.tenders:
            if row["buyer_edrpou"] and row["buyer"]:
                self.names.setdefault(row["buyer_edrpou"], row["buyer"])
        # Відомі нам компанії — на випадок, якщо у цій вибірці їх немає:
        # краще показати «КОМЕЛ», ніж німий код.
        for code, name in KNOWN_COMPANIES.items():
            self.names.setdefault(code, name)

        # Основний код ДК021 закупівлі: найчастіший серед її позицій, інакше —
        # перший зі списку кодів картки. Так гроші не подвоюються по галузях.
        self.cpv_of: dict[str, str] = {}
        for tender_id, items in self.items_by_tender.items():
            codes = Counter(row["cpv"] for row in items if row["cpv"])
            if codes:
                self.cpv_of[tender_id] = codes.most_common(1)[0][0]
        for tender_id, tender in self.tenders.items():
            if tender_id not in self.cpv_of and tender["cpv_list"]:
                self.cpv_of[tender_id] = tender["cpv_list"].split(",")[0].strip()

        # ТМ закупівлі — за описами її позицій, а якщо позицій немає, то за
        # назвою предмета: у звітах про договір номенклатури часто бракує.
        self.brands_of: dict[str, list[str]] = {}
        for tender_id, tender in self.tenders.items():
            counter: Counter[str] = Counter()
            for item in self.items_by_tender.get(tender_id, ()):
                counter.update(self._brands_of(item["description"]))
            if not counter:
                counter.update(self._brands_of(tender["title"]))
                counter.update(self._brands_of(tender["description"]))
            self.brands_of[tender_id] = [name for name, _n in counter.most_common()]

        self.deals = self._deals()
        if data.missing:
            self.report.notes.append(
                "У книзі немає аркушів: " + ", ".join(data.missing) +
                ". Відповідні зрізи аналізу будуть неповними.")
        if not data.items:
            self.report.notes.append(
                "Аркуша «Номенклатура» немає — торгові марки й товари визначаються "
                "лише за назвою предмета закупівлі, тож охоплення буде вужчим. "
                "Зробіть нове вивантаження, щоб отримати позиції.")
        self._step("Дані підготовлено", 1)

    def _deals(self) -> list[dict]:
        """Угоди ринку: договір там, де він є, інакше — рішення про переможця.

        Брати і те, й інше не можна — сума подвоїться. Але й обмежитись
        договорами теж не можна: за частиною закупівель договір ще не
        оприлюднений, хоча переможець уже відомий, і без них ринок виглядав би
        меншим, ніж він є.
        """
        deals: list[dict] = []
        seen: set[str] = set()

        def add(row: dict, source: str, amount, date: str, contract_id: str = "") -> None:
            tender_id = row["tender_id"]
            tender = self.tenders.get(tender_id, {})
            code = self.cpv_of.get(tender_id, "")
            group, group_name = cpv_group(code)
            deals.append({
                "tender_id": tender_id,
                "contract_id": contract_id,
                "edrpou": row["edrpou"],
                "name": row["name"] or self.names.get(row["edrpou"], ""),
                "amount": float(amount or 0),
                "currency": row.get("currency") or "UAH",
                "status": row.get("status") or "",
                "date": date or tender.get("date", ""),
                "month": month_of(date or tender.get("date", "")),
                "buyer": tender.get("buyer") or row.get("buyer") or "",
                "buyer_edrpou": tender.get("buyer_edrpou") or row.get("buyer_edrpou") or "",
                "region": tender.get("region") or row.get("region") or "",
                "cpv": code,
                "group": group,
                "group_name": group_name or cpv_name(code),
                "method": tender.get("method") or "",
                "expected": tender.get("value") or 0.0,
                "source": source,
                "ours": row["edrpou"] in self.own,
            })

        for row in self.data.contracts:
            if not row["edrpou"] or row["status"] not in LIVE_CONTRACTS:
                continue
            add(row, "договір", row["amount"], row["signed"], row["contract_id"])
            seen.add(row["tender_id"])
        for row in self.data.awards:
            if not row["edrpou"] or row["tender_id"] in seen:
                continue
            if row["status"] not in LIVE_AWARDS:
                continue
            add(row, "рішення про переможця", row["amount"], row["decided"])
        return deals

    # --- 1. очищення ------------------------------------------------------

    def clean(self) -> None:
        issues: list[list[Any]] = []
        dropped: set[int] = set()
        reasons: Counter[str] = Counter()

        def flag(index: int | None, table: str, key: str, reason: str, detail: str,
                 drop: bool, sink: set[int] | None = None) -> None:
            """Записує зауваження і, якщо треба, вилучає рядок.

            ``sink`` — набір номерів рядків саме тієї таблиці, з якої прийшов
            рядок: номер позиції номенклатури не має жодного стосунку до
            нумерації угод, і сплутати їх означало б викидати з підсумків
            випадкові договори.
            """
            issues.append([table, key, reason, detail, "виключено" if drop else "залишено"])
            reasons[reason] += 1
            if drop and index is not None and sink is not None:
                sink.add(index)

        # --- структурні проблеми: такі рядки псують будь-яку суму ---------
        seen_keys: set[tuple[str, str]] = set()
        for i, deal in enumerate(self.deals):
            key = f"{deal['tender_id']} {deal['contract_id']}".strip()
            if deal["currency"] and deal["currency"] != "UAH":
                flag(i, "Угоди", key, "Валюта не гривня",
                     f"{deal['currency']}: перерахунок неможливий без курсу", True, dropped)
                continue
            if deal["amount"] <= 0:
                flag(i, "Угоди", key, "Нульова або від'ємна сума",
                     f"{money(deal['amount'])} грн", True, dropped)
                continue
            identity = (deal["tender_id"], deal["contract_id"] or deal["edrpou"])
            if identity in seen_keys:
                flag(i, "Угоди", key, "Дублікат рядка",
                     "такий самий договір уже врахований", True, dropped)
                continue
            seen_keys.add(identity)

        # --- звірка з очікуваною вартістю ---------------------------------
        # Порівнюємо не окремий договір, а суму всіх договорів закупівлі: у
        # багатолотовій закупівлі кожен лот — це частина очікуваної вартості,
        # і поодинці вони «мізерні» лише на вигляд.
        signed_by_tender: dict[str, float] = defaultdict(float)
        for i, deal in enumerate(self.deals):
            if i not in dropped:
                signed_by_tender[deal["tender_id"]] += deal["amount"]

        #: Угоди, чию суму підтверджує очікувана вартість закупівлі. Це
        #: незалежне число з іншої частини картки, тож статистиці тут уже
        #: нічого перевіряти: великий договір, який збігається з очікуваною
        #: вартістю, — не помилка даних, а велика закупівля.
        confirmed: set[int] = set()
        for i, deal in enumerate(self.deals):
            if i in dropped:
                continue
            expected = deal["expected"] or 0
            if expected <= 0:
                continue
            ratio = signed_by_tender[deal["tender_id"]] / expected
            if ratio > RATIO_HIGH:
                flag(i, "Угоди", deal["tender_id"], "Договір значно більший за очікувану вартість",
                     f"у {ratio:.1f} раза ({money(signed_by_tender[deal['tender_id']])} проти "
                     f"{money(expected)} грн)", self.drop_outliers, dropped)
            elif ratio < RATIO_LOW:
                flag(i, "Угоди", deal["tender_id"], "Договір мізерний проти очікуваної вартості",
                     f"{pct(ratio)} від очікуваної ({money(deal['amount'])} грн)",
                     self.drop_outliers, dropped)
            else:
                confirmed.add(i)

        # --- статистичні викиди за сумою в межах галузі -------------------
        # Тільки для угод, яким нема з чим звіритися. Інакше поріг, порахований
        # на ринку з медіаною 10 тис. грн, оголошує помилкою кожну закупівлю
        # понад 18 млн — тобто саме ті договори, у яких лежить третина грошей.
        self.amount_stats = self._flag_by_group(
            self.deals, dropped | confirmed, value_key="amount", group_key="group",
            table="Угоди", reason="Нетипова сума для галузі", flag=flag)
        self.confirmed_deals = confirmed

        # --- позиції: ціна за одиницю -------------------------------------
        item_dropped: set[int] = set()
        for i, item in enumerate(self.data.items):
            price = item["unit_price"]
            if price is None or price <= 0:
                if item["unit_price"] is not None:
                    flag(i, "Номенклатура", item["tender_id"], "Нульова ціна за одиницю",
                         item["description"][:60], True, item_dropped)
                    item_dropped.add(i)
                continue
            if item["quantity"] is not None and item["quantity"] <= 0:
                flag(i, "Номенклатура", item["tender_id"], "Нульова кількість",
                     item["description"][:60], True, item_dropped)
                item_dropped.add(i)
        # Ціну за одиницю у вивантаженні порахували як «вартість лоту ÷
        # кількість», тож звірити її нема з чим — незалежного числа просто
        # немає. Через це нетипові ціни лише позначаємо: викинути «Скоби по
        # 40 копійок» і «Модуль NetApp за 37 млн» означало б прибрати з
        # аналізу справжні товари разом з їхніми ТМ.
        self.price_stats = self._flag_by_group(
            self.data.items, item_dropped, value_key="unit_price", group_key="cpv",
            table="Номенклатура", reason="Нетипова ціна за одиницю", flag=flag,
            label_key="description", drop=False)

        # --- схожі закупівлі одного дня -----------------------------------
        fingerprints: dict[tuple, list[str]] = defaultdict(list)
        for tender in self.tenders.values():
            fingerprints[(tender["buyer_edrpou"], tender["title"][:60],
                          round(tender["value"] or 0, 2), tender["date"])].append(
                tender["tender_id"])
        for (_buyer, title, _value, day), ids in fingerprints.items():
            if len(ids) > 1 and title:
                flag(None, "Закупівлі", ", ".join(ids[:3]), "Схожі закупівлі одного дня",
                     f"{len(ids)} шт.: {title[:50]}", False)

        self.dropped_deals = dropped
        self.dropped_items = item_dropped
        self.clean_deals = [d for i, d in enumerate(self.deals) if i not in dropped]
        self.clean_items = [r for i, r in enumerate(self.data.items) if i not in item_dropped]
        self.deals_by_tender: dict[str, list[dict]] = defaultdict(list)
        for deal in self.clean_deals:
            self.deals_by_tender[deal["tender_id"]].append(deal)
        self._build_clean_block(issues, reasons)
        self._step("Дані очищено", 2)

    def _flag_by_group(self, rows: list[dict], dropped: set[int], *, value_key: str,
                       group_key: str, table: str, reason: str, flag,
                       label_key: str = "tender_id", drop: bool | None = None
                       ) -> list[list[Any]]:
        """Позначає викиди стійким z-показником у межах кожної галузі.

        Порівнювати ціну ноутбука з ціною картриджа безглуздо, тому розподіл
        будується окремо для кожного коду ДК021. Малі групи (де статистика
        нічого не означає) зливаються в один загальний розподіл.

        ``dropped`` — рядки, які до розподілу не входять і не перевіряються:
        і вже вилучені, і ті, чиє значення підтверджене з іншого джерела.
        ``drop=False`` означає «лише позначити»: там, де підтвердити число
        нічим, видалення шкодить більше, ніж хибна позначка.
        """
        if drop is None:
            drop = self.drop_outliers
        buckets: dict[str, list[int]] = defaultdict(list)
        for i, row in enumerate(rows):
            if i in dropped:
                continue
            value = row.get(value_key)
            if value is None or value <= 0:
                continue
            buckets[str(row.get(group_key) or "")].append(i)

        small: list[int] = []
        groups: dict[str, list[int]] = {}
        for key, indexes in buckets.items():
            if len(indexes) >= MIN_GROUP:
                groups[key] = indexes
            else:
                small.extend(indexes)
        if small:
            groups.setdefault("_інші", []).extend(small)

        stats: list[list[Any]] = []
        for key, indexes in sorted(groups.items()):
            values = [float(rows[i][value_key]) for i in indexes]
            # Ціни й суми розподілені логнормально: у логарифмі «дешево» і
            # «дорого» стають симетричними, і поріг однаково справедливий
            # для дрібних та великих закупівель.
            scores = robust_z([_log10(value) for value in values])
            # Сортуємо один раз на групу: медіана й квартилі беруться з того
            # самого ряду, а не перераховуються для кожного знайденого викиду.
            ordered = sorted(values)
            middle = median_of(ordered)
            hits = 0
            for i, score in zip(indexes, scores):
                if score > Z_OUTLIER:
                    hits += 1
                    flag(i, table, str(rows[i].get(label_key) or "")[:60], reason,
                         f"{money(rows[i][value_key], 2)} грн, відхилення {score:.1f}σ "
                         f"(медіана {money(middle, 2)} грн)", drop, dropped)
            name = cpv_name(key) if key != "_інші" else "дрібні групи разом"
            stats.append([key, name, len(indexes), round(middle, 2),
                          round(quantile_of(ordered, 0.25), 2),
                          round(quantile_of(ordered, 0.75), 2),
                          round(ordered[0], 2), round(ordered[-1], 2), hits])
        return stats

    def _build_clean_block(self, issues: list[list[Any]], reasons: Counter) -> None:
        data = self.data
        total_rows = data.total_rows
        deals_kept = len(self.clean_deals)
        removed = len(self.dropped_deals)

        block = Block(
            "Попередня обробка та якість даних",
            "Перед будь-якими підсумками дані перевіряються на те, що робить суми "
            "неправдивими: чужу валюту, нулі, дублікати, неможливі співвідношення "
            "та статистичні викиди. Викид визначається стійким z-показником "
            "(медіана й MAD) у логарифмі суми окремо по кожній галузі ДК021.")
        block.tiles = [
            ("Рядків у книзі", count(total_rows)),
            ("Угод знайдено", count(len(self.deals))),
            ("Угод у розрахунках", count(deals_kept)),
            ("Виключено угод", f"{count(removed)} ({pct(share(removed, len(self.deals)))})"),
            ("Зауважень до даних", count(len(issues))),
        ]

        if not self.drop_outliers:
            block.notes.append("Режим «залишати викиди»: статистично нетипові суми "
                               "позначені, але з підсумків не вилучені.")
        block.notes.append(
            "Структурний брак (не гривня, нуль, дублікат) вилучається завжди — "
            "інакше суми перестають означати гроші.")
        confirmed = len(getattr(self, "confirmed_deals", ()))
        if confirmed:
            block.notes.append(
                f"Сума {count(confirmed)} угод підтверджена очікуваною вартістю "
                f"їхніх закупівель — статистика такі числа не перевіряє. Великий "
                f"договір, який збігається з планом замовника, це не помилка даних, "
                f"а велика закупівля; на цьому ринку саме в них лежить більша "
                f"частина грошей.")
        currencies = self._foreign_currency_note()
        if currencies:
            block.notes.append(currencies)
            block.tables.append(("Угоди в іноземній валюті", self._foreign_currency_sheet()))

        # Повнота ключових полів.
        quality: list[list[Any]] = []
        checks = [
            ("Закупівлі", data.tenders, [("ЄДРПОУ замовника", "buyer_edrpou"),
                                         ("Очікувана вартість", "value"),
                                         ("Дата", "date"), ("Коди ДК021", "cpv_list"),
                                         ("Регіон", "region")]),
            ("Договори", data.contracts, [("ЄДРПОУ постачальника", "edrpou"),
                                          ("Сума", "amount"), ("Дата підписання", "signed")]),
            ("Пропозиції", data.bids, [("ЄДРПОУ учасника", "edrpou"), ("Сума", "amount")]),
            ("Номенклатура", data.items, [("Код ДК021", "cpv"), ("Кількість", "quantity"),
                                          ("Ціна за одиницю", "unit_price")]),
            ("Документи", data.documents, [("Назва файлу", "title"),
                                           ("ЄДРПОУ власника", "owner_edrpou")]),
        ]
        for table_name, rows, fields in checks:
            if not rows:
                continue
            for label, key in fields:
                filled = sum(1 for row in rows if row.get(key) not in (None, "", 0))
                quality.append([table_name, label, len(rows), filled,
                                round(share(filled, len(rows)), 4)])
        block.tables.append(("Повнота даних", (
            ["Таблиця", "Поле", "Рядків", "Заповнено", "Частка"], quality)))

        if reasons:
            top = reasons.most_common(TOP_ROWS)
            block.charts.append(ChartData(
                "Зауваження до даних за типом", "hbar",
                [Series("Випадків", [name for name, _n in top], [n for _name, n in top])],
                unit="шт", money_axis=False))
        block.tables.append(("Зауваження до даних", (
            ["Таблиця", "Ключ", "Проблема", "Деталі", "Дія"], issues[:5000])))

        if self.amount_stats:
            block.tables.append(("Розподіл сум за галузями", (
                ["Код ДК021", "Назва", "Угод", "Медіана", "Нижній квартиль",
                 "Верхній квартиль", "Мінімум", "Максимум", "Викидів"], self.amount_stats)))
        if self.price_stats:
            block.tables.append(("Розподіл цін за одиницю", (
                ["Код ДК021", "Назва", "Позицій", "Медіана", "Нижній квартиль",
                 "Верхній квартиль", "Мінімум", "Максимум", "Викидів"], self.price_stats)))

        # Гістограма сум — за порядками величини, інакше один великий договір
        # робить усі стовпці невидимими.
        if self.clean_deals:
            block.charts.append(self._amount_histogram())
        scatter = self._price_scatter()
        if scatter:
            block.charts.append(scatter)

        self.report.add("Очищення", block)

    def _foreign_deals(self) -> list[dict]:
        """Угоди не в гривні — вони випадають із будь-яких підсумків."""
        return [deal for deal in self.deals
                if deal["currency"] and deal["currency"] != "UAH" and deal["amount"] > 0]

    def _foreign_currency_note(self) -> str:
        rows = self._foreign_deals()
        if not rows:
            return ""
        totals: Counter[str] = Counter()
        for deal in rows:
            totals[deal["currency"]] += deal["amount"]
        parts = ", ".join(f"{money(value)} {code}" for code, value in totals.most_common())
        return (f"У вибірці {count(len(rows))} угод в іноземній валюті ({parts}). "
                f"Без курсу на дату договору перевести їх у гривню чесно не можна, "
                f"тож у підсумки ринку вони не входять — перелік нижче, щоб ці "
                f"гроші не зникали мовчки.")

    def _foreign_currency_sheet(self) -> Sheet:
        headers = ["Закупівля", "Дата", "Постачальник", "ЄДРПОУ", "Сума", "Валюта",
                   "Очікувана вартість", "Замовник", "Галузь"]
        rows = []
        for deal in sorted(self._foreign_deals(), key=lambda d: -d["amount"]):
            rows.append([deal["tender_id"], deal["date"], deal["name"], deal["edrpou"],
                         round(deal["amount"], 2), deal["currency"],
                         round(deal["expected"] or 0, 2), deal["buyer"],
                         deal["group_name"]])
        return headers, rows

    def _amount_histogram(self) -> ChartData:
        buckets = [(0, 20_000, "до 20 тис."), (20_000, 100_000, "20–100 тис."),
                   (100_000, 500_000, "100–500 тис."), (500_000, 2_000_000, "0,5–2 млн"),
                   (2_000_000, 10_000_000, "2–10 млн"), (10_000_000, float("inf"), "понад 10 млн")]
        labels, values = [], []
        for low, high, label in buckets:
            labels.append(label)
            values.append(sum(1 for d in self.clean_deals if low <= d["amount"] < high))
        return ChartData("Розподіл угод за сумою", "bar",
                         [Series("Угод", labels, values)], unit="шт", money_axis=False,
                         hint="Скільки угод потрапляє в кожен ціновий діапазон.")

    def _price_scatter(self) -> ChartData | None:
        """Хмара «ціна × кількість» із підсвіченими викидами.

        Малювати десятки тисяч точок немає сенсу: вони зливаються в пляму, а
        перемальовування графіка стає помітним. Тому звичайні позиції беремо
        рівномірно проріджені, а всі викиди лишаємо — саме їх і треба бачити.
        """
        usable = [(i, item) for i, item in enumerate(self.data.items)
                  if (item["unit_price"] or 0) > 0 and (item["quantity"] or 0) > 0]
        if len(usable) < 10:
            return None
        dropped = self.dropped_items
        stride = max(1, len(usable) // SCATTER_POINTS)
        points: list[tuple[float, float]] = []
        accent: set[int] = set()
        for order, (i, item) in enumerate(usable):
            if i not in dropped and order % stride:
                continue
            points.append((_log10(item["quantity"]), _log10(item["unit_price"])))
            if i in dropped:
                accent.add(len(points) - 1)
        if len(points) < 10:
            return None
        return ChartData(
            "Ціна за одиницю проти кількості", "scatter",
            [Series("Позиції", points=points, accent=accent)],
            unit="", money_axis=False,
            x_title="Кількість, порядок величини",
            y_title="Ціна за одиницю, порядок величини",
            hint=f"Обидві осі логарифмічні (порядок величини). Червоним — позиції, "
                 f"які алгоритм вважає викидами. Показано {len(points)} з "
                 f"{len(usable)} позицій із ціною.")

    # --- 2. ринок ---------------------------------------------------------

    def market(self) -> None:
        deals = self.clean_deals
        tenders = list(self.tenders.values())
        total = sum(d["amount"] for d in deals)
        self.total_market = total

        by_supplier: dict[str, dict] = defaultdict(
            lambda: {"signed": 0.0, "deals": 0, "tenders": set(), "buyers": set(),
                     "regions": set(), "amounts": [], "first": "", "last": "",
                     "months": Counter(), "groups": Counter(), "methods": Counter()})
        for deal in deals:
            cell = by_supplier[deal["edrpou"]]
            cell["signed"] += deal["amount"]
            cell["deals"] += 1
            cell["tenders"].add(deal["tender_id"])
            cell["amounts"].append(deal["amount"])
            if deal["buyer_edrpou"]:
                cell["buyers"].add(deal["buyer_edrpou"])
            if deal["region"]:
                cell["regions"].add(deal["region"])
            if deal["month"]:
                cell["months"][deal["month"]] += deal["amount"]
            if deal["group"]:
                cell["groups"][deal["group_name"] or deal["group"]] += deal["amount"]
            if deal["method"]:
                cell["methods"][deal["method"]] += 1
            day = deal["date"]
            if day:
                cell["first"] = min(cell["first"] or day, day)
                cell["last"] = max(cell["last"], day)
        self.by_supplier = by_supplier

        ranking = sorted(by_supplier.items(), key=lambda kv: -kv[1]["signed"])
        self.ranking = ranking
        self.rank_of = {edrpou: i for i, (edrpou, _cell) in enumerate(ranking, start=1)}

        shares = [share(cell["signed"], total) for _e, cell in ranking]
        index = hhi(shares)
        cr3 = sum(shares[:3])
        cr5 = sum(shares[:5])
        cr10 = sum(shares[:10])
        pareto = 0
        running = 0.0
        for value in shares:
            running += value
            pareto += 1
            if running >= 0.8:
                break

        # Конкурентність: скільки учасників приходить і як часто закупівля
        # розігрується без боротьби.
        bidders = {tid: len({b["edrpou"] for b in rows})
                   for tid, rows in self.bids_by_tender.items()}
        for tender_id, tender in self.tenders.items():
            bidders.setdefault(tender_id, int(tender.get("n_bids") or 0))
        contested = [n for n in bidders.values() if n > 0]
        single = sum(1 for n in contested if n == 1)
        self.bidders = bidders

        savings = self._savings()
        discounts = self._discounts()
        self.market_discount = median(discounts) if discounts else None
        saved_money = sum(cell["expected"] - cell["signed"] for cell in savings.values())
        period = self._period()
        self.report.period = period

        expected_total = sum(t["value"] or 0 for t in tenders)
        block = Block(
            "Ринок у цілому",
            "Гроші ринку — це підписані договори; там, де договору ще немає, "
            "береться чинне рішення про переможця, щоб свіжі закупівлі не зникали "
            "з картини.")
        block.tiles = [
            ("Період", f"{period[0]} — {period[1]}" if period[0] else "—"),
            ("Закупівель", count(len(tenders))),
            ("Очікувана вартість", compact(expected_total) + " грн"),
            ("Сума угод", compact(total) + " грн"),
            ("Угод", count(len(deals))),
            ("Постачальників", count(len(by_supplier))),
            ("Замовників", count(len({t["buyer_edrpou"] for t in tenders if t["buyer_edrpou"]}))),
            ("Середня угода", compact(total / len(deals)) + " грн" if deals else "—"),
            ("Медіанна угода", compact(median(d["amount"] for d in deals)) + " грн"),
            ("HHI", f"{index:,.0f}".replace(",", " ") + f" — {hhi_verdict(index)}"),
            ("CR3 / CR5 / CR10", f"{pct(cr3, 0)} / {pct(cr5, 0)} / {pct(cr10, 0)}"),
            ("80% обороту дають", f"{pareto} компаній"),
            ("Нерівність (Джині)", f"{gini(d['amount'] for d in deals):.3f}".replace(".", ",")),
            ("Середньо учасників", f"{sum(contested) / len(contested):.2f}".replace(".", ",")
             if contested else "—"),
            ("Закупівель з одним учасником",
             pct(share(single, len(contested))) if contested else "—"),
            ("Економія на торгах",
             pct(median(discounts)) if discounts else "—"),
            ("Зекономлено на ринку",
             compact(saved_money) + " грн" if saved_money > 0 else "—"),
        ]
        block.notes.append(
            f"Ринок {hhi_verdict(index)} (HHI {index:,.0f}".replace(",", " ") +
            f"): трійка лідерів тримає {pct(cr3)} обороту, "
            f"а {pareto} компаній — 80%.")
        if contested:
            block.notes.append(
                f"Конкуренція: у середньому {sum(contested) / len(contested):.2f} учасника "
                f"на закупівлю, {pct(share(single, len(contested)))} закупівель "
                f"розігруються без боротьби (один учасник).".replace(".", ",", 1))

        # --- графіки ------------------------------------------------------
        monthly = self._monthly_series()
        partial = self._partial_months(period)
        if monthly:
            months = [m + " *" if m in partial else m for m, *_rest in monthly]
            edge_note = (" Зірочкою позначені неповні місяці — вибірка починається "
                         "й закінчується посеред місяця." if partial else "")
            block.charts.append(ChartData(
                "Динаміка ринку по місяцях", "line",
                [Series("Очікувана вартість", months, [row[1] for row in monthly]),
                 Series("Сума угод", months, [row[2] for row in monthly])],
                unit="грн",
                hint="Обидва ряди — за місяцем оприлюднення закупівлі, тож план і "
                     "факт стосуються тих самих закупівель." + edge_note))
            block.charts.append(ChartData(
                "Коли гроші розійшлися", "line",
                [Series("Сума за датою підпису", months, [row[4] for row in monthly])],
                unit="грн",
                hint="Той самий обсяг, але за датою підписання договору. Різниця з "
                     "попереднім графіком — це затримка між торгами й договором."))
            block.charts.append(ChartData(
                "Кількість закупівель по місяцях", "bar",
                [Series("Закупівель", months, [row[3] for row in monthly])],
                unit="шт", money_axis=False, hint=edge_note.strip()))

        top, accent = self._visible_ranking(ranking, TOP_ROWS)
        block.charts.append(ChartData(
            f"Топ-{TOP_ROWS} постачальників за сумою угод", "hbar",
            [Series("Сума угод",
                    [self._rank_label(e, TOP_ROWS) for e, _c in top],
                    [cell["signed"] for _e, cell in top],
                    accent=accent)],
            unit="грн",
            hint="Наші ТОВ виділені кольором і показані завжди. Те, що не ввійшло "
                 f"до топ-{TOP_ROWS}, дописане в кінець із номером свого місця "
                 "в рейтингу."))

        methods = Counter()
        for deal in deals:
            methods[deal["method"] or "не вказано"] += deal["amount"]
        block.charts.append(ChartData(
            "Структура ринку за процедурами", "pie",
            [Series("Сума", [m for m, _v in methods.most_common(8)],
                    [v for _m, v in methods.most_common(8)])], unit="грн"))

        groups = Counter()
        for deal in deals:
            groups[deal["group_name"] or deal["group"] or "не визначено"] += deal["amount"]
        block.charts.append(ChartData(
            "Структура ринку за галузями ДК021", "pie",
            [Series("Сума", [g for g, _v in groups.most_common(9)],
                    [v for _g, v in groups.most_common(9)])], unit="грн"))

        regions = Counter()
        for deal in deals:
            regions[deal["region"] or "не вказано"] += deal["amount"]
        block.charts.append(ChartData(
            "Географія ринку", "hbar",
            [Series("Сума", [r for r, _v in regions.most_common(TOP_ROWS)],
                    [v for _r, v in regions.most_common(TOP_ROWS)])], unit="грн"))

        if discounts:
            block.charts.append(self._discount_histogram(discounts))
        savings_rows = [(name, cell) for name, cell in savings.items() if cell["values"]]
        if savings_rows:
            block.charts.append(ChartData(
                "Економія за рівнем конкуренції", "bar",
                [Series("Медіанна економія", [name.split(" (")[0] for name, _c in savings_rows],
                        [median(cell["values"]) * 100 for _n, cell in savings_rows])],
                unit="%", money_axis=False,
                hint="Скільки замовник виграє від того, що на закупівлю прийшов "
                     "не один постачальник."))
            block.tables.append(("Економія та конкуренція", (
                ["Рівень конкуренції", "Закупівель", "Очікувана вартість, грн",
                 "Сума угод, грн", "Зекономлено, грн", "Зважена економія",
                 "Медіанна економія"],
                [[name, len(cell["values"]), round(cell["expected"], 2),
                  round(cell["signed"], 2), round(cell["expected"] - cell["signed"], 2),
                  round(share(cell["expected"] - cell["signed"], cell["expected"]), 4),
                  round(median(cell["values"]), 4)]
                 for name, cell in savings_rows])))
            best = max(savings_rows, key=lambda kv: median(kv[1]["values"]))
            worst = min(savings_rows, key=lambda kv: median(kv[1]["values"]))
            if best[0] != worst[0]:
                block.notes.append(
                    f"Конкуренція окупається: у закупівлях «{best[0].lower()}» медіанна "
                    f"економія {pct(median(best[1]['values']))}, а там, де конкуренції "
                    f"немає, — {pct(median(worst[1]['values']))}. Разом на ринку "
                    f"зекономлено {compact(saved_money)} грн від очікуваної вартості.")

        # --- таблиці ------------------------------------------------------
        block.tables.append(("Рейтинг постачальників", self._ranking_sheet()))
        block.tables.append(("Замовники", self._buyers_sheet()))
        block.tables.append(("Галузі ДК021", self._groups_sheet()))
        block.tables.append(("Процедури", (
            ["Процедура", "Угод", "Сума угод, грн", "Частка", "Середня угода, грн"],
            self._breakdown(lambda d: d["method"] or "не вказано"))))
        block.tables.append(("Регіони", (
            ["Регіон", "Угод", "Сума угод, грн", "Частка", "Середня угода, грн"],
            self._breakdown(lambda d: d["region"] or "не вказано"))))
        if monthly:
            # Наші суми по місяцях — одним проходом: тричі перебирати десятки
            # тисяч угод заради тих самих чисел не варто.
            ours_by_month: Counter[str] = Counter()
            for deal in deals:
                if not deal["ours"]:
                    continue
                published = (self.tenders.get(deal["tender_id"], {}).get("date")
                             or deal["date"] or "")[:7]
                if published:
                    ours_by_month[published] += deal["amount"]
            block.tables.append(("Динаміка по місяцях", (
                ["Місяць", "Повний місяць", "Очікувана вартість, грн", "Сума угод, грн",
                 "Сума за датою підпису, грн", "Закупівель", "Наша сума, грн",
                 "Наша частка"],
                [[m, "" if m in partial else "так", e, s, signed_later, n,
                  round(ours_by_month.get(m, 0.0), 2),
                  round(share(ours_by_month.get(m, 0.0), s), 4)]
                 for m, e, s, n, signed_later in monthly])))
        self.report.add("Ринок", block)
        self._step("Ринок пораховано", 3)

    def _short_name(self, edrpou: str, limit: int = 34) -> str:
        """Назва без організаційної форми — інакше графік показує самі «ТОВ».

        Довші форми стоять першими: інакше «ТОВ» з'їло б перші літери слова
        «Товариство», і від назви лишався б огризок.
        """
        name = ORG_FORM.sub("", self.names.get(edrpou, edrpou)).strip(' "«»')
        name = name or self.names.get(edrpou, "") or edrpou
        return name if len(name) <= limit else name[:limit - 1] + "…"

    def _period(self) -> tuple[str, str]:
        days = [t["date"] for t in self.tenders.values() if t["date"]]
        days += [d["date"] for d in self.clean_deals if d["date"]]
        return (min(days), max(days)) if days else ("", "")

    def _monthly_series(self) -> list[tuple[str, float, float, int, float]]:
        """Динаміка по місяцях: очікувана вартість, гроші та кількість.

        Обидва грошові ряди прив'язані до місяця **оприлюднення** закупівлі, а
        не до дати підпису. Інакше вони описували б різні речі: договір,
        підписаний у серпні за червневою закупівлею, дав би в червні «план без
        грошей», а в серпні — «гроші без плану». На реальних даних це
        показувало обвал у першому місяці вибірки та вибух в останньому,
        хоча насправді план і факт ідуть поруч.

        Дату підпису теж лишаємо окремим рядом — вона показує, коли гроші
        реально розійшлися, і це інше корисне питання.
        """
        expected: Counter[str] = Counter()
        counts: Counter[str] = Counter()
        for tender in self.tenders.values():
            month = month_of(tender["date"] or "")
            if month:
                expected[month] += tender["value"] or 0.0
                counts[month] += 1
        signed: Counter[str] = Counter()
        by_signature: Counter[str] = Counter()
        for deal in self.clean_deals:
            published = month_of(self.tenders.get(deal["tender_id"], {}).get("date")
                                 or deal["date"] or "")
            if published:
                signed[published] += deal["amount"]
            if deal["month"]:
                by_signature[deal["month"]] += deal["amount"]
        months = sorted(set(expected) | set(signed) | set(by_signature))
        return [(m, round(expected.get(m, 0.0), 2), round(signed.get(m, 0.0), 2),
                 counts.get(m, 0), round(by_signature.get(m, 0.0), 2)) for m in months]

    def _partial_months(self, period: tuple[str, str] | None = None) -> set[str]:
        """Місяці, зачеплені вибіркою лише частково.

        Збір за «останні 6 місяців» починається й закінчується посеред місяця,
        тож перший і останній стовпчики завжди нижчі за сусідні. Без позначки
        це читається як спад або зліт, якого не було.
        """
        first, last = period or self.report.period or ("", "")
        edges = set()
        if first and first[8:10] != "01":
            edges.add(first[:7])
        if last:
            edges.add(last[:7])          # останній місяць майже завжди неповний
        return edges

    def _savings(self) -> dict[str, dict]:
        """Економія в розрізі рівня конкуренції.

        Одне число «медіанна економія» на цьому ринку нічого не означає: майже
        дев'ять із десяти закупівель — прямі договори, де сума договору і є
        очікуваною вартістю, тож медіана по всій вибірці завжди нуль. Уся
        економія живе в конкурентних процедурах, і показувати її треба саме там.

        Договори, дорожчі за план, не відкидаємо: перевитрата — така сама
        частина картини, як і економія, просто з іншим знаком.
        """
        by_tender: dict[str, float] = defaultdict(float)
        for deal in self.clean_deals:
            by_tender[deal["tender_id"]] += deal["amount"]

        levels = {
            "Без конкуренції (прямий договір або один учасник)": (0, 1),
            "Двоє учасників": (2, 2),
            "Троє і більше": (3, 10 ** 6),
        }
        out: dict[str, dict] = {
            name: {"values": [], "expected": 0.0, "signed": 0.0} for name in levels}
        for tender_id, signed in by_tender.items():
            expected = (self.tenders.get(tender_id) or {}).get("value") or 0.0
            if expected <= 0 or signed <= 0:
                continue
            players = self.bidders.get(tender_id, 0)
            for name, (low, high) in levels.items():
                if low <= players <= high:
                    cell = out[name]
                    cell["values"].append(1 - signed / expected)
                    cell["expected"] += expected
                    cell["signed"] += signed
                    break
        return out

    def _discounts(self) -> list[float]:
        """Економія лише там, де була конкуренція — інакше це не економія."""
        savings = self._savings()
        return [value for name, cell in savings.items()
                for value in cell["values"] if not name.startswith("Без конкуренції")]

    def _discount_histogram(self, discounts: list[float]) -> ChartData:
        buckets = [(0, 0.001, "0%"), (0.001, 0.02, "до 2%"), (0.02, 0.05, "2–5%"),
                   (0.05, 0.10, "5–10%"), (0.10, 0.20, "10–20%"), (0.20, 0.30, "20–30%"),
                   (0.30, 1.01, "понад 30%")]
        labels = [label for _l, _h, label in buckets]
        values = [sum(1 for d in discounts if low <= d < high) for low, high, _l in buckets]
        return ChartData("Розподіл економії на конкурентних закупівлях", "bar",
                         [Series("Закупівель", labels, values)], unit="шт", money_axis=False,
                         hint="Різниця між очікуваною вартістю та сумою угоди там, де "
                              "на закупівлю прийшов більше ніж один учасник. У прямих "
                              "договорах ця різниця нульова за побудовою.")

    def _breakdown(self, key) -> list[list[Any]]:
        cells: dict[str, dict] = defaultdict(lambda: {"n": 0, "sum": 0.0})
        for deal in self.clean_deals:
            cell = cells[key(deal)]
            cell["n"] += 1
            cell["sum"] += deal["amount"]
        rows = []
        for name, cell in sorted(cells.items(), key=lambda kv: -kv[1]["sum"]):
            rows.append([name, cell["n"], round(cell["sum"], 2),
                         round(share(cell["sum"], self.total_market), 4),
                         round(cell["sum"] / max(cell["n"], 1), 2)])
        return rows

    def _ranking_sheet(self) -> Sheet:
        headers = ["Місце", "ЄДРПОУ", "Компанія", "Наша", "Угод", "Закупівель",
                   "Сума угод, грн", "Частка ринку", "Середня угода, грн",
                   "Медіанна угода, грн", "Замовників", "Областей", "Основна галузь",
                   "Перша угода", "Остання угода"]
        rows = []
        for place, (edrpou, cell) in enumerate(self.ranking, start=1):
            groups = cell["groups"].most_common(1)
            rows.append([
                place, edrpou, self.names.get(edrpou, ""),
                "так" if self.is_ours(edrpou) else "",
                cell["deals"], len(cell["tenders"]), round(cell["signed"], 2),
                round(share(cell["signed"], self.total_market), 4),
                round(cell["signed"] / max(cell["deals"], 1), 2),
                round(median(cell["amounts"]), 2),
                len(cell["buyers"]), len(cell["regions"]),
                groups[0][0] if groups else "", cell["first"], cell["last"],
            ])
        return headers, rows

    def _buyers_sheet(self) -> Sheet:
        cells: dict[str, dict] = defaultdict(
            lambda: {"name": "", "region": "", "tenders": set(), "expected": 0.0,
                     "signed": 0.0, "suppliers": set(), "ours": 0.0})
        for tender in self.tenders.values():
            code = tender["buyer_edrpou"]
            if not code:
                continue
            cell = cells[code]
            cell["name"] = cell["name"] or tender["buyer"]
            cell["region"] = cell["region"] or tender["region"]
            cell["tenders"].add(tender["tender_id"])
            cell["expected"] += tender["value"] or 0.0
        for deal in self.clean_deals:
            code = deal["buyer_edrpou"]
            if not code:
                continue
            cell = cells[code]
            cell["name"] = cell["name"] or deal["buyer"]
            cell["signed"] += deal["amount"]
            cell["suppliers"].add(deal["edrpou"])
            if deal["ours"]:
                cell["ours"] += deal["amount"]
        headers = ["ЄДРПОУ", "Замовник", "Регіон", "Закупівель", "Очікувана вартість, грн",
                   "Сума угод, грн", "Постачальників", "Наша сума, грн", "Наша частка"]
        rows = []
        for code, cell in sorted(cells.items(), key=lambda kv: -kv[1]["signed"]):
            rows.append([code, cell["name"], cell["region"], len(cell["tenders"]),
                         round(cell["expected"], 2), round(cell["signed"], 2),
                         len(cell["suppliers"]), round(cell["ours"], 2),
                         round(share(cell["ours"], cell["signed"]), 4)])
        return headers, rows

    def _groups_sheet(self) -> Sheet:
        cells: dict[str, dict] = defaultdict(
            lambda: {"name": "", "n": 0, "sum": 0.0, "suppliers": set(), "buyers": set(),
                     "ours": 0.0, "tenders": set()})
        for deal in self.clean_deals:
            key = deal["group"] or "—"
            cell = cells[key]
            cell["name"] = cell["name"] or deal["group_name"]
            cell["n"] += 1
            cell["sum"] += deal["amount"]
            cell["tenders"].add(deal["tender_id"])
            cell["suppliers"].add(deal["edrpou"])
            if deal["buyer_edrpou"]:
                cell["buyers"].add(deal["buyer_edrpou"])
            if deal["ours"]:
                cell["ours"] += deal["amount"]
        headers = ["Галузь ДК021", "Назва", "Угод", "Закупівель", "Сума угод, грн",
                   "Частка ринку", "Постачальників", "Замовників", "Наша сума, грн",
                   "Наша частка"]
        rows = []
        for key, cell in sorted(cells.items(), key=lambda kv: -kv[1]["sum"]):
            rows.append([key, cell["name"], cell["n"], len(cell["tenders"]),
                         round(cell["sum"], 2), round(share(cell["sum"], self.total_market), 4),
                         len(cell["suppliers"]), len(cell["buyers"]),
                         round(cell["ours"], 2), round(share(cell["ours"], cell["sum"]), 4)])
        return headers, rows


def month_of(day: str) -> str:
    """``2026-05-01`` → ``2026-05``; неможлива дата — порожньо.

    Місяць звідси йде в математику прогнозу, яка робить із нього справжній
    ``date``, і «2026-13» валило б **увесь** аналіз (виміряно: ``ValueError:
    month must be in 1..12`` на першому ж перетворенні). Книга вивантаження
    такого вже не дає — :func:`app.core.xlsxload.as_date` перевіряє дату, —
    але ``Dataset`` можна скласти й повз неї, а ціна помилки тут — не
    зіпсований рядок, а порожня сторінка аналітики.
    """
    if (len(day) >= 7 and day[4] == "-" and day[:4].isdigit() and day[5:7].isdigit()
            and 1 <= int(day[5:7]) <= 12):
        return day[:7]
    return ""


def _log10(value: float) -> float:
    return math.log10(value) if value and value > 0 else 0.0


#: Порядок вкладок звіту. Підсумок збирається останнім, а показується першим.
#: «Прогнозування» стоїть одразу за «Ринком»: воно продовжує ту саму місячну
#: динаміку, і читати його окремо від неї немає сенсу.
SECTIONS = ["Підсумок", "Очищення", "Ринок", "Прогнозування", "Відмови", "Наші ТОВ",
            "Конкуренти", "Товари і ТМ", "Постачання", "Порівняння"]


def analyse(data: Dataset, own_edrpou: Iterable[str] = (), tracked: Iterable[str] = (),
            drop_outliers: bool = True, top_competitors: int = TOP_COMPETITORS,
            on_progress: Callable[[str, int, int], None] | None = None,
            cancel_event=None, horizon: int = DEFAULT_HORIZON) -> Report:
    """Повний аналіз книги. Єдина точка входу для інтерфейсу."""
    analyzer = Analyzer(data, own_edrpou, tracked, drop_outliers,
                        top_competitors, on_progress, cancel_event, horizon)
    analyzer.prepare()
    analyzer.clean()
    analyzer.market()
    analyzer.rejections()
    analyzer.profiles()
    analyzer.brands()
    analyzer.supply()
    analyzer.compare()
    analyzer.outlook()
    analyzer.summary()
    report = analyzer.report
    report.sections = {name: report.sections[name] for name in SECTIONS
                       if report.sections.get(name)}
    return report
