"""Прогнозування місячних показників ринку.

Модуль навмисно нічого не знає ні про закупівлі, ні про звіт: на вході — ряд
чисел по місяцях, на виході — прогноз із межами. Усе на чистому Python, без
numpy й statsmodels: у проєкті три залежності, і заводити четверту на сотню
рядків арифметики немає сенсу.

Порядок роботи повторює те, у якому прогноз узагалі можна робити чесно:

1. **Повнота.** Останні місяці вибірки завжди неповні — і за днями (збір
   уривається посеред місяця), і за розвитком (договір з'являється не в день
   оприлюднення закупівлі). Спершу ряд доводиться до «як буде, коли все
   допишеться», інакше модель побачить обвал, якого немає.
2. **Моделі.** Десяток простих моделей — від наївної до сезонної. Кожна знає,
   скільки точок їй потрібно, і жодна не бере даних більше, ніж є.
3. **Перевірка.** Ковзний початок (rolling origin) і MASE — усі моделі
   міряються на **однакових** згортках, інакше числа непорівнянні.
4. **Вибір.** Мінімум MASE, а далі правило однієї стандартної похибки: серед
   моделей, що вкладаються в межу «найкраща + SE», перемагає найощадливіша.
   На шести точках різниця в MASE — це здебільшого шум, і складніша модель
   виграє її випадково.
5. **Межі.** Інтервал за формулами дисперсії ETS (Hyndman, «Forecasting:
   Principles and Practice», табл. 8.8) з квантилем Стьюдента: на 4-6 точках
   нормальний квантиль занижує інтервал на чверть. До дисперсії моделі
   додається дисперсія самого **вибору** моделі — розкид прогнозів усіх
   моделей, які перевірка визнала рівноправними. Без неї 80% межі накривали
   факт лише в 70-72% випадків (виміряно на 120 ковзних прогнозах), бо
   інтервал мовчки вважав обрану модель відомою наперед.

Головна вимога до модуля — **не вигадувати**. Якщо місяців менше трьох,
прогнозу немає взагалі; якщо згорток замало, модель обирається не перевіркою,
а за ощадністю, і про це прямо сказано в ``Outcome.notes``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Sequence

#: Менше трьох місяців — не ряд, а дві точки: прогнозу не буде.
MIN_MONTHS = 3
#: Скільки місяців прогнозуємо за замовчуванням.
DEFAULT_HORIZON = 3
#: Довірча ймовірність інтервалу. 80% — типова для прогнозів межа: 95% на
#: шести точках виходить такою широкою, що не несе жодної інформації.
LEVEL = 0.80
#: Скільки згорток потрібно, щоб перевірка взагалі щось означала.
MIN_FOLDS = 2
#: Місяць, від якого лишилося менше цієї частки, у підгонку не береться:
#: домножити спостережене вчетверо — це вже не корекція, а вигадування.
MIN_COMPLETE = 0.40
#: Довжина сезону.
SEASON = 12
#: Скільки днів розвитку розглядаємо щонайбільше.
MAX_DEVELOPMENT = 180
#: Менша когорта криву розвитку не тримає. На одній-двох закупівлях вона
#: показує не закономірність, а те, коли підписали саме ці договори, — і
#: домножувати на неї цілий місяць означало б множити на випадок.
MIN_COHORT = 20


# --- календар -------------------------------------------------------------

def month_start(month: str) -> date:
    return date(int(month[:4]), int(month[5:7]), 1)


def month_next(month: str) -> str:
    year, number = int(month[:4]), int(month[5:7])
    return f"{year + (number == 12)}-{number % 12 + 1:02d}"


def month_shift(month: str, count: int) -> str:
    out = month
    for _ in range(max(count, 0)):
        out = month_next(out)
    return out


def month_days(month: str) -> list[date]:
    first = month_start(month)
    last = month_start(month_next(month))
    return [first + timedelta(days=i) for i in range((last - first).days)]


def weekday_weights(days: Sequence[date]) -> list[float]:
    """Скільки закупівель припадає на кожен день тижня — частками від одиниці.

    Вихідні на цьому ринку майже порожні (виміряно на вибірці 2026 року:
    субота 1,2%, неділя 0,7% проти 17-22% у будні), тож рахувати неповний
    місяць простою часткою днів не можна: обрив у понеділок і обрив у неділю
    з'їдають зовсім різну частину місяця.
    """
    counts = [0] * 7
    for day in days:
        counts[day.weekday()] += 1
    total = sum(counts)
    if not total:
        return [1 / 7] * 7
    # Порожній день тижня лишати нулем небезпечно: місяць, що обірвався в
    # суботу, отримав би експозицію рівно 1 і виглядав би повним.
    return [max(value / total, 0.001) for value in counts]


def month_exposure(month: str, first: date, last: date,
                   weights: Sequence[float]) -> float:
    """Яка частка місяця потрапила у вікно вибірки ``[first, last]``."""
    days = month_days(month)
    whole = sum(weights[day.weekday()] for day in days)
    seen = sum(weights[day.weekday()] for day in days if first <= day <= last)
    return seen / whole if whole > 0 else 0.0


# --- крива розвитку -------------------------------------------------------

class Development:
    """Яку частку остаточної суми місяця видно через ``u`` днів.

    Договір з'являється не в день оприлюднення закупівлі. Виміряно на вибірці
    з 893 закупівель (березень-серпень 2026): у день оприлюднення видно 24%
    грошей, через тиждень — 58%, через два — 92%, через три — 99%, і лише на
    28-й день ряд закривається. Тому «сума угод» останнього місяця завжди
    занижена, і без поправки будь-яка модель бачить у ньому обвал.

    Крива будується відтворенням знімків (у страховій математиці — трикутник
    розвитку): для кожної закупівлі відомо, коли саме кожна гривня стала
    видимою, тож видно й те, як виглядав би ряд місяць тому. Приріст може
    бути від'ємним — рішення про переможця заміщується договором на іншу
    суму, — і саме тому крива рахується з приростів, а не з дат договорів.
    """

    def __init__(self, cohorts: Sequence[tuple[int, Sequence[tuple[int, float]]]],
                 max_age: int = MAX_DEVELOPMENT):
        #: ``cohorts`` — пари ``(вік закупівлі на дату зрізу, [(лаг, приріст)])``.
        self.horizon = 0
        self.curve: list[float] = [1.0]
        self.cohort = 0
        rough = self._guess(cohorts, max_age)
        if rough <= 0:
            return
        # Другий прохід: вибравши горизонт за грубою оцінкою, перебудовуємо
        # криву на ширшій когорті. Що менший горизонт, то більше закупівель
        # встигли «дозріти», і то стійкіша крива — на реальних даних вона
        # збігається до третього знака вже на горизонті 45-75 днів.
        self.horizon = rough
        self.curve = self._build(cohorts, rough)
        self.cohort = sum(1 for age, _steps in cohorts if age >= rough)

    def _guess(self, cohorts, max_age: int) -> int:
        period = max((age for age, _s in cohorts), default=0)
        if period <= 0:
            return 0
        # Когорта має бути і зрілою, і не крихітною: беремо третину періоду,
        # але не менше двох тижнів і не більше вказаної стелі.
        start = min(max(period // 3, 14), max_age, period)
        curve = self._build(cohorts, start)
        for age, value in enumerate(curve):
            if value >= 0.995:
                return max(age, 1)
        return start

    @staticmethod
    def _build(cohorts, horizon: int) -> list[float]:
        """G(u) для u = 0…horizon по закупівлях, що вже дозріли."""
        seen = [0.0] * (horizon + 1)
        ultimate = 0.0
        for age, steps in cohorts:
            if age < horizon:
                continue
            running = 0.0
            index = 0
            ordered = sorted(steps)
            for u in range(horizon + 1):
                while index < len(ordered) and ordered[index][0] <= u:
                    running += ordered[index][1]
                    index += 1
                seen[u] += running
            ultimate += running
        if ultimate <= 0:
            return [1.0] * (horizon + 1)
        # Крива має бути неспадною: на дрібній когорті від'ємний приріст
        # (договір дешевший за рішення) інакше дає «ковток» усередині.
        out: list[float] = []
        best = 0.0
        for value in seen:
            best = max(best, value / ultimate)
            out.append(min(best, 1.0))
        return out

    def share(self, age: int) -> float:
        """Частка суми закупівлі, видима через ``age`` днів після оприлюднення."""
        if age < 0:
            return 0.0
        if age >= len(self.curve):
            return 1.0
        return self.curve[age]

    @property
    def known(self) -> bool:
        return self.horizon > 0 and self.cohort >= MIN_COHORT

    def table(self) -> list[list[Any]]:
        marks = [0, 3, 7, 10, 14, 21, 28, 35, 45, 60, 90, 120, 180]
        return [[age, round(self.share(age), 4)]
                for age in marks if age <= max(self.horizon, 0)]


# --- статистика -----------------------------------------------------------

def _num(value: float, digits: int = 2, sign: bool = False) -> str:
    """Число для підпису моделі — з комою й без показника степеня.

    Підпис бачить користувач, а не лише розробник: рівень моделі в гривнях
    ``%g`` записував як ``1,705e+05``, і в підказці до графіка це читалося
    як збій. Тому великі числа йдуть розрядами, а малі (α, β, φ) лишаються
    зі значущими цифрами.
    """
    mark = "+" if sign else ""
    if abs(value) >= 1000:
        return f"{value:{mark},.0f}".replace(",", " ")
    return f"{value:{mark}.{digits}g}".replace(".", ",")


def _median(values: Sequence[float]) -> float:
    data = sorted(values)
    if not data:
        return 0.0
    middle = len(data) // 2
    if len(data) % 2:
        return data[middle]
    return (data[middle - 1] + data[middle]) / 2


def _betacf(a: float, b: float, x: float) -> float:
    """Ланцюговий дріб для неповної бета-функції (метод Лентца)."""
    tiny = 1e-30
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        step = d * c
        h *= step
        if abs(step - 1.0) < 3e-12:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Регуляризована неповна бета-функція I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                     + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_quantile(p: float, df: float) -> float:
    """Квантиль розподілу Стьюдента — половинка інтервалу в одиницях σ.

    Потрібен саме він, а не нормальний: на чотирьох ступенях свободи
    двобічні 80% дають 1,533σ проти 1,282σ у нормального, тобто інтервал
    ширший на чверть. Округляти це до нормального означало б показувати
    впевненість, якої немає.
    """
    if df <= 0:
        return float("inf")
    if p <= 0.5:
        return -t_quantile(1.0 - p, df)
    low, high = 0.0, 200.0
    for _ in range(90):
        middle = (low + high) / 2
        x = df / (df + middle * middle)
        cdf = 1.0 - 0.5 * _betainc(df / 2.0, 0.5, x)
        if cdf < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2


# --- моделі ---------------------------------------------------------------

@dataclass
class Fitted:
    """Підігнана модель: як прогнозує, як помиляється, як росте невизначеність."""
    predict: Callable[[int], list[float]]
    residuals: list[float]
    ratio: Callable[[int], float]
    detail: str = ""


class Model:
    """Спільний інтерфейс моделі.

    ``params`` — не кількість чисел усередині, а кількість **рішень**, які
    модель приймає за нас: саме за нею правило однієї стандартної похибки
    обирає ощадливішу. ``dof`` — інша річ: скільки параметрів справді
    оцінено з даних. Плутати їх не можна, бо ``dof`` іде в знаменник
    дисперсії залишків, і зайва одиниця там роздуває інтервал на чверть.
    ``min_points`` — скільки точок потрібно, щоб підгонка взагалі мала сенс
    (грубо: параметрів плюс два-три спостереження на кожен).
    """
    name = ""
    params = 0
    dof = 0
    min_points = 2

    def fit(self, y: Sequence[float]) -> Fitted:      # перевизначається
        raise NotImplementedError


class Naive(Model):
    """Випадкове блукання: завтра буде як сьогодні. Еталон для MASE."""
    name = "Наївна (останній місяць)"
    params = 0
    dof = 0
    min_points = 2

    def fit(self, y):
        last = y[-1]
        errors = [y[i] - y[i - 1] for i in range(1, len(y))]
        return Fitted(lambda h: [last] * h, errors, lambda h: math.sqrt(h))


class Flat(Model):
    """Стала на рівні медіани — стійкий варіант «середнього».

    Медіана, а не середнє: один місяць-сплеск (у вибірці 2026 року це червень
    із 12,3 млн проти 7-9 в решті) зсуває середнє на десяту частину, а
    медіану не зачіпає взагалі.
    """
    name = "Стала (медіана)"
    params = 1
    dof = 1
    min_points = 2

    def fit(self, y):
        level = _median(y)
        errors = [value - level for value in y]
        scale = math.sqrt(1.0 + 1.0 / len(y))
        return Fitted(lambda h: [level] * h, errors, lambda h: scale,
                      f"рівень {_num(level, 4)}")


class Drift(Model):
    """Пряма через першу й останню точку — найпростіший тренд."""
    name = "Дрейф"
    params = 2
    dof = 1
    min_points = 4

    def fit(self, y):
        n = len(y)
        slope = (y[-1] - y[0]) / (n - 1)
        errors = [y[i] - (y[i - 1] + slope) for i in range(1, n)]
        return Fitted(lambda h: [y[-1] + slope * (i + 1) for i in range(h)],
                      errors,
                      lambda h: math.sqrt(h * (1.0 + h / n)),
                      f"нахил {_num(slope, 4, sign=True)}/міс")


def _solve(matrix: list[list[float]], right: list[float]) -> list[float]:
    """Розв'язок малої системи методом Гаусса з вибором головного елемента."""
    size = len(right)
    grid = [row[:] + [right[i]] for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda r: abs(grid[r][column]))
        if abs(grid[pivot][column]) < 1e-12:
            return [0.0] * size
        grid[column], grid[pivot] = grid[pivot], grid[column]
        for row in range(size):
            if row == column:
                continue
            factor = grid[row][column] / grid[column][column]
            for k in range(column, size + 1):
                grid[row][k] -= factor * grid[column][k]
    return [grid[i][size] / grid[i][i] for i in range(size)]


def _affine_start(run: Callable[[Sequence[float]], list[float]], size: int
                  ) -> tuple[list[float], list[float]]:
    """Початковий стан згладжування, підібраний найменшими квадратами.

    Рекурсії згладжування лінійні за початковим станом: помилка кожного кроку
    — це «помилка при нульовому старті» мінус лінійна комбінація складників
    стану. Тому оптимальний старт має точний розв'язок, і шукати його
    перебором не треба.

    Це не косметика. Доки рівень стартував із першого спостереження, мале α
    робило прогноз ≈ першому місяцю вибірки: на живих даних SES віддавав
    8,07 млн — рівно березень, — хоча решта ряду стояла біля 8,7 млн.
    """
    base = run([0.0] * size)
    columns: list[list[float]] = []
    for j in range(size):
        unit = [0.0] * size
        unit[j] = 1.0
        shifted = run(unit)
        columns.append([base[i] - shifted[i] for i in range(len(base))])
    normal = [[sum(columns[a][i] * columns[b][i] for i in range(len(base)))
               for b in range(size)] for a in range(size)]
    right = [sum(columns[a][i] * base[i] for i in range(len(base)))
             for a in range(size)]
    start = _solve(normal, right)
    errors = [base[i] - sum(start[j] * columns[j][i] for j in range(size))
              for i in range(len(base))]
    return start, errors


def _ses_errors(y: Sequence[float], alpha: float, level: float) -> list[float]:
    errors: list[float] = []
    for value in y[1:]:
        error = value - level
        errors.append(error)
        level += alpha * error
    return errors


def _ses_pass(y: Sequence[float], alpha: float) -> tuple[float, float, list[float]]:
    """SSE, кінцевий рівень і залишки для заданого α з оптимальним стартом."""
    (start,), errors = _affine_start(
        lambda init: _ses_errors(y, alpha, init[0]), 1)
    level = start
    for error in errors:
        level += alpha * error
    return sum(e * e for e in errors), level, errors


class Ses(Model):
    """Просте експоненційне згладжування, ETS(A,N,N)."""
    name = "Експоненційне згладжування"
    params = 2
    dof = 2
    min_points = 4

    def fit(self, y):
        alpha, level, errors = 0.3, y[-1], []
        best = None
        for step in range(1, 20):
            candidate = step / 20
            sse, last, errs = _ses_pass(y, candidate)
            if best is None or sse < best:
                best, alpha, level, errors = sse, candidate, last, errs
        return Fitted(lambda h: [level] * h, errors,
                      lambda h: math.sqrt(1.0 + alpha * alpha * (h - 1)),
                      f"α={_num(alpha)}")


class Theta(Model):
    """Метод Тета — переможець змагання M3.

    Дорівнює згладжуванню з половинним нахилом лінії найменших квадратів:
    тренд не відкидається, але й не продовжується цілком. На коротких рядах
    це майже завжди краще за обидві крайності.
    """
    name = "Тета"
    params = 2
    dof = 3
    min_points = 5

    def fit(self, y):
        n = len(y)
        mean_x = (n - 1) / 2
        mean_y = sum(y) / n
        sxx = sum((i - mean_x) ** 2 for i in range(n)) or 1.0
        slope = sum((i - mean_x) * (y[i] - mean_y) for i in range(n)) / sxx
        alpha, level, errors = 0.3, y[-1], []
        best = None
        for step in range(1, 20):
            candidate = step / 20
            sse, last, errs = _ses_pass(y, candidate)
            if best is None or sse < best:
                best, alpha, level, errors = sse, candidate, last, errs
        drift = slope / 2
        tail = (1.0 - (1.0 - alpha) ** n) / alpha

        def predict(h: int) -> list[float]:
            return [level + drift * (i + tail) for i in range(h)]

        return Fitted(predict, errors,
                      lambda h: math.sqrt(1.0 + alpha * alpha * (h - 1)),
                      f"α={_num(alpha)}, нахил {_num(slope, 4, sign=True)}/міс")


def _damped_errors(y: Sequence[float], alpha: float, beta: float, phi: float,
                   level: float, trend: float) -> list[float]:
    errors: list[float] = []
    for value in y[1:]:
        forecast = level + phi * trend
        error = value - forecast
        errors.append(error)
        level = forecast + alpha * error
        trend = phi * trend + beta * error
    return errors


def _damped_pass(y: Sequence[float], alpha: float, beta: float, phi: float):
    (level, trend), errors = _affine_start(
        lambda init: _damped_errors(y, alpha, beta, phi, init[0], init[1]), 2)
    for error in errors:
        forecast = level + phi * trend
        level = forecast + alpha * error
        trend = phi * trend + beta * error
    return sum(e * e for e in errors), level, trend, errors


class Damped(Model):
    """Гольт із загасаючим трендом, ETS(A,Ad,N).

    Гарднер і Маккензі (1985) показали: тренд, продовжений без загасання,
    систематично перебільшує — а загасаючий варіант виявився настільки
    стійким, що в літературі його беруть за еталон, який важко перемогти.
    Саме тому він тут єдина модель із трендом, що не згасає до нуля.
    """
    name = "Гольт із загасанням"
    params = 5
    dof = 5
    min_points = 8

    def fit(self, y):
        best = None
        for a10 in range(1, 10):
            alpha = a10 / 10
            for beta in (0.02, 0.05, 0.10, 0.20, 0.35, 0.50):
                for phi in (0.75, 0.85, 0.90, 0.95, 0.98):
                    sse, level, trend, errors = _damped_pass(y, alpha, beta, phi)
                    if best is None or sse < best[0]:
                        best = (sse, alpha, beta, phi, level, trend, errors)
        _sse, alpha, beta, phi, level, trend, errors = best

        def predict(h: int) -> list[float]:
            out, running = [], 0.0
            for i in range(h):
                running += phi ** (i + 1)
                out.append(level + running * trend)
            return out

        def ratio(h: int) -> float:
            # Формула дисперсії ETS(A,Ad,N) — Hyndman, табл. 8.8.
            if h <= 1:
                return 1.0
            phi_h = phi * (1.0 - phi ** h) / (1.0 - phi)
            one = (beta * phi_h / (1.0 - phi) ** 2) * (2 * alpha * (1 - phi) + beta * phi)
            two = ((beta * phi * (1.0 - phi ** h))
                   / ((1.0 - phi) ** 2 * (1.0 - phi * phi))
                   * (2 * alpha * (1 - phi * phi) + beta * phi * (1 + 2 * phi - phi ** h)))
            value = 1.0 + alpha * alpha * (h - 1) + one - two
            return math.sqrt(max(value, 1.0))

        return Fitted(predict, errors, ratio,
                      f"α={_num(alpha)}, β={_num(beta)}, φ={_num(phi)}")


class TheilSen(Model):
    """Стійка пряма: нахил — медіана нахилів усіх пар точок.

    Один викид зсуває звичайну регресію, а медіану попарних нахилів — ні
    (точка зламу 29%). На ряду з одним місяцем-сплеском це різниця між
    «ринок росте вдвічі» і «ринок стоїть».
    """
    name = "Стійка пряма (Тейл — Сен)"
    params = 2
    dof = 2
    min_points = 5

    def fit(self, y):
        n = len(y)
        slopes = [(y[j] - y[i]) / (j - i) for i in range(n) for j in range(i + 1, n)]
        slope = _median(slopes)
        base = _median([y[i] - slope * i for i in range(n)])
        errors = [y[i] - (base + slope * i) for i in range(n)]
        mean_x = (n - 1) / 2
        sxx = sum((i - mean_x) ** 2 for i in range(n)) or 1.0

        def ratio(h: int) -> float:
            x = n - 1 + h
            return math.sqrt(1.0 + 1.0 / n + (x - mean_x) ** 2 / sxx)

        return Fitted(lambda h: [base + slope * (n - 1 + i + 1) for i in range(h)],
                      errors, ratio, f"нахил {_num(slope, 4, sign=True)}/міс")


class SeasonalNaive(Model):
    """Торішній той самий місяць. Еталон для рядів із сезонністю."""
    name = "Сезонна наївна"
    params = 0
    dof = 0
    min_points = SEASON + 1

    def fit(self, y):
        n = len(y)
        errors = [y[i] - y[i - SEASON] for i in range(SEASON, n)]

        def predict(h: int) -> list[float]:
            out = []
            for i in range(1, h + 1):
                k = (i - 1) // SEASON
                out.append(y[n + i - SEASON * (k + 1) - 1])
            return out

        return Fitted(predict, errors, lambda h: math.sqrt((h - 1) // SEASON + 1))


class Seasonal(Model):
    """Сезонні поправки плюс будь-яка несезонна модель усередині.

    Класична декомпозиція: центрована ковзна середня дає тренд, різниця з нею
    — сезонні індекси, і далі базова модель працює вже з очищеним рядом.
    Це стійкіше за повний Голт-Вінтерс на 24-36 точках, бо індекси рахуються
    один раз і не змагаються з рештою параметрів за ті самі спостереження.
    """
    min_points = 2 * SEASON

    def __init__(self, base: Model):
        self.base = base
        self.name = f"Сезонна поправка + {base.name.lower()}"
        self.params = base.params + 1
        self.dof = base.dof + SEASON - 1
        self.min_points = max(2 * SEASON, base.min_points + SEASON)

    def fit(self, y):
        index = self._indices(y)
        plain = [y[i] - index[i % SEASON] for i in range(len(y))]
        inner = self.base.fit(plain)
        offset = len(y)

        def predict(h: int) -> list[float]:
            return [value + index[(offset + i) % SEASON]
                    for i, value in enumerate(inner.predict(h))]

        return Fitted(predict, inner.residuals, inner.ratio, inner.detail)

    @staticmethod
    def _indices(y: Sequence[float]) -> list[float]:
        """Адитивні сезонні індекси; сума по році зведена до нуля."""
        n = len(y)
        half = SEASON // 2
        parts: list[list[float]] = [[] for _ in range(SEASON)]
        for i in range(half, n - half):
            window = y[i - half:i + half + 1]
            trend = (sum(window) - (window[0] + window[-1]) / 2) / SEASON
            parts[i % SEASON].append(y[i] - trend)
        raw = [_median(part) if part else 0.0 for part in parts]
        shift = sum(raw) / SEASON
        return [value - shift for value in raw]


class Combo(Model):
    """Середнє з простих моделей.

    У змаганнях M3 і M4 просте усереднення кількох моделей стабільно
    обходило кожну з них поодинці: помилки в різні боки гасяться. Тут
    усереднюються всі моделі пулу з не більш ніж трьома рішеннями.
    """
    name = "Комбінація простих"

    def __init__(self, members: Sequence[Model]):
        self.members = list(members)
        self.params = max((m.params for m in self.members), default=0) + 1
        self.dof = max((m.dof for m in self.members), default=0)
        self.min_points = max((m.min_points for m in self.members), default=2)

    def fit(self, y):
        parts = [model.fit(y) for model in self.members]
        count = len(parts)

        def predict(h: int) -> list[float]:
            grids = [part.predict(h) for part in parts]
            return [sum(grid[i] for grid in grids) / count for i in range(h)]

        # Залишки усереднюємо по спільному хвосту: моделі лишають різну
        # кількість одноразових помилок (наївна — n-1, стала — n).
        tail = min(len(part.residuals) for part in parts)
        errors = [sum(part.residuals[-tail + i] for part in parts) / count
                  for i in range(tail)] if tail else []
        return Fitted(predict, errors,
                      lambda h: sum(part.ratio(h) for part in parts) / count,
                      f"{count} моделі")


class Logged(Model):
    """Та сама модель, але на логарифмах — тобто зі сталим *темпом*.

    Гроші ринку ростуть відсотками, а не гривнями, і не бувають від'ємними.
    Прогноз у логарифмах обидві властивості дає задарма, а межі виходять
    несиметричними — як і має бути в розподілі, витягнутому вправо.
    Повертаючись у гривні, точку правимо на зсув логнормального середнього
    (Hyndman: множник ``1 + σ²/2``), інакше сума трьох місяців буде меншою
    за суму трьох окремих прогнозів.
    """

    def __init__(self, base: Model):
        self.base = base
        self.name = f"{base.name} (логарифми)"
        self.params = base.params + 1
        self.dof = base.dof
        self.min_points = base.min_points

    def fit(self, y):
        logs = [math.log(value) for value in y]
        inner = self.base.fit(logs)
        # Поправку застосовуємо, лише коли дисперсію справді є на чому
        # оцінити. На двох-трьох залишках вона сама шум: перша ж перевірка
        # на живих даних дала SES у логарифмах MASE 0,04 проти 0,45 у
        # звичайної — і весь виграш був у тому, що роздута σ випадково
        # підняла рівень рівно туди, де опинився наступний місяць.
        df = len(inner.residuals) - self.dof
        sigma = _sigma(inner.residuals, self.dof) if df >= 2 else 0.0

        def predict(h: int) -> list[float]:
            out = []
            for i, value in enumerate(inner.predict(h), start=1):
                spread = sigma * inner.ratio(i)
                out.append(math.exp(value) * (1.0 + spread * spread / 2))
            return out

        # Залишки повертаємо в шкалу ряду, щоб MASE й межі рахувалися там
        # само, де живуть дані: логарифмічна помилка з ними непорівнянна.
        errors = [math.exp(logs[-len(inner.residuals) + i]) -
                  math.exp(logs[-len(inner.residuals) + i] - inner.residuals[i])
                  for i in range(len(inner.residuals))]
        return Fitted(predict, errors, inner.ratio, inner.detail)


def _sigma(residuals: Sequence[float], dof: int) -> float:
    """Стандартне відхилення залишків із поправкою на ступені свободи."""
    df = max(len(residuals) - dof, 1)
    return math.sqrt(sum(e * e for e in residuals) / df)


def pool_for(y: Sequence[float], positive: bool, quick: bool = False) -> list[Model]:
    """Моделі, яким вистачає цього ряду. Порядок — від простої до складної.

    ``quick`` прибирає моделі з перебором сітки (Гольт і сезонна на його
    основі). Потрібен там, де рядів десятки — прогноз по кожному гравцеві
    окремо: перебір 270 комбінацій на кожній згортці кожного ряду коштує
    десятки секунд і нічого не додає до вибірки з 6-12 точок.
    """
    n = len(y)
    plain: list[Model] = [Naive(), Flat(), Drift(), Ses(), Theta(), TheilSen()]
    if not quick:
        plain.append(Damped())
    models = [model for model in plain if model.min_points <= n]
    if positive:
        models += [Logged(model) for model in models
                   if isinstance(model, (Ses, Theta, TheilSen, Damped))]
    if n >= SeasonalNaive.min_points:
        models.append(SeasonalNaive())
    for base in ((Ses(),) if quick else (Ses(), Damped())):
        wrapped = Seasonal(base)
        if wrapped.min_points <= n:
            models.append(wrapped)
    simple = [model for model in models if model.params <= 3]
    if len(simple) >= 2:
        models.append(Combo(simple))
    return models


# --- перевірка ковзним початком -------------------------------------------

@dataclass
class Score:
    """Як модель показала себе на перевірці.

    ``checks`` — скільки саме перевірок вона витримала: на кожному початку
    моделі дають прогноз на кілька місяців уперед, і кожен із них — окреме
    порівняння з фактом. Це не те саме, що кількість початків, і саме
    ``checks`` вирішує, чи можна вірити самому MASE.
    """
    model: str
    params: int
    mase: float | None
    checks: int
    detail: str = ""
    chosen: bool = False


def _mase_scale(y: Sequence[float]) -> float:
    """Масштаб MASE — середня похибка наївного прогнозу в межах вибірки."""
    if len(y) < 2:
        return 0.0
    return sum(abs(y[i] - y[i - 1]) for i in range(1, len(y))) / (len(y) - 1)


def cross_validate(y: Sequence[float], models: Sequence[Model], horizon: int
                   ) -> tuple[int, dict[str, list[float]]]:
    """Ковзний початок: усі моделі — на **однакових** згортках.

    Спільний початок береться за найвимогливішою моделлю пулу. Дозволити
    кожній свій — найпоширеніша й найтихіша помилка в цій задачі: модель, яка
    почала пізніше, міряється на коротшому й легшому хвості ряду, і її MASE
    виходить кращим ні за що. Виміряно на цій самій вибірці: згладжування
    з власним початком показувало MASE 0,48 проти 1,26 у наївної, а на
    спільних згортках — 0,48 проти 1,07.
    """
    n = len(y)
    if not models:
        return 0, {}
    start = max(model.min_points for model in models)
    if n - start < MIN_FOLDS:
        return 0, {}
    scale = _mase_scale(y)
    if scale <= 0:
        return 0, {}
    errors: dict[str, list[float]] = {}
    for model in models:
        scaled: list[float] = []
        for origin in range(start, n):
            try:
                predicted = model.fit(y[:origin]).predict(min(horizon, n - origin))
            except (ValueError, ZeroDivisionError, OverflowError):
                scaled = []
                break
            for i, value in enumerate(predicted):
                if math.isfinite(value):
                    scaled.append(abs(y[origin + i] - value) / scale)
        if scaled:
            errors[model.name] = scaled
    return start, errors


def choose(models: Sequence[Model], errors: dict[str, list[float]]
           ) -> tuple[Model | None, list[Score], list[Model]]:
    """Найкраща за MASE, а далі — правило однієї стандартної похибки.

    На чотирьох-шести точках згорток виходить три-чотири, і різниця в MASE
    між сусідами менша за її власну похибку. Тому серед моделей, що
    вкладаються в «найкраща + SE», перемагає та, що приймає найменше рішень:
    складнішу модель ми беремо, тільки якщо вона виграла помітно.
    """
    scores: list[Score] = []
    for model in models:
        values = errors.get(model.name)
        if values:
            mean = sum(values) / len(values)
            scores.append(Score(model.name, model.params, round(mean, 3), len(values)))
        else:
            scores.append(Score(model.name, model.params, None, 0))
    ranked = [(errors[m.name], m, s) for m, s in zip(models, scores)
              if s.mase is not None]
    if not ranked:
        return None, scores, []
    best_values, _best_model, best_score = min(ranked, key=lambda row: row[2].mase)
    spread = 0.0
    if len(best_values) > 1:
        mean = sum(best_values) / len(best_values)
        variance = sum((v - mean) ** 2 for v in best_values) / (len(best_values) - 1)
        spread = math.sqrt(variance / len(best_values))
    limit = best_score.mase + spread
    band = [row for row in ranked if row[2].mase <= limit]
    if best_score.mase < 1.0:
        # Ощадність не може переважити програш еталону. На рядах окремих
        # компаній розкид помилок такий великий, що смуга «найкраща + SE»
        # накривала навіть наївну модель із MASE 2,07 — і вона вигравала
        # просто тому, що не має жодного параметра. Модель, гірша за
        # наївну, не має перемагати ні за яких обставин.
        band = [row for row in band if row[2].mase < 1.0] or band
    winner = min(band, key=lambda row: (row[1].params, row[2].mase))
    winner[2].chosen = True
    return winner[1], scores, [row[1] for row in band]


def _rival_spread(y: Sequence[float], band: Sequence[Model], picked: Model,
                  horizon: int, predicted: Sequence[float]) -> list[float]:
    """Наскільки розходяться моделі, які перевірка визнала рівноправними.

    Інтервал, порахований за формулою дисперсії обраної моделі, мовчки
    припускає, що модель відома наперед. А її обрали — тими самими даними, на
    яких потім рахують межі. Ціна цього припущення виміряна: на 120 ковзних
    прогнозах підряд 80% межі накривали факт у 70-72% випадків, і найгірше
    саме там, де перевірка помилково віддавала перевагу тренду на рівному ряду.

    Тому до дисперсії моделі додається дисперсія **вибору**: середньоквадратичне
    відхилення прогнозів усіх моделей смуги «найкраща + SE» від обраного. Коли
    моделі згодні, воно нульове й нічого не змінює; коли розходяться — інтервал
    росте рівно на стільки, наскільки непевний сам вибір.
    """
    others = [model for model in band if model.name != picked.name]
    if not others:
        return [0.0] * horizon
    grids: list[list[float]] = []
    for model in others:
        try:
            grid = model.fit(y).predict(horizon)
        except (ValueError, ZeroDivisionError, OverflowError):
            continue
        if all(math.isfinite(value) for value in grid):
            grids.append(grid)
    if not grids:
        return [0.0] * horizon
    out: list[float] = []
    for i in range(horizon):
        gaps = [grid[i] - predicted[i] for grid in grids]
        out.append(math.sqrt(sum(gap * gap for gap in gaps) / len(gaps)))
    return out


# --- результат ------------------------------------------------------------

@dataclass
class Observation:
    """Один місяць історії — як спостережено й як скориговано."""
    month: str
    value: float
    adjusted: float
    exposure: float = 1.0
    development: float = 1.0
    used: bool = True

    @property
    def completeness(self) -> float:
        return self.exposure * self.development


@dataclass
class Prediction:
    month: str
    value: float
    low: float
    high: float


@dataclass
class Outcome:
    """Прогноз одного показника."""
    name: str
    unit: str = ""
    history: list[Observation] = field(default_factory=list)
    points: list[Prediction] = field(default_factory=list)
    model: str = ""
    detail: str = ""
    params: int = 0
    mase: float | None = None
    #: Скільки початків дала ковзна перевірка (не те саме, що ``checks``).
    folds: int = 0
    sigma: float = 0.0
    level: float = LEVEL
    scores: list[Score] = field(default_factory=list)
    #: Середньомісячна зміна **всередині** прогнозу — власний нахил моделі.
    #: Для сталих моделей це рівно нуль, і так і має бути: рівна лінія не
    #: має «темпу». Порівняння з останнім місяцем — окреме число нижче,
    #: бо це зовсім інше питання.
    trend: float | None = None
    #: Наскільки перший прогнозний місяць відрізняється від останнього факту.
    versus_last: float | None = None
    notes: list[str] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    #: Скільки разів обрану модель звірили з фактом.
    checks: int = 0
    #: Скільки моделей перевірка визнала рівноправними з обраною. Що їх
    #: більше й що дужче вони розходяться, то ширший інтервал.
    rivals: int = 0

    @property
    def total(self) -> float:
        return sum(point.value for point in self.points)

    @property
    def last(self) -> float:
        used = [row.adjusted for row in self.history if row.used]
        return used[-1] if used else 0.0

    @property
    def grade(self) -> str:
        """Наскільки перевіреним є цей прогноз — трьома словами.

        Бінарне «надійний / ненадійний» тут брехало б в обидва боки: модель
        із MASE 0,2 на трьох перевірках і така сама на тридцяти — різні речі,
        хоча обидві «кращі за наївну». Тому станів три, і межа стоїть на
        шести перевірках: менше — це один-два місяці історії, віддані на
        звірку, і будь-який висновок з них тримається на випадку.
        """
        if self.mase is None or not self.checks:
            return "не перевірено"
        if self.mase >= 1.0:
            return "не краща за наївну"
        return "перевірено" if self.checks >= 6 else "перевірено побіжно"

    @property
    def reliable(self) -> bool:
        """Чи можна на цей прогноз спиратися хоч якось."""
        return bool(self.ok and self.mase is not None
                    and self.mase < 1.0 and self.checks >= 3)


def forecast(name: str, months: Sequence[str], values: Sequence[float], *,
             unit: str = "", horizon: int = DEFAULT_HORIZON,
             exposure: dict[str, float] | None = None,
             development: dict[str, float] | None = None,
             floor: float | None = 0.0, quick: bool = False) -> Outcome:
    """Прогноз одного місячного ряду з межами.

    ``exposure`` і ``development`` — частки, на які місяць неповний за днями
    й за розвитком договорів. Ряд підганяється по **скоригованих** значеннях:
    модель має бачити ринок, а не край вибірки.
    """
    out = Outcome(name=name, unit=unit)
    horizon = max(1, int(horizon))
    exposure = exposure or {}
    development = development or {}

    for month, value in zip(months, values):
        e = min(max(exposure.get(month, 1.0), 0.0), 1.0)
        d = min(max(development.get(month, 1.0), 0.0), 1.0)
        factor = e * d
        adjusted = value / factor if factor > 0 else value
        out.history.append(Observation(month, round(value, 2), round(adjusted, 2), e, d,
                                       used=factor >= MIN_COMPLETE))

    fit_rows = [row for row in out.history if row.used]
    if len(fit_rows) < MIN_MONTHS:
        out.reason = (f"Для прогнозу потрібно щонайменше {MIN_MONTHS} придатних "
                      f"місяці, а їх {len(fit_rows)}. Зберіть довший період.")
        return out
    # Дірка посеред ряду ламає будь-яку модель мовчки: місяці стають нерівними
    # кроками, а моделі рахують крок сталим. Тому беремо суцільний хвіст.
    tail = [fit_rows[-1]]
    for row in reversed(fit_rows[:-1]):
        if month_next(row.month) != tail[0].month:
            break
        tail.insert(0, row)
    if len(tail) < MIN_MONTHS:
        out.reason = ("У ряду розрив між місяцями — суцільного відрізка "
                      f"завдовжки {MIN_MONTHS} місяці немає.")
        return out
    keep = {row.month for row in tail}
    for row in out.history:
        row.used = row.month in keep

    y = [row.adjusted for row in tail]
    if max(y) - min(y) <= 0:
        out.notes.append("Ряд сталий — прогноз повторює те саме значення.")
    models = pool_for(y, positive=all(value > 0 for value in y), quick=quick)
    # Модель, якій ряду не вистачить навіть на дві згортки, до перевірки не
    # допускаємо: інакше вона визначає спільний початок і забирає згортки в усіх.
    models = [model for model in models if model.min_points <= len(y) - MIN_FOLDS] or \
             [model for model in models if model.min_points <= len(y)]
    start, errors = cross_validate(y, models, horizon)
    picked, scores, band = choose(models, errors)
    out.scores = scores
    out.folds = len(y) - start if start else 0

    if picked is None:
        # Перевірити нічим — лишається медіана: на трьох-чотирьох точках це
        # єдина відповідь, яку не соромно показати. Наївна модель тут гірша:
        # вона повністю довіряє останньому місяцю, а саме він скоригований
        # найсильніше і помиляється найбільше.
        picked = next((m for m in models if isinstance(m, Flat)),
                      min(models, key=lambda m: (m.params, m.min_points)))
        for score in scores:
            score.chosen = score.model == picked.name
        out.notes.append(
            f"Згорток для перевірки не вистачило ({len(y)} місяців), тож модель "
            f"обрано не за точністю, а за ощадністю. Це орієнтир, не прогноз.")
    else:
        best = min((s.mase for s in scores if s.mase is not None), default=None)
        chosen = next((s for s in scores if s.chosen), None)
        if chosen and best is not None and chosen.mase > best:
            out.notes.append(
                f"За чистим MASE попереду інша модель ({best:.2f} проти "
                f"{chosen.mase:.2f}), але різниця менша за власну похибку "
                f"перевірки, тож обрано ощадливішу.")

    fitted = picked.fit(y)
    out.model = picked.name
    out.detail = fitted.detail
    out.params = picked.params
    chosen = next((s for s in scores if s.chosen), None)
    out.mase = chosen.mase if chosen else None
    out.checks = chosen.checks if chosen else 0

    sigma = _sigma(fitted.residuals, picked.dof)
    # Залишки в межах вибірки завжди оптимістичні: модель підганялася саме під
    # них. Якщо перевірка ковзним початком показала більший розкид, беремо її.
    tested = errors.get(picked.name) or []
    if len(tested) >= 3:
        scale = _mase_scale(y)
        real = [value * scale for value in tested]
        cv_sigma = math.sqrt(sum(v * v for v in real) / len(real))
        sigma = max(sigma, cv_sigma)
    out.sigma = round(sigma, 4)

    df = max(len(fitted.residuals) - picked.dof, 1)
    quantile = t_quantile((1.0 + LEVEL) / 2, df)
    last_month = tail[-1].month
    predicted = fitted.predict(horizon)
    rival = _rival_spread(y, band, picked, horizon, predicted)
    out.rivals = len(band)
    for i, value in enumerate(predicted, start=1):
        # Дисперсія складається з двох: наскільки помиляється сама модель і
        # наскільки взагалі не факт, що модель та. Другу частину дає розкид
        # прогнозів усіх моделей, які перевірка визнала рівноправними.
        inside = sigma * fitted.ratio(i)
        spread = quantile * math.sqrt(inside * inside + rival[i - 1] ** 2)
        low, high = value - spread, value + spread
        if floor is not None:
            value = max(value, floor)
            low = max(low, floor)
            high = max(high, floor)
        out.points.append(Prediction(month_shift(last_month, i),
                                     round(value, 2), round(low, 2), round(high, 2)))

    if len(out.points) >= 2 and out.points[0].value > 0:
        ratio = out.points[-1].value / out.points[0].value
        out.trend = (ratio ** (1.0 / (len(out.points) - 1)) - 1.0
                     if ratio > 0 else -1.0)
    elif out.points:
        out.trend = 0.0
    if y[-1] > 0 and out.points:
        out.versus_last = out.points[0].value / y[-1] - 1.0
    out.ok = True
    return out
