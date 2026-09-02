"""Розділ «Прогнозування»: місячні показники ринку на кілька місяців уперед.

Рушій прогнозу (:mod:`app.core.forecast`) не знає ні про закупівлі, ні про
звіт — він працює з рядом чисел. Цей модуль перекладає одне в інше: дістає
з вибірки місячні ряди, рахує, наскільки неповні останні місяці, і складає
з відповідей рушія звичні плитки, графіки й таблиці.

Порядок такий самий, як у решті аналітики: спочатку чесність про дані,
потім числа. Тому перший блок розділу — прогноз, а другий — звідки він
узявся: крива розвитку договорів, повнота кожного місяця й таблиця
перевірки моделей. Без другого блоку перший читався б як віщування.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any, Sequence

from . import forecast as fc
from .report import Block, ChartData, Series, Sheet, compact, count, money, pct

#: Скільки гравців беремо в таблицю прогнозу по компаніях.
TOP_PLAYERS = 15
#: Скільки місяців із грошима потрібно гравцеві, щоб його ряд щось означав.
PLAYER_MIN_ACTIVE = 3
#: Показники, які прогнозуємо. ``key`` — індекс у ``_monthly_series``.
INDICATORS = (
    ("Сума угод", "грн", 2, True),
    ("Очікувана вартість", "грн", 1, False),
    ("Закупівель", "шт", 3, False),
)


def _iso(day: str) -> date | None:
    try:
        return date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return None


def _on_months(count: int) -> str:
    """«На 1 такому місяці», «на 5 таких місяцях» — узгоджено з числом.

    Місцевий відмінок різниться лише для одиниці (крім 11), тож правило
    коротке: 1, 21, 31 — однина, решта — множина.
    """
    single = count % 10 == 1 and count % 100 != 11
    return (f"На {count} такому місяці" if single
            else f"На {count} таких місяцях")


class OutlookMixin:
    """Прогноз ринку на наступні місяці."""

    # --- повнота вибірки --------------------------------------------------

    def _exposure(self, months: Sequence[str]) -> dict[str, float]:
        """Яка частка кожного місяця взагалі потрапила у вікно збору.

        Збір за «останні пів року» починається й закінчується посеред місяця,
        тож крайні стовпчики нижчі за сусідні просто тому, що в них менше
        днів. Модель, яка цього не знає, читає обрив як спад — на реальній
        вибірці (03.03 — 31.08.2026) це коштувало б березню 4,5% обсягу.
        """
        days = [d for d in (_iso(t["date"]) for t in self.tenders.values()) if d]
        if not days:
            return {}
        first, last = min(days), max(days)
        weights = fc.weekday_weights(days)
        return {month: fc.month_exposure(month, first, last, weights)
                for month in months}

    def _development(self, months: Sequence[str]) -> tuple[fc.Development, dict[str, float]]:
        """Крива розвитку договорів і повнота кожного місяця за нею.

        Знімки відтворюємо з сирих таблиць, а не з очищених угод: у
        ``clean_deals`` рішення про переможця лишається тільки там, де
        договору ще немає, — а нам потрібно саме те, що було видно раніше,
        коли договору не було ні в кого. Статистичне очищення на **частку**
        не впливає: крива — це відношення, і кілька викинутих рядків
        зсувають її в третьому знаку.
        """
        steps = self._steps()
        if not steps:
            return fc.Development(()), {}
        days = [d for d in (_iso(t["date"]) for t in self.tenders.values()) if d]
        cut = max(days)
        curve = self._curve_at(steps, cut)
        if not curve.known:
            return curve, {}
        out: dict[str, float] = {}
        for month in months:
            share = self._development_at(steps, curve, cut, month)
            if share is not None:
                out[month] = share
        return curve, out

    def _steps(self) -> dict[str, list[tuple[int, float]]]:
        """Для кожної закупівлі — коли саме кожна її гривня стала видимою."""
        contracts: dict[str, list[tuple[date, float]]] = defaultdict(list)
        awards: dict[str, list[tuple[date, float]]] = defaultdict(list)
        for row in self.data.contracts:
            when = _iso(row["signed"])
            if (row["edrpou"] and row["status"] in ("active", "terminated")
                    and when and (row["amount"] or 0) > 0
                    and (row.get("currency") or "UAH") == "UAH"):
                contracts[row["tender_id"]].append((when, float(row["amount"])))
        for row in self.data.awards:
            when = _iso(row["decided"])
            if (row["edrpou"] and row["status"] == "active"
                    and when and (row["amount"] or 0) > 0
                    and (row.get("currency") or "UAH") == "UAH"):
                awards[row["tender_id"]].append((when, float(row["amount"])))
        out: dict[str, list[tuple[int, float]]] = {}
        for tender_id, tender in self.tenders.items():
            published = _iso(tender["date"])
            if published:
                out[tender_id] = sorted(self._visibility(
                    published, contracts.get(tender_id, ()), awards.get(tender_id, ())))
        return out

    def _curve_at(self, steps: dict[str, list[tuple[int, float]]],
                  cut: date) -> fc.Development:
        """Крива, яку програма побудувала б, якби збір урвався ``cut``.

        Приросту, що стався б після зрізу, модель бачити не має — інакше
        зворотна перевірка поправки підглядала б у майбутнє й доводила сама
        себе.
        """
        cohorts = []
        for tender_id, tender in self.tenders.items():
            published = _iso(tender["date"])
            if not published or published > cut:
                continue
            age = (cut - published).days
            cohorts.append((age, [(lag, delta) for lag, delta in steps[tender_id]
                                  if lag <= age]))
        return fc.Development(cohorts)

    def _development_at(self, steps: dict[str, list[tuple[int, float]]],
                        curve: fc.Development, cut: date, month: str) -> float | None:
        """Яку частку грошей місяця видно станом на ``cut``.

        Вага закупівлі — її очікувана вартість: вона відома одразу, зокрема й
        там, де договору ще немає. Саме тому вона й годиться для місяця, у
        якому сума угод ще не дописана. Закупівлі без очікуваної вартості
        важать як одна — краще, ніж нуль.
        """
        del steps
        rows: list[tuple[float, int]] = []
        for tender in self.tenders.values():
            when = _iso(tender["date"])
            if when is None or tender["date"][:7] != month or when > cut:
                continue
            rows.append((float(tender["value"] or 0.0), (cut - when).days))
        if not rows:
            return None
        share = sum((value or 1.0) * curve.share(age) for value, age in rows)
        total = sum(value or 1.0 for value, _age in rows) or 1.0
        return min(share / total, 1.0)

    @staticmethod
    def _visible(steps: Sequence[tuple[int, float]], age: int) -> float:
        return sum(delta for lag, delta in steps if lag <= age)

    # --- чи працює сама поправка ------------------------------------------

    def _calibration(self, curve: fc.Development) -> tuple[Sheet, list[float], list[float]]:
        """Зворотна перевірка поправки на цій самій вибірці.

        Питання «а чи не вигадує програма ці 27%?» має відповідь із самих
        даних. Для кожного вже дозрілого місяця вдаємо, що збір урвався
        останнім його днем: будуємо криву розвитку **лише з того, що було
        видно тоді**, коригуємо місяць — і звіряємо з тим, скільки в ньому
        опинилося насправді. Це той самий код і те саме положення, у якому
        зараз перебуває останній місяць вибірки.

        Міряємо на сирій видимій сумі, а не на очищених угодах: перевіряємо
        механізм поправки, а не відсів викидів. Похибки відносні, тож різниця
        між двома мірами в чисельнику й знаменнику скорочується.
        """
        headers = ["Місяць", "Видно було на кінець місяця", "Скориговано тоді",
                   "Насправді виявилося", "Похибка без поправки", "Похибка з поправкою"]
        rows: list[list[Any]] = []
        raw_errors: list[float] = []
        fixed_errors: list[float] = []
        steps = self._steps()
        if not steps or not curve.known:
            return (headers, rows), raw_errors, fixed_errors
        # Дати розбираємо один раз: нижче вони потрібні в кожному місяці й на
        # кожній закупівлі, а нерозпізнана дата не має ані потрапляти в
        # порівняння, ані рахуватись у знаменнику.
        published = {tid: day for tid, t in self.tenders.items()
                     if (day := _iso(t["date"])) and tid in steps}
        if not published:
            return (headers, rows), raw_errors, fixed_errors
        days = sorted(published.values())
        first, last = days[0], days[-1]

        months = sorted({day.isoformat()[:7] for day in published.values()})
        for month in months:
            end = fc.month_start(fc.month_next(month)) - timedelta(days=1)
            # Місяць годиться, тільки якщо він і закінчився в межах вибірки, і
            # вже дозрів: недозрілий «насправді» сам був би недорахований.
            if end >= last or (last - end).days < curve.horizon:
                continue
            past = self._curve_at(steps, end)
            if not past.known:
                continue
            development = self._development_at(steps, past, end, month)
            if development is None:
                continue
            weights = fc.weekday_weights([d for d in days if d <= end])
            exposure = fc.month_exposure(month, first, end, weights)
            factor = exposure * development
            if factor < fc.MIN_COMPLETE:
                continue
            cohort = [(tid, day) for tid, day in published.items()
                      if day.isoformat()[:7] == month]
            seen = sum(self._visible(steps[tid], (end - day).days)
                       for tid, day in cohort if day <= end)
            truth = sum(self._visible(steps[tid], (last - day).days)
                        for tid, day in cohort)
            if truth <= 0:
                continue
            raw_errors.append(abs(seen / truth - 1))
            fixed_errors.append(abs(seen / factor / truth - 1))
            rows.append([month, round(seen, 2), round(seen / factor, 2), round(truth, 2),
                         round(seen / truth - 1, 4), round(seen / factor / truth - 1, 4)])
        return (headers, rows), raw_errors, fixed_errors

    @staticmethod
    def _visibility(published: date, contracts: Sequence[tuple[date, float]],
                    awards: Sequence[tuple[date, float]]) -> list[tuple[int, float]]:
        """Приріст видимої суми закупівлі за днями від оприлюднення.

        Правило видимості повторює :meth:`Analyzer._deals`: доки не з'явився
        жоден договір, гроші закупівлі — це рішення про переможця; щойно
        виходить перший договір, рішення перестають рахуватися зовсім.
        Через це приріст у день першого договору буває від'ємним — договір
        часто дешевший за рішення, — і крива розвитку має це витримувати.

        Рішення, ухвалені **після** першого договору, не видно взагалі:
        ``_deals`` дивиться не на дати, а на факт («є договір — рішення не
        рахуємо»), і крива має казати те саме. Раніше такі рішення й далі
        додавались, тож у багатолотовій закупівлі (лот 1 з договором, лот 2 ще
        з рішенням) крива бачила більше грошей, ніж бачить сам аналіз, і
        завищувала повноту місяця. На цій вибірці різниці немає — виміряно:
        серед 745 закупівель із договором жодне рішення не ухвалене пізніше за
        перший договір, — але на ринках із лотами вона з'явиться.
        """
        if not contracts:
            return [((when - published).days, amount) for when, amount in awards]
        first = min(when for when, _amount in contracts)
        events = [((when - published).days, amount)
                  for when, amount in awards if when <= first]
        drop = -sum(amount for when, amount in awards if when <= first)
        if drop:
            events.append(((first - published).days, drop))
        for when, amount in contracts:
            events.append(((when - published).days, amount))
        return events

    # --- прогноз ----------------------------------------------------------

    def outlook(self) -> None:
        horizon = max(1, int(getattr(self, "horizon", fc.DEFAULT_HORIZON)))
        monthly = self._monthly_series()
        if not monthly:
            self._step("Прогноз пропущено", 9)
            return
        months = [row[0] for row in monthly]
        exposure = self._exposure(months)
        curve, development = self._development(months)

        results: list[fc.Outcome] = []
        for name, unit, column, lagged in INDICATORS:
            results.append(fc.forecast(
                name, months, [row[column] for row in monthly], unit=unit,
                horizon=horizon, exposure=exposure,
                development=development if lagged else None))
        ours = self._ours_series(months)
        if ours is not None:
            results.append(fc.forecast(
                "Сума наших ТОВ", months, ours, unit="грн", horizon=horizon,
                exposure=exposure, development=development))

        self.report.add("Прогнозування", self._outlook_block(results, horizon))
        self.report.add("Прогнозування", self._method_block(results, curve))
        players = self._players_block(months, exposure, development, horizon)
        if players is not None:
            self.report.add("Прогнозування", players)
        self._step("Прогноз пораховано", 9)

    def _ours_series(self, months: Sequence[str]) -> list[float] | None:
        if not self.own:
            return None
        totals: Counter[str] = Counter()
        for deal in self.clean_deals:
            if not deal["ours"]:
                continue
            published = (self.tenders.get(deal["tender_id"], {}).get("date")
                         or deal["date"] or "")[:7]
            if published:
                totals[published] += deal["amount"]
        if sum(1 for month in months if totals.get(month)) < PLAYER_MIN_ACTIVE:
            return None
        return [round(totals.get(month, 0.0), 2) for month in months]

    # --- блоки ------------------------------------------------------------

    def _outlook_block(self, results: Sequence[fc.Outcome], horizon: int) -> Block:
        word = "місяць" if horizon == 1 else "місяці" if horizon < 5 else "місяців"
        block = Block(
            f"Прогноз на {horizon} {word}",
            "Кожен показник прогнозується окремо: програма перебирає десяток "
            "моделей, міряє їх на однакових згортках ковзним початком і бере "
            "ту, що виграла — з поправкою на ощадність. Числа наведені з "
            f"межами {fc.LEVEL:.0%}: справжнє значення має потрапити в них у "
            "чотирьох випадках із п'яти.")

        main = next((r for r in results if r.name == "Сума угод"), None)
        block.tiles = [("Горизонт", f"{horizon} {word}")]
        for result in results:
            if not result.ok or not result.points:
                block.tiles.append((result.name, "не рахується"))
                continue
            first = result.points[0]
            value = (compact(first.value) + " грн" if result.unit == "грн"
                     else count(round(first.value)))
            block.tiles.append((f"{result.name}: {first.month}", value))
        if main and main.ok:
            block.tiles.append((f"Разом за {horizon} {word}",
                                compact(main.total) + " грн"))
            if main.trend is not None:
                block.tiles.append(("Тренд у прогнозі",
                                    ("+" if main.trend >= 0 else "") +
                                    pct(main.trend) + "/міс"))
            if main.versus_last is not None:
                block.tiles.append(("Проти останнього місяця",
                                    ("+" if main.versus_last >= 0 else "") +
                                    pct(main.versus_last)))
            block.tiles.append(("Модель", main.model))
            block.tiles.append(("Якість (MASE)",
                                f"{main.mase:.2f}".replace(".", ",")
                                if main.mase is not None else "не перевірена"))
            block.tiles.append(("Перевірка", f"{main.grade} ({main.checks})"))
        block.notes = self._outlook_notes(results, horizon)
        for result in results:
            chart = self._outlook_chart(result)
            if chart is not None:
                block.charts.append(chart)
        if any(result.points for result in results):
            block.tables.append(("Прогноз по місяцях", self._points_sheet(results)))
        return block

    def _outlook_notes(self, results: Sequence[fc.Outcome], horizon: int) -> list[str]:
        notes: list[str] = []
        main = next((r for r in results if r.name == "Сума угод"), None)
        if main and main.ok and main.points:
            first = main.points[0]
            notes.append(
                f"Сума угод у {first.month}: {money(first.value)} грн, межі "
                f"{fc.LEVEL:.0%} — від {money(first.low)} до {money(first.high)} грн. "
                f"Модель — «{main.model}»"
                + (f" ({main.detail})" if main.detail else "") + ".")
            width = (first.high - first.low) / first.value if first.value else 0
            if width > 1.0:
                notes.append(
                    f"Інтервал ширший за саме число (±{pct(width / 2)} від нього). "
                    f"Це не хиба моделі, а стан даних: на такому короткому ряду "
                    f"місячний обсяг ринку впевнено не передбачається. Спиратися "
                    f"варто на порядок величини й напрям, а не на конкретну суму.")
        counts = next((r for r in results if r.name == "Закупівель"), None)
        if main and counts and main.ok and counts.ok and counts.points:
            people = counts.points[0].value
            if people > 0:
                notes.append(
                    f"Із прогнозів обсягу й кількості випливає середня угода "
                    f"{money(main.points[0].value / people)} грн — це відношення "
                    f"двох прогнозів, тож його похибка більша за кожен із них.")
        losers = [r.name for r in results if r.ok and r.grade == "не краща за наївну"]
        if losers:
            notes.append(
                "Гірші за наївний прогноз: " + ", ".join(losers).lower() +
                ". Тобто просте «буде як минулого місяця» помиляється менше за "
                "будь-яку з перебраних моделей — число тут лише орієнтир.")
        thin = [r for r in results if r.ok and r.grade == "перевірено побіжно"]
        if thin:
            checks = max(r.checks for r in thin)
            notes.append(
                f"Обрані моделі звіряли з фактом лише {checks} рази — стільки "
                f"дозволяє довжина ряду. Цього досить, щоб відсіяти явно гірші "
                f"(наївну й дрейф), і замало, щоб довести перевагу переможця: "
                f"MASE менший за одиницю тут добра ознака, а не доведення. "
                f"Побіжно перевірені: " + ", ".join(r.name.lower() for r in thin) + ".")
        blocked = [r for r in results if not r.ok and r.reason]
        for result in blocked:
            notes.append(f"{result.name}: {result.reason}")
        notes.append(
            "Прогноз продовжує те, що вже було, і не знає про майбутнє поза "
            "даними: зміну Особливостей, бюджетний рік, нову хвилю фінансування "
            "чи вихід великого гравця він не передбачить. " + self._season_note())
        return notes

    def _season_note(self) -> str:
        used = len({(t["date"] or "")[:7] for t in self.tenders.values() if t["date"]})
        if used >= 2 * fc.SEASON:
            return (f"Сезонність оцінюється: у вибірці {used} місяців, тобто "
                    f"понад два повні роки.")
        return (f"Сезонність тут не оцінюється взагалі: для неї потрібно "
                f"{2 * fc.SEASON} місяців, а у вибірці {used}. Грудневий сплеск "
                f"чи січневе затишшя програма побачити не може — зберіть довший "
                f"період, і сезонні моделі увімкнуться самі.")

    def _outlook_chart(self, result: fc.Outcome) -> ChartData | None:
        if not result.ok or not result.points:
            return None
        history = result.history
        # Місяці історії та прогнозу можуть перетнутися: якщо останній місяць
        # відкинуто як надто неповний, прогноз починається саме з нього. Тому
        # вісь будуємо об'єднанням, а не склеюванням — інакше той самий місяць
        # стояв би на графіку двічі.
        labels = sorted({row.month for row in history}
                        | {point.month for point in result.points})
        slot = {month: i for i, month in enumerate(labels)}
        empty: list[float | None] = [None] * len(labels)

        def lay(pairs) -> list[float | None]:
            line = list(empty)
            for month, value in pairs:
                line[slot[month]] = value
            return line

        fact = lay((row.month, row.value) for row in history)
        # Ряд із поправкою малюємо **суцільним по всій історії**, а не самими
        # виправленими точками. Точками він давав окрему цятку над обривом
        # лінії факту, і графік читався як помилка: прогноз ніби починався не
        # звідти, де закінчився факт. Насправді він і має починатися вище — з
        # оцінки повного місяця, — і суцільна лінія це показує: два ряди
        # збігаються всюди, крім неповних місяців, і саме від скоригованого
        # ряду відходить прогноз.
        corrected = [row for row in history if abs(row.adjusted - row.value) > 0.005]
        adjusted = lay((row.month, row.adjusted) for row in history)
        # Прогноз малюємо від останньої взятої в підгонку точки, інакше лінія
        # висить у повітрі й читається як окремий, ні з чим не пов'язаний ряд.
        used = [row for row in history if row.used]
        bridge = [(used[-1].month, used[-1].adjusted)] if used else []
        line = lay(bridge + [(p.month, p.value) for p in result.points])
        low = lay(bridge + [(p.month, p.low) for p in result.points])
        high = lay(bridge + [(p.month, p.high) for p in result.points])

        # Скоригований ряд іде **першим**, тобто малюється під фактом. Зверху
        # він накривав би факт усюди, де вони збігаються, і на графіку, де
        # виправлено лише один місяць, від лінії факту не лишалося б нічого,
        # хоча легенда її обіцяє. Знизу все навпаки: видно факт, а поправка
        # виглядає рівно там, де вона є.
        series: list[Series] = []
        if corrected:
            series.append(Series("Оцінка повного місяця", labels, adjusted))
        series.append(Series("Факт", labels, fact))
        series.append(Series("Прогноз", labels, line))
        series.append(Series(f"Нижня межа {fc.LEVEL:.0%}", labels, low))
        series.append(Series(f"Верхня межа {fc.LEVEL:.0%}", labels, high))
        join = ""
        if corrected and used and abs(used[-1].adjusted - used[-1].value) > 0.005:
            join = (f" Прогноз починається не з {money(used[-1].value)}, а з "
                    f"{money(used[-1].adjusted)}: {used[-1].month} видно лише на "
                    f"{pct(used[-1].completeness, 0)}, і модель має продовжувати "
                    f"місяць, а не його видиму частину.")
        return ChartData(
            f"{result.name}: факт і прогноз", "line", series,
            unit=result.unit, money_axis=result.unit == "грн",
            hint=f"Модель «{result.model}»"
                 + (f", {result.detail}" if result.detail else "")
                 + (f"; MASE {result.mase:.2f}".replace(".", ",")
                    if result.mase is not None else "; без перевірки")
                 + ". Де два перші ряди розходяться — там місяць ще неповний."
                 + join)

    def _points_sheet(self, results: Sequence[fc.Outcome]) -> Sheet:
        headers = ["Показник", "Місяць", "Прогноз", f"Нижня межа {fc.LEVEL:.0%}",
                   f"Верхня межа {fc.LEVEL:.0%}", "Модель", "MASE", "Звірок",
                   "Перевірка"]
        rows: list[list[Any]] = []
        for result in results:
            for point in result.points:
                rows.append([result.name, point.month, point.value, point.low,
                             point.high, result.model,
                             result.mase if result.mase is not None else "",
                             result.checks, result.grade])
        return headers, rows

    def _method_block(self, results: Sequence[fc.Outcome],
                      curve: fc.Development) -> Block:
        block = Block(
            "Звідки взялися числа",
            "Прогноз рахується не з того, що видно, а з того, що буде видно, "
            "коли дані допишуться. Тут показано обидві поправки й перевірку "
            "моделей — щоб число з попереднього блоку можна було перевірити, "
            "а не просто прийняти.")

        main = next((r for r in results if r.history), None)
        if main:
            thin = [row for row in main.history if row.completeness < 0.999]
            if thin:
                worst = min(thin, key=lambda row: row.completeness)
                fixed = (f" — {money(worst.value)} грн замість "
                         f"{money(worst.adjusted)} грн, які там будуть"
                         if worst.adjusted > worst.value else "")
                block.notes.append(
                    f"Неповних місяців {len(thin)}. Найменше даних у "
                    f"{worst.month}: видно {pct(worst.completeness)}{fixed}. "
                    f"Без цієї поправки модель побачила б у кінці ряду спад, "
                    f"якого немає.")
            dropped = [row.month for row in main.history if not row.used]
            if dropped:
                block.notes.append(
                    "У підгонку не взято: " + ", ".join(dropped) +
                    f" — там лишилося менше {fc.MIN_COMPLETE:.0%} даних, а "
                    f"домножувати спостережене більш ніж удвічі означає "
                    f"вигадувати, а не коригувати.")
        if curve.known:
            block.notes.append(
                f"Крива розвитку побудована на {count(curve.cohort)} закупівлях, "
                f"які вже дозріли (від оприлюднення минуло щонайменше "
                f"{curve.horizon} днів). У день оприлюднення видно "
                f"{pct(curve.share(0))} грошей закупівлі, через тиждень — "
                f"{pct(curve.share(7))}, через два — {pct(curve.share(14))}, "
                f"через три — {pct(curve.share(21))}.")
            block.charts.append(ChartData(
                "Крива розвитку договорів", "line",
                [Series("Видно суми", [str(row[0]) for row in curve.table()],
                        [round(row[1] * 100, 1) for row in curve.table()])],
                unit="%", money_axis=False,
                hint="Скільки відсотків остаточної суми закупівлі видно через "
                     "стільки днів після її оприлюднення. Саме на цю криву "
                     "діляться останні місяці ряду."))
            block.tables.append(("Крива розвитку договорів", (
                ["Днів від оприлюднення", "Видно суми"],
                [[row[0], row[1]] for row in curve.table()])))
        else:
            block.notes.append(
                f"Криву розвитку договорів побудувати нема на чому: дозрілих "
                f"закупівель {count(curve.cohort)}, а треба щонайменше "
                f"{fc.MIN_COHORT}. Тому поправка на затримку договорів не "
                f"застосовується взагалі — свіжі місяці лишаються такими, "
                f"якими їх видно, і вони занижені.")
        block.tables.append(("Повнота місяців", self._completeness_sheet(results)))
        check, raw_errors, fixed_errors = self._calibration(curve)
        if check[1]:
            worse = sum(raw_errors) / len(raw_errors)
            better = sum(fixed_errors) / len(fixed_errors)
            verdict = ("і це помітно краще" if better < worse * 0.75
                       else "і це майже не змінює справи" if better < worse * 1.1
                       else "і це **гірше**, ніж без неї")
            block.notes.append(
                f"Поправку перевірено на цій самій вибірці. Для кожного вже "
                f"дозрілого місяця програма вдала, що збір урвався його останнім "
                f"днем, побудувала криву лише з того, що було видно тоді, і "
                f"звірила скориговане число з тим, що в місяці опинилося "
                f"насправді. {_on_months(len(check[1]))} середня "
                f"похибка без поправки — {pct(worse, 0)} (і завжди в мінус: "
                f"місяць недорахований), з поправкою — {pct(better, 0)}, "
                f"{verdict}. Таблиця нижче показує кожен місяць окремо.")
            block.tables.append(("Перевірка поправки", check))
        scores = self._scores_sheet(results)
        if scores[1]:
            block.tables.append(("Перевірка моделей", scores))
        return block

    def _completeness_sheet(self, results: Sequence[fc.Outcome]) -> Sheet:
        headers = ["Місяць", "Показник", "Спостережено", "Частка місяця у вибірці",
                   "Розвиток договорів", "Повнота", "З поправкою", "У підгонці"]
        rows: list[list[Any]] = []
        for result in results:
            for row in result.history:
                if row.completeness >= 0.999 and row.used:
                    continue
                rows.append([row.month, result.name, row.value,
                             round(row.exposure, 4), round(row.development, 4),
                             round(row.completeness, 4), row.adjusted,
                             "так" if row.used else "ні"])
        if not rows:                       # усі місяці повні — так теж буває
            first = results[0] if results else None
            if first:
                for row in first.history:
                    rows.append([row.month, first.name, row.value,
                                 round(row.exposure, 4), round(row.development, 4),
                                 round(row.completeness, 4), row.adjusted, "так"])
        return headers, rows

    def _scores_sheet(self, results: Sequence[fc.Outcome]) -> Sheet:
        headers = ["Показник", "Модель", "Рішень у моделі", "MASE",
                   "Звірок з фактом", "Обрана"]
        rows: list[list[Any]] = []
        for result in results:
            for score in sorted(result.scores,
                                key=lambda s: (s.mase is None, s.mase or 0.0)):
                rows.append([result.name, score.model, score.params,
                             score.mase if score.mase is not None else "",
                             score.checks, "так" if score.chosen else ""])
        return headers, rows

    # --- прогноз по гравцях -----------------------------------------------

    def _players_block(self, months: Sequence[str], exposure: dict[str, float],
                       development: dict[str, float], horizon: int) -> Block | None:
        """Скільки візьме кожен помітний гравець у наступні місяці.

        Ряд однієї компанії на порядок коротший і дірявіший за ринковий:
        більшість гравців з'являється не щомісяця. Тому тут навмисно
        обмежений набір моделей і жорсткіший поріг придатності — компанія без
        трьох місяців з грошима у таблицю не потрапляє взагалі.
        """
        by_month: dict[str, Counter[str]] = defaultdict(Counter)
        for deal in self.clean_deals:
            published = (self.tenders.get(deal["tender_id"], {}).get("date")
                         or deal["date"] or "")[:7]
            if published:
                by_month[deal["edrpou"]][published] += deal["amount"]

        wanted = [edrpou for edrpou, _cell in self.ranking[:TOP_PLAYERS]]
        wanted += [edrpou for edrpou in self.own
                   if edrpou in by_month and edrpou not in wanted]
        rows: list[list[Any]] = []
        ahead = ""
        for edrpou in wanted:
            series = by_month.get(edrpou) or Counter()
            if sum(1 for month in months if series.get(month)) < PLAYER_MIN_ACTIVE:
                continue
            result = fc.forecast(
                self.names.get(edrpou, edrpou), months,
                [round(series.get(month, 0.0), 2) for month in months],
                unit="грн", horizon=horizon, exposure=exposure,
                development=development, quick=True)
            if not result.ok or not result.points:
                continue
            ahead = ahead or result.points[0].month
            rows.append([
                edrpou, self.names.get(edrpou, ""),
                "так" if self.is_ours(edrpou) else "",
                self.rank_of.get(edrpou, 0), round(result.last, 2),
                round(result.points[0].value, 2), round(result.points[0].low, 2),
                round(result.points[0].high, 2), round(result.total, 2),
                result.model, result.mase if result.mase is not None else "",
                result.grade,
            ])
        if not rows:
            return None
        # Порядок — за розміром прогнозу, а не за місцем у рейтингу: смуги на
        # графіку інакше стрибають угору-вниз, і читати їх неможливо. Місце
        # лишається колонкою, тож із таблиці воно нікуди не дівається.
        rows.sort(key=lambda row: -row[5])
        block = Block(
            "Прогноз по гравцях",
            "Той самий рушій, але на ряду однієї компанії. Ряд гравця коротший "
            "і рідший за ринковий, тож моделі тут лише прості, а колонка "
            "«Надійний» порожня частіше, ніж заповнена — це чесна ознака того, "
            "що для окремої компанії місячних даних замало.")
        sure = sum(1 for row in rows if row[11].startswith("перевірено"))
        block.tiles = [
            ("Гравців у прогнозі", count(len(rows))),
            ("З них перевірку пройшли", count(sure)),
            (f"Разом за {horizon} міс.", compact(sum(row[8] for row in rows)) + " грн"),
        ]
        block.notes.append(
            f"Ковзну перевірку пройшли {sure} з {len(rows)} гравців. Для решти "
            f"прогноз показано, але спиратися на нього не можна: модель не "
            f"перемогла наївного «як минулого місяця». Це очікувано — місячний "
            f"ряд однієї компанії наполовину складається з нулів.")
        top = [row for row in rows if row[5] > 0][:12]
        if top:
            block.charts.append(ChartData(
                f"Прогноз на {ahead} по гравцях", "hbar",
                [Series("Прогноз", [self._short_name(row[0]) for row in top],
                        [row[5] for row in top],
                        accent={i for i, row in enumerate(top) if row[2]})],
                unit="грн",
                hint="Наші ТОВ виділені кольором. Смуга — точковий прогноз; "
                     "межі дивіться в таблиці нижче."))
        block.tables.append(("Прогноз по гравцях", (
            ["ЄДРПОУ", "Компанія", "Наша", "Місце", "Остання сума, грн",
             "Прогноз, грн", f"Нижня межа {fc.LEVEL:.0%}, грн",
             f"Верхня межа {fc.LEVEL:.0%}, грн", f"Разом за {horizon} міс., грн",
             "Модель", "MASE", "Перевірка"], rows)))
        return block
