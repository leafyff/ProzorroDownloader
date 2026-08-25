"""Порівняльна частина аналітики: товари, ТМ, канали й сторони гравців.

Продовження :mod:`app.core.players`. Профілі гравців уже складені — тут із них
і з очищених угод виводяться відповіді про товар: під якою маркою хто заходить,
звідки той товар узагалі береться і чим наші ТОВ сильніші або слабші за
конкурентів. Там, де цифр для висновку не вистачає, звіт прямо каже про це,
а не вигадує зв'язок.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from . import brands as tm
from .players import MIN_FACTS, TOP_RIVALS
from .report import (
    Block, ChartData, NEEDS_AI, Profile, Series, Sheet,
    compact, count, median, money, pct, share,
)


class BenchmarkMixin:
    """Товари, ТМ, канали постачання та порівняння. Домішка до ``Analyzer``."""

    # --- 4. товари й торгові марки ---------------------------------------

    def brands(self) -> None:
        block = Block(
            "Товари та торгові марки",
            "ТМ визначається за текстом позицій і назвою предмета закупівлі за "
            "довідником data/brands.json — його можна доповнювати власними марками.")

        brand_tenders: dict[str, set[str]] = defaultdict(set)
        brand_items: Counter[str] = Counter()
        brand_money: Counter[str] = Counter()
        brand_companies: dict[str, Counter[str]] = defaultdict(Counter)
        for tender_id in self.tenders:
            found = self.brands_of.get(tender_id) or []
            for name in found:
                brand_tenders[name].add(tender_id)
            for item in self.items_by_tender.get(tender_id, ()):
                for name in self._brands_of(item["description"]):
                    brand_items[name] += 1
        for deal in self.clean_deals:
            found = self.brands_of.get(deal["tender_id"]) or []
            if not found:
                continue
            # Гроші закупівлі зараховуємо головній ТМ, інакше сума ринку
            # роздувалася б у стільки разів, скільки марок згадано в описі.
            brand_money[found[0]] += deal["amount"]
            brand_companies[found[0]][deal["edrpou"]] += deal["amount"]

        known = set(brand_tenders) | set(brand_money)
        rows: list[list[Any]] = []
        for name in sorted(known, key=lambda b: -brand_money.get(b, 0)):
            players = brand_companies.get(name, Counter())
            leader = players.most_common(1)[0] if players else ("", 0.0)
            own_note = tm.own_tm().get(name) or {}
            rows.append([
                name, len(brand_tenders.get(name, ())), brand_items.get(name, 0),
                round(brand_money.get(name, 0.0), 2),
                round(share(brand_money.get(name, 0.0), self.total_market), 4),
                len(players), self.names.get(leader[0], leader[0]),
                round(share(leader[1], sum(players.values())), 4) if players else None,
                "так" if len(players) == 1 and players else "",
                own_note.get("owner", ""),
                ", ".join(tm.distributors().get(name, [])) or NEEDS_AI,
            ])
        block.tables.append(("Торгові марки", (
            ["Торгова марка", "Закупівель", "Позицій", "Сума угод, грн", "Частка ринку",
             "Компаній із цією ТМ", "Головний гравець", "Його частка в ТМ",
             "Ексклюзив у вибірці", "Власник ТМ за довідником", "Дистриб'ютори"], rows)))

        if brand_money:
            top = brand_money.most_common(TOP_RIVALS)
            block.charts.append(ChartData(
                "Торгові марки за сумою угод", "hbar",
                [Series("Сума", [b for b, _v in top], [v for _b, v in top])], unit="грн"))
            block.charts.append(ChartData(
                "Частки ТМ", "pie",
                [Series("Сума", [b for b, _v in brand_money.most_common(9)],
                        [v for _b, v in brand_money.most_common(9)])], unit="грн"))

        matrix = self._brand_matrix(brand_companies)
        if matrix[1]:
            block.tables.append(("ТМ по компаніях", matrix))

        types = self._product_types()
        if types[1]:
            block.tables.append(("Товари ринку", types))
            top = types[1][:TOP_RIVALS]
            block.charts.append(ChartData(
                "Найпоширеніші товари", "hbar",
                [Series("Позицій", [r[0][:34] for r in top], [r[1] for r in top])],
                unit="шт", money_axis=False))

        catalog = self._catalog_block()
        if catalog:
            block.tables.extend(catalog["tables"])
            block.charts.extend(catalog["charts"])
            block.notes.extend(catalog["notes"])

        texts = [item["description"] for item in self.data.items]
        texts += [t["title"] for t in self.tenders.values()]
        new_brands = tm.candidates(texts)
        if new_brands:
            block.tables.append(("Кандидати в ТМ", (
                ["Слово", "Згадок"], [[word, n] for word, n in new_brands])))
            block.notes.append(
                "«Кандидати в ТМ» — латинські слова з описів, яких немає в довіднику. "
                "Якщо серед них є справжня марка, допишіть її у data/brands.json — "
                "наступний аналіз рахуватиме її нарівні з рештою.")
        if not known:
            block.notes.append(
                "У цій вибірці жодної ТМ із довідника не знайдено: або це не ІТ-ринок, "
                "або описи позицій не містять назв марок. " + NEEDS_AI +
                " за змістом тендерної документації.")
        self.report.add("Товари і ТМ", block)
        self._step("Товари й ТМ пораховано", 5)

    def _brand_matrix(self, brand_companies: dict[str, Counter]) -> Sheet:
        totals = {name: sum(players.values()) for name, players in brand_companies.items()}
        brands = [name for name, _v in sorted(totals.items(), key=lambda kv: -kv[1])[:12]]
        players: Counter[str] = Counter()
        for name in brands:
            players.update(brand_companies[name])
        companies = [code for code, _v in players.most_common(15)]
        headers = ["Компанія", "ЄДРПОУ", "Наша"] + brands + ["Разом, грн"]
        rows: list[list[Any]] = []
        for code in companies:
            line: list[Any] = [self.names.get(code, code), code,
                               "так" if self.is_ours(code) else ""]
            total = 0.0
            for name in brands:
                value = brand_companies[name].get(code, 0.0)
                total += value
                line.append(round(value, 2) if value else None)
            line.append(round(total, 2))
            rows.append(line)
        return headers, rows

    def _product_types(self) -> Sheet:
        cells: dict[str, dict] = defaultdict(
            lambda: {"items": 0, "tenders": set(), "qty": 0.0, "prices": [], "brands": Counter()})
        for item in self.clean_items:
            kind = self._product_type(item["description"])
            if not kind:
                continue
            cell = cells[kind]
            cell["items"] += 1
            cell["tenders"].add(item["tender_id"])
            cell["qty"] += item["quantity"] or 0
            if item["unit_price"]:
                cell["prices"].append(item["unit_price"])
            cell["brands"].update(self._brands_of(item["description"]))
        headers = ["Товар", "Позицій", "Закупівель", "Кількість", "Медіанна ціна, грн",
                   "Мін. ціна, грн", "Макс. ціна, грн", "Головні ТМ"]
        rows = []
        for kind, cell in sorted(cells.items(), key=lambda kv: -kv[1]["items"]):
            prices = cell["prices"]
            rows.append([kind, cell["items"], len(cell["tenders"]), round(cell["qty"], 2),
                         round(median(prices), 2) if prices else None,
                         round(min(prices), 2) if prices else None,
                         round(max(prices), 2) if prices else None,
                         ", ".join(f"{b} ({n})" for b, n in cell["brands"].most_common(3))])
        return headers, rows

    def _catalog_block(self) -> dict | None:
        """Картки е-каталогу: єдине місце, де бренд і ціна лежать у явному полі."""
        products = self.data.products
        if not products:
            return None
        cells: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "low": [], "high": [], "vendors": Counter(),
                     "specs": [], "images": []})
        for row in products:
            brand = row["brand"] or "не вказано"
            cell = cells[brand]
            cell["n"] += 1
            if row["price_low"]:
                cell["low"].append(row["price_low"])
            if row["price_high"]:
                cell["high"].append(row["price_high"])
            if row["vendor"]:
                cell["vendors"][row["vendor"]] += 1
            cell["specs"].append(row["n_specs"])
            cell["images"].append(row["n_images"])
        headers = ["Бренд", "Карток", "Медіана нижнього квартиля, грн",
                   "Медіана верхнього квартиля, грн", "Середньо характеристик",
                   "Середньо фото", "Постачальники карток"]
        rows = []
        for brand, cell in sorted(cells.items(), key=lambda kv: -kv[1]["n"]):
            rows.append([brand, cell["n"],
                         round(median(cell["low"]), 2) if cell["low"] else None,
                         round(median(cell["high"]), 2) if cell["high"] else None,
                         round(sum(cell["specs"]) / max(cell["n"], 1), 1),
                         round(sum(cell["images"]) / max(cell["n"], 1), 1),
                         ", ".join(f"{v} ({n})" for v, n in cell["vendors"].most_common(3))])
        top = rows[:TOP_RIVALS]
        charts = [ChartData(
            "Бренди в е-каталозі: карток", "hbar",
            [Series("Карток", [r[0][:30] for r in top], [r[1] for r in top])],
            unit="шт", money_axis=False)]
        prices = [r for r in rows if r[2]][:TOP_RIVALS]
        if prices:
            charts.append(ChartData(
                "Цінове позиціонування брендів у каталозі", "hbar",
                [Series("Нижній квартиль", [r[0][:30] for r in prices], [r[2] for r in prices]),
                 Series("Верхній квартиль", [r[0][:30] for r in prices],
                        [r[3] or 0 for r in prices])], unit="грн"))
        return {"tables": [("Бренди е-каталогу", (headers, rows))], "charts": charts,
                "notes": [f"У книзі є {len(products)} карток е-каталогу — це єдине джерело, "
                          f"де бренд, ціновий діапазон і постачальник картки вказані явно."]}

    # --- 5. канали постачання --------------------------------------------

    def supply(self) -> None:
        block = Block(
            "Канали постачання",
            "З відкритих даних закупівлі прямо не видно, у кого гравець бере товар. "
            "Але видно сигнали: чи заходить він з ТМ, якої більше ні в кого немає, "
            "чи подає авторизаційні листи виробника, і хто веде картку його товару "
            "в е-каталозі. З них і складається висновок.")

        brand_companies: dict[str, Counter[str]] = defaultdict(Counter)
        for deal in self.clean_deals:
            found = self.brands_of.get(deal["tender_id"]) or []
            if found:
                brand_companies[found[0]][deal["edrpou"]] += deal["amount"]
        catalog_vendors: dict[str, Counter[str]] = defaultdict(Counter)
        for row in self.data.products:
            if row["brand"] and row["vendor"]:
                catalog_vendors[row["brand"]][row["vendor"]] += 1

        headers = ["ЄДРПОУ", "Компанія", "Наша", "Основні ТМ", "Ексклюзивні ТМ",
                   "Власна ТМ за довідником", "Авторизаційних листів", "Сертифікатів",
                   "Постачальник картки в е-каталозі", "Дистриб'ютори з довідника",
                   "Висновок про канал"]
        rows: list[list[Any]] = []
        for profile in [*self.report.ours, *self.report.competitors]:
            if not profile.brands and not profile.signed:
                continue
            top_brands = [name for name, _n in profile.brands[:4]]
            exclusive = [name for name in top_brands
                         if len(brand_companies.get(name, {})) == 1
                         and profile.edrpou in brand_companies.get(name, {})]
            own_marks = [f"{name} ({(tm.own_tm().get(name) or {}).get('owner') or 'власна'})"
                         for name in top_brands if name in tm.own_tm()]
            vendors: Counter[str] = Counter()
            for name in top_brands:
                vendors.update(catalog_vendors.get(name, {}))
            known = [d for name in top_brands for d in tm.distributors().get(name, [])]

            verdict = self._supply_verdict(profile, exclusive, own_marks, vendors, known)
            rows.append([
                profile.edrpou, profile.name, "так" if profile.is_ours else "",
                ", ".join(top_brands), ", ".join(exclusive), ", ".join(own_marks),
                profile.authorizations, profile.certificates,
                ", ".join(f"{v} ({n})" for v, n in vendors.most_common(3)),
                ", ".join(sorted(set(known))), verdict,
            ])
        block.tables.append(("Канали постачання", (headers, rows)))

        # Хто ще возить ту саму ТМ — найпряміший натяк на спільний канал.
        shared_headers = ["Торгова марка", "Компаній", "Гравці (сума угод, грн)",
                          "Ексклюзив", "Власник ТМ за довідником"]
        shared_rows = []
        for name, players in sorted(brand_companies.items(),
                                    key=lambda kv: -sum(kv[1].values())):
            shared_rows.append([
                name, len(players),
                "; ".join(f"{self.names.get(code, code)} — {money(value)}"
                          for code, value in players.most_common(6)),
                "так" if len(players) == 1 else "",
                (tm.own_tm().get(name) or {}).get("owner", ""),
            ])
        if shared_rows:
            block.tables.append(("Хто возить ту саму ТМ", (shared_headers, shared_rows)))

        kinds: Counter[str] = Counter()
        for docs in self.docs_by_owner.values():
            for doc in docs:
                kinds[tm.document_kind(doc["title"], doc["format"]) or "не класифіковано"] += 1
        if kinds:
            block.charts.append(ChartData(
                "Що учасники кладуть у пропозиції", "hbar",
                [Series("Файлів", [k for k, _n in kinds.most_common(12)],
                        [n for _k, n in kinds.most_common(12)])],
                unit="шт", money_axis=False,
                hint="Класифікація за назвою файлу. Авторизаційні листи й сертифікати — "
                     "найкорисніші для розуміння каналу."))
            block.tables.append(("Типи документів", (
                ["Тип документа", "Файлів"], [[k, n] for k, n in kinds.most_common()])))
        else:
            block.notes.append(
                "У книзі немає реєстру документів або не заповнений власник файлу — "
                "сигнал про авторизацію виробника недоступний.")

        block.notes.append(
            "Конкретний постачальник або дистриб'ютор у закупівельних даних не "
            "публікується. Там, де в колонці «Дистриб'ютори» стоїть «" + NEEDS_AI +
            "», канал з цифр не виводиться: потрібен розбір змісту документів "
            "(авторизаційні листи, специфікації) і ринкових джерел. Відомі вам "
            "зв'язки «ТМ → дистриб'ютор» можна внести у data/brands.json — "
            "аналіз почне їх показувати.")
        self.report.add("Постачання", block)
        self._step("Канали постачання оцінено", 6)

    def _supply_verdict(self, profile: Profile, exclusive: list[str], own_marks: list[str],
                        vendors: Counter, known: list[str]) -> str:
        if own_marks:
            return f"власна ТМ ({own_marks[0].split(' (')[0]}) — товар власного бренду"
        if exclusive and profile.top_brand_share >= 0.5:
            return (f"ексклюзивний канал: ТМ {exclusive[0]} у вибірці більше ніхто "
                    f"не возить — ознака власної марки або прямого контракту")
        if profile.authorizations:
            return f"є авторизаційні листи виробника ({profile.authorizations}) — офіційний канал"
        if known:
            return f"за довідником: {', '.join(sorted(set(known))[:3])}"
        if vendors:
            return f"картку товару веде {vendors.most_common(1)[0][0]}"
        return NEEDS_AI

    # --- 6. порівняння ТМ і сторони ---------------------------------------

    def compare(self) -> None:
        block = Block(
            "Порівняння наших ТОВ із конкурентами",
            "Сильні та слабкі сторони виводяться з вимірюваних ознак: широти "
            "портфеля ТМ, монобрендовості, сертифікатів і авторизацій у поданих "
            "файлах, цінової поведінки, результативності, географії та стійкості "
            "бази замовників.")

        market = {
            "discount": self.market_discount,
            "regions": len({d["region"] for d in self.clean_deals if d["region"]}),
            "brands": median([len(p.brands) for p in self.report.competitors] or [0]),
            "check": median([d["amount"] for d in self.clean_deals] or [0]),
        }
        everyone = [*self.report.ours, *self.report.competitors]
        for profile in everyone:
            profile.strengths, profile.weaknesses = self._sides(profile, market)

        headers = ["ЄДРПОУ", "Компанія", "Наша", "Основна ТМ", "Частка основної ТМ",
                   "ТМ у портфелі", "Власна ТМ", "Сертифікатів", "Авторизацій",
                   "Результативність", "Дисконт", "Областей", "Замовників",
                   "Повторні замовники", "Сильні сторони", "Слабкі сторони",
                   "Що не виводиться з цифр"]
        rows = []
        for profile in everyone:
            rows.append([
                profile.edrpou, profile.name, "так" if profile.is_ours else "",
                profile.top_brand, round(profile.top_brand_share, 4), len(profile.brands),
                (tm.own_tm().get(profile.top_brand) or {}).get("owner", ""),
                profile.certificates, profile.authorizations,
                round(profile.win_rate, 4) if profile.win_rate is not None else None,
                round(profile.discount, 4) if profile.discount is not None else None,
                profile.n_regions, profile.n_buyers, round(profile.repeat_share, 4),
                "; ".join(profile.strengths) or "—",
                "; ".join(profile.weaknesses) or "—",
                NEEDS_AI,
            ])
        block.tables.append(("Сильні та слабкі сторони", (headers, rows)))

        ours = [p for p in self.report.ours if p.signed > 0 or p.n_bids > 0]
        rivals = self.report.competitors[:TOP_RIVALS]
        if rivals:
            # Назви компаній довгі, тож порівняння йде смугами: у вертикальних
            # стовпчиках підписи довелося б різати до трьох літер.
            line = [*ours, *rivals]
            labels = [self._short_name(p.edrpou, 30) for p in line]
            accent = set(range(len(ours)))
            block.charts.append(ChartData(
                "Ширина портфеля ТМ", "hbar",
                [Series("ТМ у портфелі", labels, [len(p.brands) for p in line],
                        accent=accent)], unit="шт", money_axis=False))
            block.charts.append(ChartData(
                "Цінова поведінка: медіанний дисконт", "hbar",
                [Series("Дисконт", labels, [(p.discount or 0) * 100 for p in line],
                        accent=accent)], unit="%", money_axis=False,
                hint="Наскільки гравець зазвичай опускається від очікуваної вартості."))
            block.charts.append(ChartData(
                "Підтверджувальні документи у пропозиціях", "hbar",
                [Series("Сертифікати", labels, [p.certificates for p in line], accent=accent),
                 Series("Авторизації", labels, [p.authorizations for p in line],
                        accent=accent)],
                unit="шт", money_axis=False))

        block.notes.append(
            "Що з цифр не виводиться взагалі: власне виробництво й локалізація, умови "
            "дистрибуції та кредитні ліміти, глибина складу, сервісна мережа, якість "
            "передпродажної підтримки, наявність сертифікатів, які подавалися в "
            "паперовому вигляді. " + NEEDS_AI + " — за змістом тендерної документації "
            "та зовнішніми джерелами.")
        if not self.own:
            block.notes.append("Наші ЄДРПОУ не задані — порівняння показує лише конкурентів.")
        self.report.add("Порівняння", block)
        self._step("Порівняння складено", 7)

    def _sides(self, profile: Profile, market: dict) -> tuple[list[str], list[str]]:
        strong: list[str] = []
        weak: list[str] = []
        own_note = tm.own_tm().get(profile.top_brand)
        brand_hits = sum(n for _b, n in profile.brands)

        if len(profile.brands) >= max(5, market["brands"] + 2):
            strong.append(f"портфель дистрибуції: {len(profile.brands)} ТМ")
        elif profile.brands and len(profile.brands) <= 2 and not own_note \
                and brand_hits >= MIN_FACTS:
            weak.append(f"вузький портфель: {len(profile.brands)} ТМ")

        if profile.top_brand_share >= 0.6 and profile.top_brand and brand_hits >= MIN_FACTS:
            if own_note:
                strong.append(f"монобрендовість із власною ТМ {profile.top_brand} "
                              f"({pct(profile.top_brand_share)} позицій)")
            else:
                strong.append(f"фокус на ТМ {profile.top_brand} "
                              f"({pct(profile.top_brand_share)} позицій)")
                if profile.top_brand_share >= 0.85:
                    weak.append(f"залежність від однієї ТМ ({profile.top_brand})")
        if profile.certificates >= 2:
            strong.append(f"наявність сертифікатів ({profile.certificates} файлів)")
        elif profile.n_bids and not profile.certificates:
            weak.append("сертифікатів у поданих файлах не знайдено")
        if profile.authorizations:
            strong.append(f"авторизація виробника ({profile.authorizations} листів)")

        if profile.discount is not None and market["discount"] is not None:
            if profile.discount > market["discount"] + 0.02:
                strong.append(f"цінова конкурентність: дисконт {pct(profile.discount)} "
                              f"проти {pct(market['discount'])} по ринку")
            elif profile.discount < market["discount"] - 0.02:
                weak.append(f"слабка цінова позиція: дисконт {pct(profile.discount)} "
                            f"проти {pct(market['discount'])} по ринку")
        if profile.win_rate is not None and profile.n_bids >= 3:
            if profile.win_rate >= 0.5:
                strong.append(f"висока результативність на торгах ({pct(profile.win_rate)})")
            elif profile.win_rate < 0.25:
                weak.append(f"низька результативність на торгах ({pct(profile.win_rate)})")
        if profile.n_regions >= 5:
            strong.append(f"широка географія: {profile.n_regions} областей")
        elif profile.n_regions and profile.n_regions <= 2 and market["regions"] > 5:
            weak.append(f"вузька географія: {profile.n_regions} обл.")
        if profile.repeat_share >= 0.4 and profile.n_contracts >= 5:
            strong.append(f"стабільна база замовників ({pct(profile.repeat_share)} повторних)")
        if profile.n_buyers == 1 and profile.n_contracts >= 3:
            weak.append("залежність від одного замовника")
        if profile.trend is not None:
            if profile.trend > 0.3:
                strong.append(f"зростання ({pct(profile.trend, 0)} у другій половині періоду)")
            elif profile.trend < -0.3:
                weak.append(f"спад ({pct(profile.trend, 0)} у другій половині періоду)")
        if profile.reporting_share >= 0.8 and profile.n_contracts >= 3:
            weak.append("майже не бере участі в конкурентних процедурах")
        elif profile.reporting_share <= 0.2 and profile.n_contracts >= 3:
            strong.append("уміє вигравати у відкритих торгах")
        if profile.avg_check > market["check"] * 3 and market["check"]:
            strong.append("працює з великими контрактами")
        return strong, weak

    # --- 7. підсумок ------------------------------------------------------

    def summary(self) -> None:
        block = Block(
            "Головне",
            f"Джерело: {self.data.path.name}. Період: "
            f"{self.report.period[0]} — {self.report.period[1]}.")
        total_ours = sum(p.signed for p in self.report.ours)
        best = min((p.rank for p in self.report.ours if p.rank), default=0)
        leader = self.ranking[0] if self.ranking else None
        block.tiles = [
            ("Сума ринку", compact(self.total_market) + " грн"),
            ("Закупівель", count(len(self.tenders))),
            ("Постачальників", count(len(self.by_supplier))),
            ("Наша сума", compact(total_ours) + " грн"),
            ("Наша частка", pct(share(total_ours, self.total_market))),
            ("Наше місце", f"№{best}" if best else "поза рейтингом"),
            ("Лідер ринку", self._short_name(leader[0]) if leader else "—"),
            ("Частка лідера",
             pct(share(leader[1]["signed"], self.total_market)) if leader else "—"),
        ]

        notes: list[str] = []
        if leader:
            notes.append(f"Лідер ринку — {self.names.get(leader[0], leader[0])}: "
                         f"{compact(leader[1]['signed'])} грн "
                         f"({pct(share(leader[1]['signed'], self.total_market))} обороту).")
        if best:
            gap = (leader[1]["signed"] if leader else 0) - max(
                (p.signed for p in self.report.ours), default=0)
            notes.append(f"Найкраще місце наших ТОВ — №{best}. "
                         f"Відрив від лідера: {compact(gap)} грн.")
        else:
            notes.append("Наших ТОВ у цій вибірці немає — перевірте період і фільтр "
                         "або внесіть ЄДРПОУ в налаштуваннях.")
        top3 = self.report.competitors[:3]
        if top3:
            notes.append("Головні конкуренти: " + "; ".join(
                f"{p.name or p.edrpou} ({compact(p.signed)} грн"
                + (f", ТМ {p.top_brand}" if p.top_brand else "") + ")" for p in top3) + ".")
        losses = self._loss_summary()
        if losses:
            notes.append(losses)
        notes.extend(n for n in self.report.notes if n)
        block.notes = notes

        for section in ("Ринок", "Наші ТОВ"):
            for other in self.report.sections.get(section, []):
                for chart in other.charts[:2]:
                    block.charts.append(chart)
        self.report.add("Підсумок", block)
        self._step("Звіт зібрано", 8)

    def _loss_summary(self) -> str:
        if not self.own:
            return ""
        absent = price = other = 0
        for profile in self.report.competitors:
            _h, rows = self._loss_table(profile.edrpou)
            absent += sum(1 for r in rows if r[7] == "ми не подавалися")
            price += sum(1 for r in rows if r[7] == "програли за ціною")
            other += sum(1 for r in rows if r[7].startswith("ціна була нижча"))
        total = absent + price + other
        if not total:
            return ""
        return (f"З {total} закупівель, які взяли розібрані конкуренти, у {absent} "
                f"({pct(share(absent, total))}) нас навіть не було на подачі, у {price} "
                f"нас перебили ціною, а в {other} наша ціна була нижчою — і закупівля "
                f"все одно пішла не до нас (відхилення чи невідповідність вимогам).")
