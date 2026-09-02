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

#: Кольори рядів графіка. Порядок підібраний так, щоб сусідні ряди різнилися і
#: за тоном, і за яскравістю — графік лишається читабельним і в чорно-білому
#: друці. Палітра живе тут, а не у віджеті, бо ті самі кольори беруть і
#: намальовані на QPainter графіки, і діаграми в книзі Excel: розійшовшись,
#: вони показували б один ряд різними кольорами на екрані й у звіті.
SERIES_COLORS = [
    "#3d7eff", "#3ecf8e", "#f0b429", "#a78bfa", "#22d3ee",
    "#fb923c", "#f472b6", "#84cc16", "#e879f9", "#94a3b8",
]
#: Колір для виділених значень — наші ТОВ у рейтингах.
OWN_COLOR = "#3ecf8e"
#: Колір викидів у точковій хмарі.
OUTLIER_COLOR = "#ef5f5f"

#: Слова в назві колонки, за якими число показується відсотком.
PERCENT_WORDS = ("частка", "дисконт", "результативність", "розрив", "динаміка",
                 "повторні", "без торгів", "економія", "охоплення")


def is_percent_column(header: str) -> bool:
    """Чи ця колонка — частка (0…1), яку показують відсотком.

    Тип колонки визначається за назвою, а не за значеннями: 0,25 у колонці
    «Частка» — це 25%, а в колонці «Розрив» — теж, а от у «Позицій» — ні.
    Правило спільне для таблиці на екрані й для формату клітинки в книзі,
    інакше та сама колонка читалася б по-різному.
    """
    low = str(header or "").lower()
    return any(word in low for word in PERCENT_WORDS)


def is_money_column(header: str) -> bool:
    return "грн" in str(header or "").lower()


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
    збігаються, — лишається звичайне стандартне відхилення.
    Коли ж усі значення однакові, розкиду немає взагалі.

    **Межа останнього запасного масштабу.** Самотній викид роздуває те саме
    стандартне відхилення, яким його міряють: у ряду з ``n`` однакових значень
    і одного іншого показник дорівнює рівно ``√n`` — незалежно від того,
    наскільки те число інше. Тож при :data:`app.core.insight.Z_OUTLIER` = 3,5
    одинокий викид помітний лише з 13 значень, а в найменшій групі
    (``MIN_GROUP`` = 8) не перетне поріг ніколи. Це властивість масштабу, а не
    недогляд: пропустити викид тут безпечніше, ніж вилучити з аналізу
    справжній товар, а на групах із двома й більше несхожими значеннями MAD
    або міжквартильний розмах уже не нульові й працюють нормально.
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
    """Коротка сума для плиток: ``12 345 678`` → ``12,3 млн``.

    Нескінченність і NaN відсіюємо так само, як :func:`money`: ділення на нуль
    десь у рушії не має перетворюватись на плитку «inf млрд».
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
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


def plural(number: int, one: str, few: str, many: str) -> str:
    """Форма іменника за числом: «1 раз», «2 рази», «5 разів».

    Українська рахує трьома формами, і число у висновку майже завжди
    підставляється з даних — тобто «зачепило 1 разів» вийде рано чи пізно.
    Винятком є другий десяток: 11-14 беруть множину попри останню цифру.
    """
    if 11 <= number % 100 <= 14:
        return many
    tail = number % 10
    if tail == 1:
        return one
    return few if 2 <= tail <= 4 else many


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
    #: Назви осей. Потрібні там, де категорії не називають себе самі, —
    #: тобто на точковій хмарі, де без них видно лише два стовпці чисел.
    x_title: str = ""
    y_title: str = ""


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
    #: ТМ лише з виграних закупівель — на відміну від ``brands``, куди входять
    #: і програні подання. Потрібні там, де питання саме про проданий товар.
    sold_brands: list[tuple[str, int]] = field(default_factory=list)
    main_product: str = ""
    top_region: str = ""
    discount: float | None = None
    repeat_share: float = 0.0
    trend: float | None = None
    reporting_share: float = 0.0
    certificates: int = 0
    authorizations: int = 0
    #: Скасовані перемоги: рішення про переможця, які замовник відхилив.
    #: Це не те саме, що програш — компанія вже виграла, і договір зірвався
    #: після цього.
    n_rejected: int = 0
    rejected_sum: float = 0.0
    #: Найчастіша причина відмов і частка зривів серед усіх перемог компанії.
    reject_reason: str = ""
    reject_share: float | None = None
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
