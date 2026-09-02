"""Гравці ринку: портрети компаній і розбір очних зустрічей.

Тут з очищених угод складається відповідь на головне питання аналізу: хто наші
конкуренти, як вони поводяться на торгах і чому конкретна закупівля пішла не до
нас. Товарну частину — ТМ, канали й порівняння сторін — веде
:mod:`app.core.benchmark`.

Клас нижче — домішка до :class:`app.core.insight.Analyzer`: він користується
станом, який той підготував (``clean_deals``, ``tenders``, ``bids_by_tender``
тощо), і дописує до звіту власні розділи.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from . import brands as tm
from .report import (
    Block, ChartData, Profile, Series, Sheet, compact, count, median, pct, share,
)

#: Скільки рядків показуємо у детальних таблицях профілю.
DETAIL_ROWS = 300
#: Скільки суперників показуємо на порівняльних графіках.
TOP_RIVALS = 12
#: Мінімум спостережень, за якого частка вже щось означає.
MIN_FACTS = 3
#: Процедури, які означають закупівлю без торгів.
DIRECT_METHODS = ("звіт про договір", "переговорна")


def _is_direct(method: str) -> bool:
    low = str(method or "").lower()
    return any(mark in low for mark in DIRECT_METHODS)


class PlayersMixin:
    """Профілі, ТМ, постачання та порівняння. Домішка до ``Analyzer``."""

    # --- 3. профілі гравців ----------------------------------------------

    def profiles(self) -> None:
        self.deals_by_edrpou: dict[str, list[dict]] = defaultdict(list)
        for deal in self.clean_deals:
            self.deals_by_edrpou[deal["edrpou"]].append(deal)

        self.bids_by_edrpou: dict[str, list[dict]] = defaultdict(list)
        for row in self.data.bids:
            if row["edrpou"]:
                self.bids_by_edrpou[row["edrpou"]].append(row)

        self.docs_by_owner = self._attribute_documents()

        wanted: list[str] = []
        for edrpou, _cell in self.ranking:
            if not self.is_ours(edrpou) and len(wanted) < self._top_competitors:
                wanted.append(edrpou)
        for edrpou in self.tracked:
            if edrpou not in wanted and not self.is_ours(edrpou):
                wanted.append(edrpou)
        # Учасник, який жодного разу не виграв, у рейтингу не з'явиться, але
        # як конкурент він реальний — беремо найактивніших за подачами.
        by_activity = sorted(self.bids_by_edrpou.items(), key=lambda kv: -len(kv[1]))
        for edrpou, rows in by_activity[:self._top_competitors]:
            if edrpou not in wanted and not self.is_ours(edrpou) and len(rows) >= 3:
                wanted.append(edrpou)

        self.report.competitors = [self._profile(code) for code in wanted]
        self.report.ours = [self._profile(code, ours=True) for code in sorted(self.own)]
        self._build_ours_section()
        self._build_competitors_section()
        self._step("Профілі гравців складено", 5)

    def _attribute_documents(self) -> dict[str, list[dict]]:
        """Файли за власником.

        У вивантаженні ЄДРПОУ власника файлу заповнений не завжди: портал дає
        його лише там, де документ прив'язаний до пропозиції чи кваліфікації.
        Коли коду немає, файл можна впевнено віднести лише в одному випадку —
        якщо в закупівлі був єдиний учасник; інакше здогад тільки зашкодить.
        """
        out: dict[str, list[dict]] = defaultdict(list)
        for tender_id, docs in self.docs_by_tender.items():
            parties = {b["edrpou"] for b in self.bids_by_tender.get(tender_id, ())}
            parties |= {d["edrpou"] for d in self.deals_by_tender.get(tender_id, ())}
            lone = next(iter(parties)) if len(parties) == 1 else ""
            for doc in docs:
                owner = doc["owner_edrpou"]
                if not owner and lone and str(doc["scope"] or "").lower() != "закупівля":
                    owner = lone
                if owner:
                    out[owner].append(doc)
        return out

    def _profile(self, edrpou: str, ours: bool = False) -> Profile:
        deals = self.deals_by_edrpou.get(edrpou, [])
        bids = self.bids_by_edrpou.get(edrpou, [])
        docs = self.docs_by_owner.get(edrpou, [])
        won_tenders = {d["tender_id"] for d in deals}
        bid_tenders = {b["tender_id"] for b in bids}
        signed = sum(d["amount"] for d in deals)

        profile = Profile(
            edrpou=edrpou, name=self.names.get(edrpou, ""), is_ours=ours or self.is_ours(edrpou),
            rank=self.rank_of.get(edrpou, 0), signed=signed,
            share=share(signed, self.total_market), n_contracts=len(deals),
            n_tenders=len(won_tenders), n_bids=len(bid_tenders), n_wins=len(won_tenders),
            avg_check=signed / len(deals) if deals else 0.0,
            median_check=median(d["amount"] for d in deals),
            n_buyers=len({d["buyer_edrpou"] for d in deals if d["buyer_edrpou"]}),
            n_regions=len({d["region"] for d in deals if d["region"]}),
        )
        # Результативність рахуємо лише там, де пропозиції видно: перемоги
        # у звітах про договір до знаменника не входять, інакше вийшло б
        # «перемог більше, ніж подань».
        profile.n_won_bids = len(won_tenders & bid_tenders)
        if bid_tenders:
            profile.win_rate = share(profile.n_won_bids, len(bid_tenders))

        days = sorted(d["date"] for d in deals if d["date"])
        if days:
            profile.first_seen, profile.last_seen = days[0], days[-1]

        # --- товар і ТМ ---------------------------------------------------
        # Два лічильники: у ``brand_counter`` йде все, з чим гравець виходив на
        # торги, у ``sold_counter`` — лише виграні закупівлі. Різниця не
        # косметична: питання «скільки ТМ він возить» і «скільки ТМ він
        # реально продав» дають різні числа в того, хто багато подається й
        # мало виграє.
        brand_counter: Counter[str] = Counter()
        sold_counter: Counter[str] = Counter()
        type_counter: Counter[str] = Counter()
        for tender_id in won_tenders | bid_tenders:
            found: Counter[str] = Counter()
            for item in self.items_by_tender.get(tender_id, ()):
                found.update(self._brands_of(item["description"]))
                # Родовий тип, а не дослівна назва: інакше «Мишка» й «Миша»
                # у портреті одного гравця стають двома різними товарами.
                kind = self._product_group(item["description"])
                if kind:
                    type_counter[kind] += 1
            if not self.items_by_tender.get(tender_id):
                tender = self.tenders.get(tender_id) or {}
                found.update(self._brands_of(tender.get("title", "")))
                kind = self._product_group(tender.get("title", ""))
                if kind:
                    type_counter[kind] += 1
            brand_counter.update(found)
            if tender_id in won_tenders:
                sold_counter.update(found)
        profile.brands = brand_counter.most_common()
        profile.sold_brands = sold_counter.most_common()
        total_brand_hits = sum(brand_counter.values())
        if profile.brands:
            profile.top_brand = profile.brands[0][0]
            profile.top_brand_share = share(profile.brands[0][1], total_brand_hits)
        profile.main_product = type_counter.most_common(1)[0][0] if type_counter else ""

        # --- поведінка ----------------------------------------------------
        regions = Counter(d["region"] for d in deals if d["region"])
        profile.top_region = regions.most_common(1)[0][0] if regions else ""
        direct = sum(1 for d in deals if _is_direct(d["method"]))
        profile.reporting_share = share(direct, len(deals))
        profile.discount = self._company_discount(deals)
        profile.repeat_share = self._repeat_share(deals)
        profile.trend = self._trend(deals)

        kinds = Counter(tm.document_kind(d["title"], d["format"]) for d in docs)
        profile.certificates = kinds.get("Сертифікат / декларація відповідності", 0)
        profile.authorizations = kinds.get("Лист виробника / авторизація", 0)

        # --- зірвані перемоги ---------------------------------------------
        # Знаменник — перемоги плюс відмови самої компанії: питання «як часто
        # її виграш зривається», а не «яку частку ринку вона втратила».
        rejected = self.rejections_by_edrpou.get(edrpou, [])
        profile.n_rejected = len(rejected)
        profile.rejected_sum = sum(row["amount"] for row in rejected)
        if rejected:
            profile.reject_reason = Counter(
                row["category"] for row in rejected).most_common(1)[0][0]
            attempts = profile.n_tenders + profile.n_rejected
            profile.reject_share = share(profile.n_rejected, attempts) if attempts else None

        profile.traits = self._traits(profile, deals, regions)
        profile.block = self._profile_block(profile, deals, bids, docs, kinds,
                                            brand_counter, type_counter, regions)
        return profile

    def _company_discount(self, deals: list[dict]) -> float | None:
        values: list[float] = []
        by_tender: dict[str, float] = defaultdict(float)
        for deal in deals:
            by_tender[deal["tender_id"]] += deal["amount"]
        for tender_id, amount in by_tender.items():
            expected = (self.tenders.get(tender_id) or {}).get("value") or 0.0
            if expected > 0 and 0 < amount <= expected * 1.05:
                values.append(1 - amount / expected)
        return median(values) if values else None

    def _repeat_share(self, deals: list[dict]) -> float:
        """Частка угод із замовником, з яким уже був договір раніше."""
        seen: set[str] = set()
        repeats = 0
        for deal in sorted(deals, key=lambda d: d["date"]):
            code = deal["buyer_edrpou"]
            if not code:
                continue
            if code in seen:
                repeats += 1
            seen.add(code)
        return share(repeats, len(deals))

    def _trend(self, deals: list[dict]) -> float | None:
        """Приріст суми в другій половині періоду проти першої."""
        days = sorted(d["date"] for d in deals if d["date"])
        if len(days) < 4:
            return None
        middle = days[len(days) // 2]
        first = sum(d["amount"] for d in deals if d["date"] and d["date"] < middle)
        second = sum(d["amount"] for d in deals if d["date"] and d["date"] >= middle)
        if first <= 0:
            return None
        return second / first - 1

    def _traits(self, profile: Profile, deals: list[dict], regions: Counter) -> list[str]:
        """Коротка характеристика гравця — лише те, що видно з цифр.

        Частки на одному-двох спостереженнях нічого не означають, тому кожне
        твердження має свій поріг: «монобрендовий» на єдиній позиції — це не
        висновок, а шум.
        """
        traits: list[str] = []
        enough = len(deals) >= MIN_FACTS
        if not deals and not profile.n_bids:
            # Так виглядає відстежуваний конкурент, якого в цій вибірці немає:
            # без цього рядка сторінка була б просто набором нулів.
            return ["У цій вибірці компанії немає: ані угод, ані пропозицій. "
                    "Перевірте період і фільтр збору — або вона тут справді "
                    "не працює."]
        if profile.rank:
            traits.append(f"{profile.rank}-е місце на ринку: {compact(profile.signed)} грн "
                          f"({pct(profile.share)} обороту)")
        if profile.main_product:
            traits.append(f"Основний товар: {profile.main_product.lower()}")
        if profile.top_brand and sum(n for _b, n in profile.brands) >= MIN_FACTS:
            note = tm.own_tm().get(profile.top_brand)
            mark = f"ТМ {profile.top_brand} у {pct(profile.top_brand_share)} позицій"
            if profile.top_brand_share >= 0.6:
                mark = f"Монобрендовий гравець: {mark}"
            if note:
                owner = note.get("owner")
                mark += " — довідник знає її як власну ТМ" + (f" {owner}" if owner else "")
            traits.append(mark)
            if len(profile.brands) >= 5:
                traits.append(f"Портфель із {len(profile.brands)} ТМ")
        elif profile.top_brand:
            traits.append(f"Помічена ТМ: {profile.top_brand} (замало позицій для висновку)")
        if enough:
            if profile.reporting_share >= 0.6:
                traits.append(f"Працює переважно поза торгами: {pct(profile.reporting_share)} "
                              f"угод — прямі договори та переговорні процедури")
            elif profile.reporting_share <= 0.2:
                traits.append(f"Грає у відкритих процедурах: лише "
                              f"{pct(profile.reporting_share)} угод без торгів")
        if profile.discount is not None:
            market = self.market_discount
            if market is not None and profile.discount > market + 0.02:
                traits.append(f"Агресивна ціна: медіанний дисконт {pct(profile.discount)} "
                              f"проти {pct(market)} по ринку")
            elif market is not None and profile.discount < market - 0.02:
                traits.append(f"Тримає ціну: дисконт {pct(profile.discount)} "
                              f"проти {pct(market)} по ринку")
        if regions and enough:
            top_share = share(regions.most_common(1)[0][1], sum(regions.values()))
            if top_share >= 0.6:
                traits.append(f"Географічно сфокусований: {profile.top_region} "
                              f"({pct(top_share)} угод)")
            elif profile.n_regions >= 5:
                traits.append(f"Широка географія: {profile.n_regions} областей")
        if profile.repeat_share >= 0.4 and len(deals) >= 5:
            traits.append(f"Тримається постійних замовників: {pct(profile.repeat_share)} "
                          f"угод — повторні")
        if profile.win_rate is not None and profile.n_bids >= 3:
            traits.append(f"Результативність на торгах: {pct(profile.win_rate)} "
                          f"({profile.n_won_bids} з {profile.n_bids})")
        if profile.trend is not None:
            word = "зростає" if profile.trend > 0.2 else (
                "спадає" if profile.trend < -0.2 else "тримається рівно")
            traits.append(f"Динаміка: {word} ({pct(profile.trend, 0)} у другій половині періоду)")
        if profile.certificates or profile.authorizations:
            parts = []
            if profile.certificates:
                parts.append(f"сертифікатів: {profile.certificates}")
            if profile.authorizations:
                parts.append(f"авторизаційних листів: {profile.authorizations}")
            traits.append("У поданих файлах — " + ", ".join(parts))
        if profile.n_rejected:
            # Частку показуємо лише там, де вона щось означає: одна відмова на
            # одну перемогу — це «50%», і виглядає це переконливіше, ніж є.
            # Разом із нею йдуть обидва числа: без знаменника «84,6%» читалося
            # б як частка ринку, а не як «11 виграшів із 13 зірвалися».
            if profile.reject_share is not None and profile.n_rejected >= MIN_FACTS:
                traits.append(
                    f"Зривається {pct(profile.reject_share)} виграшів: "
                    f"{profile.n_rejected} з {profile.n_rejected + profile.n_tenders} "
                    f"на {compact(profile.rejected_sum)} грн, найчастіше — "
                    f"{profile.reject_reason.lower()}")
            else:
                traits.append(
                    f"Зірваних перемог: {profile.n_rejected} на "
                    f"{compact(profile.rejected_sum)} грн, найчастіше — "
                    f"{profile.reject_reason.lower()}")
        return traits

    def _profile_block(self, profile: Profile, deals: list[dict], bids: list[dict],
                       docs: list[dict], kinds: Counter, brand_counter: Counter,
                       type_counter: Counter, regions: Counter) -> Block:
        block = Block(profile.label)
        block.tiles = [
            ("Місце на ринку", f"№{profile.rank}" if profile.rank else "поза рейтингом"),
            ("Сума угод", compact(profile.signed) + " грн"),
            ("Частка ринку", pct(profile.share)),
            ("Угод", count(profile.n_contracts)),
            ("Закупівель виграно", count(profile.n_tenders)),
            ("Подань (де видно)", count(profile.n_bids)),
            # Поруч із відсотком показуємо самі числа: перемоги в закупівлях
            # без торгів у знаменник не входять, і без цієї плитки виходило б,
            # ніби відсоток не сходиться з попередніми двома.
            ("Виграно з поданих",
             f"{count(profile.n_won_bids)} з {count(profile.n_bids)}"
             if profile.n_bids else "—"),
            ("Результативність",
             pct(profile.win_rate) if profile.win_rate is not None else "—"),
            ("Середня угода", compact(profile.avg_check) + " грн"),
            ("Медіанна угода", compact(profile.median_check) + " грн"),
            ("Замовників", count(profile.n_buyers)),
            ("Областей", count(profile.n_regions)),
            ("Основна ТМ", profile.top_brand or "не визначено"),
            ("Основний товар", profile.main_product or "не визначено"),
            ("Дисконт (медіана)",
             pct(profile.discount) if profile.discount is not None else "—"),
            ("Повторні замовники", pct(profile.repeat_share)),
            ("Активність", f"{profile.first_seen or '—'} — {profile.last_seen or '—'}"),
            ("Зірвано перемог",
             f"{count(profile.n_rejected)} на {compact(profile.rejected_sum)} грн"
             if profile.n_rejected else "—"),
            ("Причина зривів", profile.reject_reason or "—"),
        ]
        block.notes = list(profile.traits)

        months: Counter[str] = Counter()
        for deal in deals:
            if deal["month"]:
                months[deal["month"]] += deal["amount"]
        if len(months) >= 2:
            labels = sorted(months)
            block.charts.append(ChartData(
                "Динаміка по місяцях", "line",
                [Series("Сума угод", labels, [months[m] for m in labels])], unit="грн"))
        if brand_counter:
            top = brand_counter.most_common(8)
            block.charts.append(ChartData(
                "Торгові марки у позиціях", "pie",
                [Series("Позицій", [b for b, _n in top], [n for _b, n in top])],
                unit="шт", money_axis=False))
        if type_counter:
            top = type_counter.most_common(10)
            block.charts.append(ChartData(
                "Що саме продає", "hbar",
                [Series("Позицій", [t for t, _n in top], [n for _t, n in top])],
                unit="шт", money_axis=False))
        buyers: Counter[str] = Counter()
        for deal in deals:
            if deal["buyer_edrpou"]:
                buyers[deal["buyer"] or deal["buyer_edrpou"]] += deal["amount"]
        if buyers:
            top = buyers.most_common(10)
            block.charts.append(ChartData(
                "Ключові замовники", "hbar",
                [Series("Сума", [b[:40] for b, _v in top], [v for _b, v in top])], unit="грн"))
        methods = Counter(d["method"] or "не вказано" for d in deals)
        if len(methods) > 1:
            block.charts.append(ChartData(
                "Процедури", "pie",
                [Series("Угод", [m for m, _n in methods.most_common(6)],
                        [n for _m, n in methods.most_common(6)])],
                unit="шт", money_axis=False))
        if regions and len(regions) > 1:
            top = regions.most_common(10)
            block.charts.append(ChartData(
                "Географія", "hbar",
                [Series("Угод", [r for r, _n in top], [n for _r, n in top])],
                unit="шт", money_axis=False))

        if brand_counter:
            block.tables.append(("ТМ", (
                ["Торгова марка", "Позицій", "Частка", "Власна ТМ за довідником"],
                [[name, n, round(share(n, sum(brand_counter.values())), 4),
                  (tm.own_tm().get(name) or {}).get("owner", "")]
                 for name, n in brand_counter.most_common(40)])))
        if type_counter:
            block.tables.append(("Товари", (
                ["Товар", "Позицій"], [[name, n] for name, n in type_counter.most_common(40)])))
        if kinds:
            block.tables.append(("Документи", (
                ["Тип документа", "Файлів"],
                [[name or "не класифіковано", n] for name, n in kinds.most_common()])))
        rejected = self.rejections_by_edrpou.get(profile.edrpou, [])
        if rejected:
            block.tables.append(("Зірвані перемоги", (
                ["Закупівля", "Дата", "Замовник", "Сума, грн", "Причина",
                 "Формулювання замовника"],
                [[row["tender_id"], row["date"], row["buyer"], round(row["amount"], 2),
                  row["category"], row["reason"][:300]]
                 for row in sorted(rejected, key=lambda r: -r["amount"])[:DETAIL_ROWS]])))

        block.tables.append(("Угоди", (
            ["Закупівля", "Дата", "Замовник", "Регіон", "Сума, грн", "Очікувана, грн",
             "Процедура", "Галузь", "Джерело"],
            [[d["tender_id"], d["date"], d["buyer"], d["region"], round(d["amount"], 2),
              round(d["expected"] or 0, 2), d["method"], d["group_name"], d["source"]]
             for d in sorted(deals, key=lambda d: -d["amount"])[:DETAIL_ROWS]])))

        # Для власних ТОВ таблиця порожня за побудовою: «хто виграв замість
        # нас» має сенс лише щодо чужої компанії.
        losses = self._loss_table(profile.edrpou)
        if losses[1]:
            block.tables.append(("Наші зустрічі", losses))
        return block

    # --- очні зустрічі ----------------------------------------------------

    def _loss_table(self, edrpou: str) -> Sheet:
        """Що сталося в закупівлях, які виграв цей гравець.

        Три можливі відповіді, і всі три однаково важливі: нас там не було,
        нас перебили ціною, або ціна була наша, а закупівля — ні (відхилення,
        невідповідність, дискваліфікація).

        У третьому випадку здогадуватись більше не треба: якщо нас відхилили,
        замовник назвав підставу, і вона лежить у тій самій картці. Тоді
        замість «ціна була нижча — програли не за ціною» стоїть сама причина.
        """
        cached = self._loss_cache.get(edrpou)
        if cached is not None:
            return cached
        headers = ["Закупівля", "Дата", "Замовник", "Предмет", "Сума переможця, грн",
                   "Наша пропозиція, грн", "Розрив", "Що сталося"]
        rows: list[list[Any]] = []
        if self.is_ours(edrpou) or not self.own:
            self._loss_cache[edrpou] = (headers, rows)
            return headers, rows
        won: dict[str, float] = defaultdict(float)
        for deal in self.deals_by_edrpou.get(edrpou, []):
            won[deal["tender_id"]] += deal["amount"]
        for tender_id, won_sum in won.items():
            tender = self.tenders.get(tender_id) or {}
            bids = self.bids_by_tender.get(tender_id, [])
            theirs = [b["amount"] for b in bids if b["edrpou"] == edrpou and b["amount"]]
            ours = [b["amount"] for b in bids if b["edrpou"] in self.own and b["amount"]]
            winner_sum = min(theirs) if theirs else won_sum
            if not ours:
                verdict, our_sum, gap = "ми не подавалися", None, None
            elif not theirs:
                verdict, our_sum, gap = "подавалися, ціни переможця не видно", min(ours), None
            elif min(ours) > min(theirs):
                our_sum = min(ours)
                gap = our_sum / min(theirs) - 1
                verdict = "програли за ціною"
            else:
                our_sum = min(ours)
                gap = our_sum / min(theirs) - 1
                verdict = "ціна була нижча — програли не за ціною"
            ours_rejected = [row for row in self.rejections_by_tender.get(tender_id, ())
                             if row["edrpou"] in self.own]
            if ours_rejected:
                verdict = "нас відхилили: " + ours_rejected[0]["category"].lower()
            rows.append([tender_id, tender.get("date", ""), tender.get("buyer", ""),
                         (tender.get("title", ""))[:80], round(winner_sum or 0, 2),
                         round(our_sum, 2) if our_sum else None,
                         round(gap, 4) if gap is not None else None, verdict])
        rows.sort(key=lambda r: -(r[4] or 0))
        sheet = (headers, rows[:DETAIL_ROWS])
        self._loss_cache[edrpou] = sheet
        return sheet

    # --- розділ «Наші ТОВ» ------------------------------------------------

    def _build_ours_section(self) -> None:
        block = Block(
            "Наші ТОВ на ринку",
            "Позиція кожного нашого ЄДРПОУ в загальному заліку, охоплення ринку "
            "та розбір закупівель, які пішли до інших.")
        if not self.own:
            block.notes.append("У налаштуваннях не вказано жодного нашого ЄДРПОУ — "
                               "порівнювати нема з чим.")
            self.report.add("Наші ТОВ", block)
            return

        rows: list[list[Any]] = []
        for profile in self.report.ours:
            leader = self.ranking[0][1]["signed"] if self.ranking else 0.0
            rows.append([
                profile.edrpou, profile.name or "немає у вибірці",
                profile.rank or "—", round(profile.signed, 2), round(profile.share, 4),
                profile.n_contracts, profile.n_tenders, profile.n_bids,
                round(profile.win_rate, 4) if profile.win_rate is not None else None,
                round(profile.avg_check, 2), profile.n_buyers, profile.n_regions,
                profile.top_brand, profile.main_product,
                round(leader - profile.signed, 2),
            ])
        block.tables.append(("Наші ТОВ", (
            ["ЄДРПОУ", "Компанія", "Місце", "Сума угод, грн", "Частка ринку", "Угод",
             "Закупівель", "Подань", "Результативність", "Середня угода, грн",
             "Замовників", "Областей", "Основна ТМ", "Основний товар",
             "Відрив від лідера, грн"], rows)))

        active = [p for p in self.report.ours if p.signed > 0 or p.n_bids > 0]
        total_ours = sum(p.signed for p in self.report.ours)
        block.tiles = [
            ("Наша сума угод", compact(total_ours) + " грн"),
            ("Наша частка ринку", pct(share(total_ours, self.total_market))),
            ("Найкраще місце",
             f"№{min((p.rank for p in active if p.rank), default=0)}"
             if any(p.rank for p in active) else "—"),
            ("Наших угод", count(sum(p.n_contracts for p in self.report.ours))),
            ("Подань на торги", count(sum(p.n_bids for p in self.report.ours))),
        ]
        if not active:
            block.notes.append(
                "У цій вибірці наших ЄДРПОУ немає: ані угод, ані пропозицій. "
                "Або період/фільтр не той, або закупівлі проходили поза цією галуззю.")

        coverage = self._coverage()
        if coverage:
            block.tiles.append(("Охоплення ринку", pct(coverage["rate"])))
            block.notes.append(
                f"У галузях, де ми взагалі присутні, відбулося {coverage['addressable']} "
                f"закупівель на {compact(coverage['value'])} грн. Ми брали участь у "
                f"{coverage['participated']} із них ({pct(coverage['rate'])}); "
                f"повз пройшло {coverage['missed']} закупівель на "
                f"{compact(coverage['missed_value'])} грн.")
            block.tables.append(("Пропущені закупівлі", coverage["sheet"]))
            if coverage["by_group"]:
                labels = [name for name, _v in coverage["by_group"]]
                block.charts.append(ChartData(
                    "Де ми не брали участі — за галузями", "hbar",
                    [Series("Сума пропущеного", labels,
                            [v for _n, v in coverage["by_group"]])], unit="грн"))

        rivals = self._head_to_head()
        if rivals[1]:
            block.tables.append(("Очні зустрічі", rivals))
            top = rivals[1][:TOP_RIVALS]
            block.charts.append(ChartData(
                "З ким ми найчастіше зустрічаємось на торгах", "hbar",
                [Series("Спільних закупівель", [r[1][:34] for r in top],
                        [r[2] for r in top])], unit="шт", money_axis=False))

        if self.report.ours:
            line, accent = self._visible_ranking(self.ranking, TOP_RIVALS)
            block.charts.insert(0, ChartData(
                f"Ми проти лідерів ринку (топ-{TOP_RIVALS})", "hbar",
                [Series("Сума угод",
                        [self._rank_label(e, TOP_RIVALS) for e, _c in line],
                        [cell["signed"] for _e, cell in line], accent=accent)],
                unit="грн",
                hint="Наші ТОВ виділені кольором і стоять у списку завжди: те, що не "
                     f"ввійшло до топ-{TOP_RIVALS}, дописане в кінець із номером свого "
                     "місця. Тут лише ті, у кого є підписані угоди — ЄДРПОУ без жодної "
                     "угоди в рейтингу немає."))
        self.report.add("Наші ТОВ", block)

    def _coverage(self) -> dict | None:
        """Скільки ринку в наших галузях ми взагалі чіпали."""
        if not self.own:
            return None
        our_groups = {d["group"] for d in self.clean_deals if d["ours"] and d["group"]}
        for tender_id, bids in self.bids_by_tender.items():
            if any(b["edrpou"] in self.own for b in bids):
                group = self._group_of(self.cpv_of.get(tender_id, ""))
                if group:
                    our_groups.add(group)
        our_groups.discard("")
        if not our_groups:
            return None

        participated: set[str] = set()
        for deal in self.clean_deals:
            if deal["ours"]:
                participated.add(deal["tender_id"])
        for tender_id, bids in self.bids_by_tender.items():
            if any(b["edrpou"] in self.own for b in bids):
                participated.add(tender_id)

        addressable: list[dict] = []
        for tender_id, tender in self.tenders.items():
            if self._group_of(self.cpv_of.get(tender_id, "")) in our_groups:
                addressable.append(tender)
        missed = [t for t in addressable if t["tender_id"] not in participated]
        value = sum(t["value"] or 0 for t in addressable)
        missed_value = sum(t["value"] or 0 for t in missed)

        by_group: Counter[str] = Counter()
        for tender in missed:
            group, name = self._group_pair(self.cpv_of.get(tender["tender_id"], ""))
            by_group[name or group or "не визначено"] += tender["value"] or 0.0

        sheet: Sheet = (
            ["Закупівля", "Дата", "Замовник", "Регіон", "Предмет", "Очікувана вартість, грн",
             "Процедура", "Переможець"],
            [[t["tender_id"], t["date"], t["buyer"], t["region"], t["title"][:80],
              round(t["value"] or 0, 2), t["method"],
              ", ".join(sorted({d["name"] for d in
                                self.deals_by_tender.get(t["tender_id"], ())}))[:60]]
             for t in sorted(missed, key=lambda t: -(t["value"] or 0))[:DETAIL_ROWS]])
        return {
            "addressable": len(addressable), "participated": len(addressable) - len(missed),
            "missed": len(missed), "value": value, "missed_value": missed_value,
            "rate": share(len(addressable) - len(missed), len(addressable)),
            "sheet": sheet, "by_group": by_group.most_common(TOP_RIVALS),
        }

    def _head_to_head(self) -> Sheet:
        """Скільки разів ми виходили проти кожного конкурента і чим це кінчалось."""
        headers = ["ЄДРПОУ", "Конкурент", "Спільних закупівель", "Виграли ми",
                   "Виграв конкурент", "Медіанний розрив ціни", "Сума їхніх перемог, грн"]
        rows: list[list[Any]] = []
        if not self.own:
            return headers, rows
        our_tenders = {tid for tid, bids in self.bids_by_tender.items()
                       if any(b["edrpou"] in self.own for b in bids)}
        stats: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "we": 0, "they": 0, "gaps": [], "sum": 0.0})
        for tender_id in our_tenders:
            bids = self.bids_by_tender.get(tender_id, [])
            ours = [b["amount"] for b in bids if b["edrpou"] in self.own and b["amount"]]
            deals = self.deals_by_tender.get(tender_id, ())
            winners = {d["edrpou"] for d in deals}
            for rival in {b["edrpou"] for b in bids} - self.own:
                cell = stats[rival]
                cell["n"] += 1
                if winners & self.own:
                    cell["we"] += 1
                elif rival in winners:
                    cell["they"] += 1
                    cell["sum"] += sum(d["amount"] for d in deals if d["edrpou"] == rival)
                theirs = [b["amount"] for b in bids if b["edrpou"] == rival and b["amount"]]
                if ours and theirs and min(theirs) > 0:
                    cell["gaps"].append(min(ours) / min(theirs) - 1)
        for edrpou, cell in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
            rows.append([edrpou, self.names.get(edrpou, ""), cell["n"], cell["we"],
                         cell["they"],
                         round(median(cell["gaps"]), 4) if cell["gaps"] else None,
                         round(cell["sum"], 2)])
        return headers, rows

    # --- розділ «Конкуренти» ---------------------------------------------

    def _build_competitors_section(self) -> None:
        block = Block(
            "Конкуренти",
            "Перелік помітних гравців із ключовими показниками. Розгорнутий портрет "
            "кожного — у списку праворуч.")
        headers = ["Місце", "ЄДРПОУ", "Компанія", "Сума угод, грн", "Частка", "Угод",
                   "Подань", "Результативність", "Середня угода, грн", "Основна ТМ",
                   "Частка ТМ", "Основний товар", "Без торгів", "Дисконт", "Областей",
                   "Замовників", "Повторні", "Сертифікатів", "Авторизацій", "Динаміка"]
        rows = []
        for profile in self.report.competitors:
            rows.append([
                profile.rank or "—", profile.edrpou, profile.name,
                round(profile.signed, 2), round(profile.share, 4), profile.n_contracts,
                profile.n_bids,
                round(profile.win_rate, 4) if profile.win_rate is not None else None,
                round(profile.avg_check, 2), profile.top_brand,
                round(profile.top_brand_share, 4), profile.main_product,
                round(profile.reporting_share, 4),
                round(profile.discount, 4) if profile.discount is not None else None,
                profile.n_regions, profile.n_buyers, round(profile.repeat_share, 4),
                profile.certificates, profile.authorizations,
                round(profile.trend, 4) if profile.trend is not None else None,
            ])
        block.tables.append(("Конкуренти", (headers, rows)))

        loss_headers = ["ЄДРПОУ", "Конкурент", "Його перемог", "Ми не подавалися",
                        "Програли за ціною", "Програли не за ціною",
                        "Медіанний розрив ціни", "Сума його перемог, грн"]
        loss_rows: list[list[Any]] = []
        for profile in self.report.competitors:
            _headers, rows_of = self._loss_table(profile.edrpou)
            if not rows_of:
                continue
            absent = sum(1 for r in rows_of if r[7] == "ми не подавалися")
            price = sum(1 for r in rows_of if r[7] == "програли за ціною")
            other = sum(1 for r in rows_of if r[7].startswith("ціна була нижча"))
            gaps = [r[6] for r in rows_of if r[6] is not None]
            loss_rows.append([profile.edrpou, profile.name, len(rows_of), absent, price,
                              other, round(median(gaps), 4) if gaps else None,
                              round(sum(r[4] or 0 for r in rows_of), 2)])
        if loss_rows and self.own:
            block.tables.append(("Чому програли", (loss_headers, loss_rows)))
            top = sorted(loss_rows, key=lambda r: -r[2])[:TOP_RIVALS]
            names = [self._short_name(r[0], 30) for r in top]
            block.charts.append(ChartData(
                "Закупівлі конкурентів: де нас не було, а де програли", "hbar",
                [Series("Не подавалися", names, [r[3] for r in top]),
                 Series("Програли за ціною", names, [r[4] for r in top]),
                 Series("Програли не за ціною", names, [r[5] for r in top])],
                unit="шт", money_axis=False,
                hint="Три різні проблеми: перша — про моніторинг закупівель, "
                     "друга — про ціну, третя — про якість підготовки пропозиції."))

        if self.report.competitors:
            # Наші ТОВ дописуємо в хвіст: без опорної смуги «а скільки в нас»
            # висота стовпчиків конкурентів ні про що не говорить.
            top = self.report.competitors[:TOP_RIVALS]
            line = [*top, *(p for p in self.report.ours if p.signed > 0)]
            accent = {i for i, p in enumerate(line) if p.is_ours}
            # Наше ТОВ стоїть у хвості незалежно від свого місця, тож номер
            # ставимо завжди: без нього смуга наприкінці читалася б як останнє
            # місце ринку.
            labels = [f"{self._short_name(p.edrpou, 27)} (№{p.rank})"
                      if p.is_ours and p.rank else self._short_name(p.edrpou)
                      for p in line]
            block.charts.insert(0, ChartData(
                f"Найбільші конкуренти (топ-{TOP_RIVALS}) і ми", "hbar",
                [Series("Сума угод", labels, [p.signed for p in line], accent=accent)],
                unit="грн",
                hint="Кольором виділені наші ТОВ — вони показані для порівняння й до "
                     "рейтингу конкурентів не входять."))
        self.report.add("Конкуренти", block)
