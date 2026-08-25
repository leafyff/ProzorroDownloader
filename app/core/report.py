"""Структури звіту й статистика, на якій він тримається.

Рушій аналізу нічого не знає про Qt: він складає звіт із простих структур —
плитки, тексти, дані графіків і таблиці. Інтерфейс лише малює те, що прийшло,
а вивантаження в Excel бере ті самі таблиці. Завдяки цьому один і той самий
аналіз однаково лягає і на екран, і у книгу.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: Таблиця у звіті — ті самі ``(заголовки, рядки)``, що і в решті програми.
Sheet = tuple[list[str], list[list[Any]]]

#: Текст, яким аналіз чесно зізнається, що далі потрібна не арифметика.
NEEDS_AI = "Потрібний глибший ШІ аналіз"


# --- статистика -----------------------------------------------------------

def clean(values: Iterable[Any]) -> list[float]:
    """Лише скінченні числа — решта для статистики шкідлива."""
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def median_of(data: Sequence[float]) -> float:
    """Медіана вже відсортованого набору.

    Окрема функція, бо на великій вибірці сортування — найдорожча частина, а
    з одного відсортованого списку зазвичай треба і медіану, і квартилі.
    """
    if not data:
        return 0.0
    middle = len(data) // 2
    if len(data) % 2:
        return data[middle]
    return (data[middle - 1] + data[middle]) / 2


def quantile_of(data: Sequence[float], q: float) -> float:
    """Квантиль відсортованого набору лінійною інтерполяцією."""
    if not data:
        return 0.0
    if len(data) == 1:
        return data[0]
    position = (len(data) - 1) * min(max(q, 0.0), 1.0)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return data[low]
    return data[low] + (data[high] - data[low]) * (position - low)


def median(values: Iterable[Any]) -> float:
    return median_of(sorted(clean(values)))


def quantile(values: Iterable[Any], q: float) -> float:
    """Квантиль лінійною інтерполяцією (як ``PERCENTILE.INC`` в Excel)."""
    return quantile_of(sorted(clean(values)), q)


def _std(data: Sequence[float]) -> float:
    """Стандартне відхилення — останній запасний масштаб для z-показника."""
    if len(data) < 2:
        return 0.0
    mean = sum(data) / len(data)
    return math.sqrt(sum((value - mean) ** 2 for value in data) / (len(data) - 1))


def robust_z(values: Sequence[float]) -> list[float]:
    """Модифікований z-показник Іглвіча — Гоуліна.

    Звичайний z-показник рахується від середнього й стандартного відхилення,
    а їх самі викиди й псують: один договір на мільярд робить «нормою» майже
    все. Тому центр — медіана, а масштаб — MAD. Коефіцієнт 0.6745 приводить
    MAD до масштабу стандартного відхилення нормального розподілу.

    Якщо MAD нульовий (половина значень однакові), масштаб беремо з
    міжквартильного розмаху. А якщо нульовий і він — понад три чверті значень
    збігаються, — лишається звичайне стандартне відхилення: у такому ряду
    навіть одне інше число вже помітне, і мовчки пропускати його не можна.
    Коли ж усі значення однакові, розкиду немає взагалі.
    """
    data = clean(values)
    if len(data) < 4:
        return [0.0] * len(values)
    ordered = sorted(data)
    center = median_of(ordered)
    scale = median_of(sorted(abs(value - center) for value in ordered))
    if scale > 0:
        factor = 0.6745 / scale
    else:
        iqr = quantile_of(ordered, 0.75) - quantile_of(ordered, 0.25)
        if iqr > 0:
            factor = 1.0 / (iqr / 1.349)
        else:
            spread = _std(ordered)
            if spread <= 0:
                return [0.0] * len(values)
            factor = 1.0 / spread
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            out.append(0.0)
            continue
        out.append(abs(number - center) * factor if math.isfinite(number) else 0.0)
    return out


def hhi(shares: Iterable[float]) -> float:
    """Індекс Герфіндаля-Гіршмана у пунктах (0…10 000) за частками 0…1."""
    return round(sum((s * 100.0) ** 2 for s in shares if s > 0), 1)


def hhi_verdict(index: float) -> str:
    if index >= 2500:
        return "високо концентрований"
    if index >= 1500:
        return "помірно концентрований"
    return "низько концентрований"


def gini(values: Iterable[float]) -> float:
    """Коефіцієнт Джині: 0 — усі рівні, 1 — усе в одного."""
    data = sorted(v for v in clean(values) if v > 0)
    if len(data) < 2:
        return 0.0
    total = sum(data)
    if total <= 0:
        return 0.0
    weighted = sum((i + 1) * value for i, value in enumerate(data))
    return round((2 * weighted) / (len(data) * total) - (len(data) + 1) / len(data), 3)


def share(part: float, whole: float) -> float:
    return (part / whole) if whole else 0.0


# --- формати --------------------------------------------------------------

def money(value: Any, digits: int = 0) -> str:
    """``1234567.8`` → ``1 234 568``. Порожнє значення — риска.

    Формат український: тисячі відокремлені пробілом, дробова частина — комою.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    whole, _, fraction = f"{number:,.{digits}f}".partition(".")
    whole = whole.replace(",", " ")
    return f"{whole},{fraction}" if fraction else whole


def compact(value: Any) -> str:
    """Коротка сума для плиток: ``12 345 678`` → ``12,3 млн``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    for limit, suffix in ((1e9, " млрд"), (1e6, " млн"), (1e3, " тис.")):
        if abs(number) >= limit:
            return f"{number / limit:,.1f}".replace(",", " ").replace(".", ",") + suffix
    return money(number)


def pct(value: Any, digits: int = 1) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number * 100:.{digits}f}%".replace(".", ",")


def count(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


# --- складники звіту ------------------------------------------------------

@dataclass
class Series:
    """Один ряд даних графіка."""
    name: str = ""
    labels: list[str] = field(default_factory=list)
    values: list[float] = field(default_factory=list)
    #: Для точкових графіків — пари ``(x, y)``.
    points: list[tuple[float, float]] = field(default_factory=list)
    #: Індекси значень, які треба виділити кольором (викиди, наші ТОВ).
    accent: set[int] = field(default_factory=set)


@dataclass
class ChartData:
    """Дані одного графіка: рушій каже, *що* малювати, а не *як*."""
    title: str
    kind: str = "bar"            # bar | hbar | pie | line | area | hist | scatter
    series: list[Series] = field(default_factory=list)
    hint: str = ""
    unit: str = ""               # «грн», «шт», «%» — для підписів
    money_axis: bool = True


@dataclass
class Block:
    """Смисловий блок сторінки: заголовок, показники, графіки, таблиці."""
    title: str
    hint: str = ""
    tiles: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    charts: list[ChartData] = field(default_factory=list)
    tables: list[tuple[str, Sheet]] = field(default_factory=list)


@dataclass
class Profile:
    """Портрет компанії — нашої або конкурента."""
    edrpou: str
    name: str = ""
    is_ours: bool = False
    rank: int = 0
    signed: float = 0.0
    share: float = 0.0
    n_contracts: int = 0
    n_tenders: int = 0
    n_bids: int = 0
    n_wins: int = 0
    #: Скільки з поданих пропозицій виграно. Не те саме, що ``n_wins``:
    #: частину закупівель гравець бере поза торгами, де пропозицій не видно.
    n_won_bids: int = 0
    win_rate: float | None = None
    avg_check: float = 0.0
    median_check: float = 0.0
    n_buyers: int = 0
    n_regions: int = 0
    top_brand: str = ""
    top_brand_share: float = 0.0
    brands: list[tuple[str, int]] = field(default_factory=list)
    main_product: str = ""
    top_region: str = ""
    discount: float | None = None
    repeat_share: float = 0.0
    trend: float | None = None
    reporting_share: float = 0.0
    certificates: int = 0
    authorizations: int = 0
    first_seen: str = ""
    last_seen: str = ""
    traits: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    block: Block | None = None

    @property
    def label(self) -> str:
        return f"{self.name or 'без назви'} ({self.edrpou})"


@dataclass
class Report:
    """Готовий звіт: усе, що показує сторінка й що лягає у книгу Excel."""
    source: str = ""
    generated: str = ""
    period: tuple[str, str] = ("", "")
    sections: dict[str, list[Block]] = field(default_factory=dict)
    ours: list[Profile] = field(default_factory=list)
    competitors: list[Profile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, section: str, block: Block) -> None:
        self.sections.setdefault(section, []).append(block)

    def sheets(self) -> dict[str, Sheet]:
        """Усі таблиці звіту для вивантаження. Назви аркушів — унікальні.

        Таблиці профілів не розкладаються по аркушу на компанію — двадцять
        гравців дали б сотню аркушів, у яких нічого не знайти. Однойменні
        таблиці зводяться в одну, а компанія стає першими двома колонками:
        такий аркуш зручно крутити зведеною таблицею.
        """
        out: dict[str, Sheet] = {}
        used: set[str] = set()

        def put(name: str, sheet: Sheet) -> None:
            base = (name or "Таблиця")[:31]
            title = base
            n = 2
            while title.lower() in used:
                suffix = f" {n}"
                title = base[:31 - len(suffix)] + suffix
                n += 1
            used.add(title.lower())
            out[title] = sheet

        for blocks in self.sections.values():
            for block in blocks:
                for name, sheet in block.tables:
                    if sheet and sheet[1]:
                        put(name, sheet)

        merged: dict[tuple[str, tuple], list[list[Any]]] = {}
        order: list[tuple[str, tuple]] = []
        for profile in [*self.ours, *self.competitors]:
            if not profile.block:
                continue
            for name, (headers, rows) in profile.block.tables:
                if not rows:
                    continue
                key = (name, tuple(headers))
                if key not in merged:
                    merged[key] = []
                    order.append(key)
                merged[key].extend([profile.edrpou, profile.name, *row] for row in rows)
        for key in order:
            name, headers = key
            put(f"{name} — гравці", (["ЄДРПОУ", "Компанія", *headers], merged[key]))
        return out
