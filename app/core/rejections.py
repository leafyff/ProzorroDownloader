"""Причини, з яких перемога не стала договором.

Замовник, відхиляючи переможця, зобов'язаний назвати підставу — і називає її
у самій картці закупівлі: ``awards[].title`` містить формулювання підстави,
``awards[].description`` — розгорнуте пояснення. Тобто **жодного зайвого
запиту**: обидва поля приходять із тією ж карткою ЦБД, яку конвеєр і так тягне.

Що виміряно на живому API 02.09.2026 (893 закупівлі ІТ-техніки за
березень-серпень 2026, 899 рішень про переможця):

* ``unsuccessful`` — 72 рішення, і **всі 72** мають ``title``; ``description``
  є у 58. Тобто підстава відома майже завжди, пояснення — у чотирьох випадках
  із п'яти;
* ``cancelled`` — 55 рішень, але це **не окремі відмови**: 51 із них має
  ``unsuccessful``-двійника з тим самим ``bid_id``. Замовник спершу скасовує
  власне рішення про переможця, а вже потім оприлюднює відхилення, тож одна
  подія лежить у базі двома рядками. Решта 4 — без тексту, і після них те саме
  рішення виходить ``active`` (замовник виправив суму). Рахувати відмови треба
  **лише за ``unsuccessful``**: інакше 127 подій замість 72, тобто +76%;
* у ``active`` і ``cancelled`` поле ``title`` теж буває заповнене (89 і 15
  разів), але там воно означає протилежне — «Визнаний переможцем». Читати
  причину зі скасованого чи чинного рішення не можна;
* ``qualifications`` (окремий етап прекваліфікації зі своїми причинами) у
  сучасних процедурах порожні: 0 записів на 1690 картках ``aboveThreshold``,
  ``belowThreshold``, ``priceQuotation`` і ``requestForProposal``. Тому
  дивимось тільки на ``awards``.

Формулювання — вільний текст, але не довільний: замовник переписує підставу
з пункту 44 Особливостей (ПКМУ №1178) або з пунктів 64/66 Порядку №822, часто
дослівно. Це й дозволяє зводити тексти до горстки категорій пошуком підрядка:
у вибірці 72 відмов трапилось лише 14 різних ``title``, і всі 72 розпізналися
без залишку — 45 відмов від договору, 15 невідповідностей технічним вимогам,
6 непідписаних договорів, 5 невідповідностей документації, 1 ненадані
документи.

**Довідник перевірено на вибірці, на якій його не будували.** 2 000 випадкових
закупівель усієї країни за березень-червень 2026 (усі галузі, не лише ІТ) дали
64 відхилення — розпізналися **всі**, і розподіл вийшов геть інший: 29,7%
непідписаних договорів проти 21,9% відмов, тоді як на ІТ відмова від договору
займала 62,5%. Спрацювали й чотири категорії, яких у першій вибірці не було
взагалі (забезпечення, невиправлені невідповідності, недостовірна інформація,
підстави для відмови). Це свідчить, що шаблони тримаються на мові закону, а не
підігнані під одну вибірку. Нуль у «Іншій причині» все одно не привід
розслаблятися — категорія існує саме для того, щоб незнайоме формулювання
лишалося незнайомим, а не приписувалося найближчому за змістом.

Одна плутанина, яку та перевірка й виявила: пункт 47 Особливостей згадують
**дві різні** підстави — «не надав документи, що підтверджують відсутність
підстав» і «сам підпадає під ці підстави». Голе посилання на пункт не каже,
котра з них; тому воно шаблоном більше не є (з 136 відмов на ньому не трималася
жодна правильна), а «підпадає під підстави» стало окремою категорією.
"""
from __future__ import annotations

import re

#: Категорії причин: ``(назва, шаблони)``. Назва потрапляє у звіт як є, тож
#: коротка — вона підписує стовпчики діаграми.
#:
#: Шаблони записані основами (``підписа\w*``), бо підстава пишеться то від
#: третьої особи («переможець не підписав»), то безособово («не підписано»).
#: Перевіряти на порожньому переліку нічого: категорія без шаблонів — це
#: :data:`OTHER`, і вона призначається залишку.
REASONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Відмова від договору", (
        r"відмов\w*[^.;]{0,80}?(?:укладенн|укладанн|підписанн)",
        r"(?:лист|повідомленн\w*)[\s\-–—]*(?:про\s+)?відмов",
        r"відмовляється\s+від",
    )),
    ("Не підписав договір у строк", (
        r"не\s+підписа\w*\s+(?:у\s+строк\s+)?догов",
        r"не\s+уклав\s+догов",
        r"не\s+з['’ʼ]явив\w*\s+для\s+підписанн",
    )),
    ("Недостовірна інформація", (
        r"недостовірн",
    )),
    ("Не усунув невідповідності", (
        r"не\s+виправив",
        r"не\s+усунув\s+невідповідност",
    )),
    ("Не надав забезпечення", (
        r"забезпеченн\w*\s+(?:тендерної\s+)?пропозиц",
        r"забезпеченн\w*\s+виконанн\w*\s+догов",
    )),
    ("Аномально низька ціна", (
        r"аномально\s+низьк",
    )),
    ("Строк дії пропозиції минув", (
        # У законі це «пропозиція, строк дії якої закінчився» — між «строк
        # дії» і дієсловом стоїть «якої», а не назва пропозиції.
        r"строк\s+дії[^.;]{0,40}?(?:закінчив|мину|сплив)",
    )),
    ("Не відповідає кваліфікаційним критеріям", (
        r"кваліфікаційн\w*\s+критер",
    )),
    # Учасник **сам** підпадає під підстави пункту 47 Особливостей (стаття 17
    # Закону) — податковий борг, судимість, банкрутство. Це не те саме, що не
    # подати довідку про їх відсутність, хоча обидві підстави посилаються на
    # той самий пункт: перша каже «він такий», друга — «він не довів, що не
    # такий». Виміряно на held-out вибірці: голе посилання на пункт відносило
    # «підпадає під підстави, встановлені пунктом 47» до ненаданих документів.
    ("Підпадає під підстави для відмови", (
        r"підпада\w*\s+під\s+підстав",
        r"наявн\w*\s+підстав\w*\s+для\s+відмови",
    )),
    ("Не надав документи", (
        # Проміжок навмисно не пускає крізь себе «забезпечення»: інакше
        # «не надав забезпечення тендерної пропозиції у формі, визначеній
        # документацією» починалося б зі слова «не надав» і перебивало
        # власну категорію, яка починається на вісім знаків пізніше.
        r"не\s+нада\w*(?:(?!забезпеченн)[^.;]){0,60}?документ",
        r"не\s+пода\w*(?:(?!забезпеченн)[^.;]){0,60}?документ",
        # Саме «документи, що підтверджують відсутність підстав» — а не голе
        # посилання на пункт: сам по собі пункт 47 не каже, котра з двох
        # підстав мається на увазі, і вгадувати тут нема чого. Виміряно: з
        # 136 відмов на голому посиланні не тримається жодна правильна.
        r"підтверджують\s+відсутність\s+підстав",
    )),
    ("Невідповідність технічним вимогам", (
        r"технічн\w*\s+специфікац",
        r"технічн\w*\s+вимог",
        r"технічн\w*[,\s]+якісн",
    )),
    ("Невідповідність вимогам документації", (
        r"тендерн\w*\s+документац",
        r"умовам,?\s+визначеним\s+(?:замовником\s+)?в\s+оголошенн",
        r"оголошенн\w*\s+про\s+проведенн",
        r"запиті\s+пропозицій\s+постачальник",
        r"вимогам\s+до\s+предмета\s+закупівлі",
    )),
)

#: Причина є, але жоден шаблон не впізнав її.
OTHER = "Інша причина"
#: Тексту немає взагалі — замовник обмежився зміною статусу.
UNSTATED = "Причину не зазначено"

#: Що саме з документів згадане у формулюванні. Для ринку ІТ-техніки це
#: найчастіше питання не ціни, а паперів: авторизаційний лист виробника,
#: сертифікат відповідності, ліцензія. Ознаки не замінюють категорії — вони
#: доповнюють її, бо «не надав документи» і «невідповідність технічним
#: вимогам» однаково можуть впиратися в той самий сертифікат.
#:
#: У вибірці з 72 відмов таких згадок одиниці (сертифікат — 1, авторизація —
#: 2, ліцензія — 1), тож ознака показується числом, а не часткою: на таких
#: кількостях відсоток створював би видимість закономірності.
MARKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("сертифікат", (r"сертифікат", r"декларац\w*\s+(?:про\s+)?відповідност")),
    ("авторизаційний лист", (r"авторизац", r"лист\w*\s+виробник")),
    ("ліцензія", (r"ліценз",)),
    ("гарантія", (r"гарантійн", r"гаранті\w*\s+(?:якост|строк)")),
)

#: Підстави відміни **всієї** закупівлі (``cancellations[].reasonType``) —
#: на відміну від причини відмови це вже перелік, а не вільний текст.
#: Виміряно у вибірці: ``unFixable`` 8, ``noDemand`` 4, ``forceMajeure`` 2;
#: у ширшій пробі (1690 карток) додався ``expensesCut``. Невідоме значення
#: показуємо як є — краще англійський код, ніж мовчазна прочерк.
CANCEL_LABELS = {
    "noDemand": "Відсутня потреба в закупівлі",
    "unFixable": "Неможливо усунути порушення законодавства",
    "forceMajeure": "Обставини непереборної сили",
    "expensesCut": "Скорочено видатки на закупівлю",
    "dateViolated": "Порушено строки процедури",
}

#: Статус рішення, який означає саме відхилення переможця.
REJECTED_STATUS = "unsuccessful"

_COMPILED = tuple(
    (label, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for label, patterns in REASONS
)
_MARKS = tuple(
    (label, tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns))
    for label, patterns in MARKS
)


def classify(reason: str, explanation: str = "") -> str:
    """Категорія причини за формулюванням підстави.

    Дивимось спершу на ``title``: там лежить сама підстава, тоді як
    ``description`` — це вже переказ обставин, у якому легко натрапити на
    слово з чужої категорії («відмовився» у тексті про технічну
    невідповідність). До пояснення звертаємось, лише коли підстави немає.

    З кількох знайдених категорій перемагає та, чий шаблон стоїть у тексті
    **найраніше**: замовники зчіплюють підстави в один рядок через крапку з
    комою, і першою йде головна. За однакової позиції виграє довший збіг —
    те саме правило, що й у :mod:`app.core.products`.
    """
    for text in (reason, explanation):
        text = (text or "").strip()
        if not text:
            continue
        best: tuple[int, int, str] | None = None
        for label, patterns in _COMPILED:
            for pattern in patterns:
                found = pattern.search(text)
                if found is None:
                    continue
                key = (found.start(), -(found.end() - found.start()), label)
                if best is None or key < best:
                    best = key
        if best is not None:
            return best[2]
        return OTHER
    return UNSTATED


def marks(reason: str, explanation: str = "") -> list[str]:
    """Які документи згадані у формулюванні — сертифікат, авторизація тощо.

    Тут, на відміну від категорії, читаємо обидва поля: підстава називає
    норму закону, а конкретний папір видно саме в поясненні.
    """
    text = f"{reason or ''} {explanation or ''}"
    if not text.strip():
        return []
    return [label for label, patterns in _MARKS
            if any(pattern.search(text) for pattern in patterns)]


def cancel_label(reason_type: str) -> str:
    """Людська назва підстави відміни закупівлі."""
    code = (reason_type or "").strip()
    if not code:
        return "Підставу не зазначено"
    return CANCEL_LABELS.get(code, code)


def is_rejected(status: str) -> bool:
    """Чи це рішення — саме відхилення переможця.

    Свідомо вузько: ``cancelled`` сюди не входить (див. модульний коментар).
    """
    return str(status or "").strip().lower() == REJECTED_STATUS


# --- розділ звіту ---------------------------------------------------------
#
# Нижче — домішка до :class:`app.core.insight.Analyzer`. Вище лежить чистий
# розбір тексту, який нічого не знає ні про звіт, ні про вибірку; тут із нього
# складаються звичні ``Block`` — так само, як :mod:`app.core.outlook` складає
# їх із відповідей :mod:`app.core.forecast`.

from collections import Counter, defaultdict          # noqa: E402
from typing import Any                                # noqa: E402

from .report import (                                 # noqa: E402
    Block, ChartData, Series, Sheet, compact, count, pct, plural, share,
)

#: Скільки рядків показуємо у переліку випадків.
DETAIL_ROWS = 300
#: Скільки позицій лишаємо на діаграмах.
TOP_ROWS = 12
#: Стани рішення, які означають ухвалений результат розгляду. Саме вони —
#: знаменник частки відмов: ``cancelled`` сюди не входить (це той самий випадок
#: удруге), ``pending`` — теж (розгляд ще триває).
DECIDED = ("active", REJECTED_STATUS)


class RejectionMixin:
    """Розділ «Відмови»: чому перемоги не стають договорами."""

    def rejections(self) -> None:
        self.rejection_rows = self._rejection_rows()
        self.rejections_by_edrpou: dict[str, list[dict]] = defaultdict(list)
        self.rejections_by_tender: dict[str, list[dict]] = defaultdict(list)
        for row in self.rejection_rows:
            self.rejections_by_edrpou[row["edrpou"]].append(row)
            self.rejections_by_tender[row["tender_id"]].append(row)

        if self.rejection_rows:
            self.report.add("Відмови", self._rejection_block())
        cancelled = self._cancellation_block()
        if cancelled is not None:
            self.report.add("Відмови", cancelled)
        self._step("Відмови розібрано", 4)

    # --- дані -------------------------------------------------------------

    def _rejection_rows(self) -> list[dict]:
        """Відхилені рішення про переможця з розпізнаною причиною."""
        rows: list[dict] = []
        for award in self.data.awards:
            if not is_rejected(award.get("status")) or not award.get("edrpou"):
                continue
            tender_id = award.get("tender_id") or ""
            tender = self.tenders.get(tender_id, {})
            reason = str(award.get("reason") or "")
            explanation = str(award.get("explanation") or "")
            day = award.get("decided") or tender.get("date", "")
            rows.append({
                "tender_id": tender_id,
                "edrpou": award["edrpou"],
                "name": award.get("name") or self.names.get(award["edrpou"], ""),
                "amount": float(award.get("amount") or 0),
                "date": day,
                "month": day[:7],
                "buyer": tender.get("buyer", ""),
                "buyer_edrpou": tender.get("buyer_edrpou", ""),
                "region": tender.get("region", ""),
                "method": tender.get("method", ""),
                "title": tender.get("title", ""),
                "reason": reason,
                "explanation": explanation,
                "category": classify(reason, explanation),
                "marks": marks(reason, explanation),
                "ours": award["edrpou"] in self.own,
            })
        return rows

    def _decisions(self) -> int:
        """Скільки рішень про переможця взагалі ухвалено у вибірці."""
        return sum(1 for award in self.data.awards
                   if str(award.get("status") or "").lower() in DECIDED)

    # --- блок «Скасовані перемоги» ---------------------------------------

    def _rejection_block(self) -> Block:
        rows = self.rejection_rows
        decisions = self._decisions()
        by_category: Counter[str] = Counter()
        money_by_category: dict[str, float] = defaultdict(float)
        firms_by_category: dict[str, set[str]] = defaultdict(set)
        sample: dict[str, Counter] = defaultdict(Counter)
        by_month: Counter[str] = Counter()
        found_marks: Counter[str] = Counter()
        for row in rows:
            by_category[row["category"]] += 1
            money_by_category[row["category"]] += row["amount"]
            firms_by_category[row["category"]].add(row["edrpou"])
            if row["reason"]:
                sample[row["category"]][row["reason"]] += 1
            if row["month"]:
                by_month[row["month"]] += 1
            found_marks.update(row["marks"])

        total_lost = sum(row["amount"] for row in rows)
        firms = {row["edrpou"] for row in rows}
        tenders = {row["tender_id"] for row in rows}
        buyers = {row["buyer_edrpou"] for row in rows if row["buyer_edrpou"]}
        ours = [row for row in rows if row["ours"]]

        block = Block(
            "Скасовані перемоги",
            "Рішення про переможця, які замовник відхилив: компанія вже виграла, "
            "але договору не буде. Причину замовник зобов'язаний назвати, і вона "
            "лежить у самій картці закупівлі.")
        block.tiles = [
            ("Скасовано перемог", count(len(rows))),
            ("Частка рішень", pct(share(len(rows), decisions)) if decisions else "—"),
            # Саме «сума скасованих перемог», а не «сума, що зірвалася»: коли в
            # одній закупівлі поспіль відхилили двох переможців, у суму входять
            # обидва. Гроші ринку від цього не зникають — закупівля йде далі.
            ("Сума скасованих перемог", compact(total_lost) + " грн"),
            ("Закупівель зачепило", count(len(tenders))),
            ("Компаній зачепило", count(len(firms))),
            ("Замовників", count(len(buyers))),
            ("Головна причина", by_category.most_common(1)[0][0]),
        ]
        if self.own:
            block.tiles.append(
                ("Наших перемог зірвано",
                 f"{count(len(ours))} на {compact(sum(r['amount'] for r in ours))} грн"
                 if ours else "жодної"))

        block.notes = self._rejection_notes(rows, decisions, ours, found_marks, tenders)
        block.charts = self._rejection_charts(by_category, money_by_category, by_month)
        block.tables = [
            ("Причини", self._reason_sheet(by_category, money_by_category,
                                           firms_by_category, sample, len(rows))),
            ("Компанії", self._firm_sheet()),
            ("Замовники", self._buyer_sheet()),
            ("Випадки", self._case_sheet()),
        ]
        return block

    def _rejection_notes(self, rows: list[dict], decisions: int, ours: list[dict],
                         found_marks: Counter, tenders: set[str]) -> list[str]:
        notes: list[str] = []
        if decisions:
            notes.append(
                f"Із {count(decisions)} "
                f"{plural(decisions, 'рішення', 'рішень', 'рішень')} про переможця "
                f"відхилено {count(len(rows))} ({pct(share(len(rows), decisions))}) у "
                f"{count(len(tenders))} "
                f"{plural(len(tenders), 'закупівлі', 'закупівлях', 'закупівлях')} на "
                f"{compact(sum(r['amount'] for r in rows))} грн.")
        # Сума рішень і сума грошей — різні речі, і різниця не теоретична:
        # виміряно на 2 000 випадкових закупівель країни — 31% суми припадає на
        # повторні відхилення в межах тієї самої закупівлі.
        crowded = sum(1 for _tid, n in Counter(r["tender_id"] for r in rows).items() if n > 1)
        if crowded:
            notes.append(
                f"У {crowded} {plural(crowded, 'закупівлі', 'закупівлях', 'закупівлях')} "
                "поспіль відхилили більш ніж одного переможця, тож у сумі такі "
                "закупівлі враховані кілька разів. Це сума скасованих рішень, а не "
                "грошей, що зникли з ринку: закупівля зазвичай іде до наступного "
                "учасника.")
        top = Counter(row["category"] for row in rows).most_common(3)
        if top:
            notes.append("Найчастіші причини: " + "; ".join(
                f"{name} — {n} ({pct(share(n, len(rows)))})" for name, n in top) + ".")
        if ours:
            reasons = Counter(row["category"] for row in ours).most_common(2)
            notes.append(
                f"Наших ТОВ це зачепило {len(ours)} "
                f"{plural(len(ours), 'раз', 'рази', 'разів')} на "
                f"{compact(sum(r['amount'] for r in ours))} грн: "
                + "; ".join(f"{name} — {n}" for name, n in reasons) + ".")
        if found_marks:
            notes.append(
                "У формулюваннях згадано: " + ", ".join(
                    f"{name} — {n}" for name, n in found_marks.most_common())
                + ". Це не окрема причина, а те, об який саме документ спіткнулися.")
        if len(rows) > DETAIL_ROWS:
            notes.append(
                f"У переліку випадків показано {DETAIL_ROWS} найбільших із "
                f"{count(len(rows))}. Повний перелік — на аркуші «Переможці» книги "
                "даних, там кожне рішення з підставою окремим рядком.")
        blank = sum(1 for row in rows if not row["reason"] and not row["explanation"])
        if blank:
            notes.append(
                f"У {blank} {plural(blank, 'випадку', 'випадках', 'випадках')} "
                f"з {len(rows)} замовник не залишив тексту підстави — їх зведено "
                "в «Причину не зазначено».")
        if "reason" in self.data.missing_columns.get("Переможці", ()):
            notes.append(
                "У цій книзі немає колонки «Причина відмови» — вона з'явилася разом "
                "із розбором відмов. Видно лише сам факт скасування; щоб побачити "
                "причини, зробіть нове вивантаження.")
        notes.append(
            "Рахуємо лише відхилення. Скасоване рішення — це та сама подія вдруге: "
            "замовник спершу скасовує власне рішення про переможця й аж потім "
            "оприлюднює відхилення. Виміряно на вибірці: 51 зі 55 скасованих "
            "рішень мають таку пару, тож рахувати їх окремо означало б завищити "
            "кількість відмов на три чверті.")
        return notes

    def _rejection_charts(self, by_category: Counter, money: dict[str, float],
                          by_month: Counter) -> list[ChartData]:
        charts: list[ChartData] = []
        top = by_category.most_common(TOP_ROWS)
        charts.append(ChartData(
            "Причини відмов", "pie",
            [Series("Випадків", [name for name, _n in top], [n for _name, n in top])],
            unit="шт", money_axis=False,
            hint="Чому саме перемога не стала договором."))
        by_money = sorted(money.items(), key=lambda kv: -kv[1])[:TOP_ROWS]
        if any(value for _name, value in by_money):
            charts.append(ChartData(
                "Сума зірваних перемог за причинами", "hbar",
                [Series("Сума", [name for name, _v in by_money],
                        [value for _n, value in by_money])], unit="грн",
                hint="Та сама причина може траплятися рідко, але коштувати найдорожче."))
        if len(by_month) >= 2:
            labels = sorted(by_month)
            charts.append(ChartData(
                "Скасовані перемоги по місяцях", "line",
                [Series("Випадків", labels, [by_month[m] for m in labels])],
                unit="шт", money_axis=False))
        return charts

    def _reason_sheet(self, by_category: Counter, money: dict[str, float],
                      firms: dict[str, set[str]], sample: dict[str, Counter],
                      total: int) -> Sheet:
        headers = ["Причина", "Випадків", "Частка", "Сума, грн", "Компаній",
                   "Приклад формулювання"]
        rows: list[list[Any]] = []
        for name, n in by_category.most_common():
            example = sample[name].most_common(1)
            rows.append([name, n, round(share(n, total), 4), round(money[name], 2),
                         len(firms[name]), example[0][0][:300] if example else ""])
        return headers, rows

    def _firm_sheet(self) -> Sheet:
        """Хто саме втрачає перемоги і як часто.

        Знаменник — виграні цією компанією закупівлі плюс її ж відмови, а не
        всі рішення ринку: питання тут «як часто виграш саме цієї компанії
        зривається».

        Перемоги рахуємо з ``clean_deals``, а не з ``deals_by_edrpou``: цей
        розділ іде **перед** профілями гравців, бо вони самі беруть із нього
        числа, тож на цей момент розкладених по компаніях угод ще немає.
        """
        headers = ["ЄДРПОУ", "Компанія", "Скасовано перемог", "Виграно закупівель",
                   "Частка зривів", "Сума, грн", "Головна причина", "Наша"]
        wins: dict[str, set[str]] = defaultdict(set)
        for deal in self.clean_deals:
            wins[deal["edrpou"]].add(deal["tender_id"])
        rows: list[list[Any]] = []
        for edrpou, items in self.rejections_by_edrpou.items():
            won = len(wins.get(edrpou, ()))
            attempts = won + len(items)
            top = Counter(row["category"] for row in items).most_common(1)[0][0]
            rows.append([
                edrpou, self.names.get(edrpou) or items[0]["name"], len(items), won,
                round(share(len(items), attempts), 4) if attempts else None,
                round(sum(row["amount"] for row in items), 2), top,
                "так" if edrpou in self.own else "",
            ])
        rows.sort(key=lambda row: (-row[2], -(row[5] or 0)))
        return headers, rows

    def _buyer_sheet(self) -> Sheet:
        headers = ["Замовник", "ЄДРПОУ", "Скасовано перемог", "Сума, грн",
                   "Компаній", "Головна причина"]
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in self.rejection_rows:
            grouped[row["buyer_edrpou"] or row["buyer"]].append(row)
        rows: list[list[Any]] = []
        for key, items in grouped.items():
            top = Counter(row["category"] for row in items).most_common(1)[0][0]
            rows.append([items[0]["buyer"] or key, items[0]["buyer_edrpou"], len(items),
                         round(sum(row["amount"] for row in items), 2),
                         len({row["edrpou"] for row in items}), top])
        rows.sort(key=lambda row: (-row[2], -row[3]))
        return headers, rows

    def _case_sheet(self) -> Sheet:
        headers = ["Закупівля", "Дата", "Компанія", "ЄДРПОУ", "Замовник", "Сума, грн",
                   "Причина", "Формулювання замовника", "Пояснення"]
        rows = [[row["tender_id"], row["date"], row["name"], row["edrpou"], row["buyer"],
                 round(row["amount"], 2), row["category"], row["reason"][:500],
                 row["explanation"][:500]]
                for row in sorted(self.rejection_rows, key=lambda r: -r["amount"])]
        return headers, rows[:DETAIL_ROWS]

    # --- блок «Відмінені закупівлі» --------------------------------------

    def _cancellation_block(self) -> Block | None:
        rows = [row for row in self.data.cancellations if row.get("tender_id")]
        if not rows:
            return None
        by_reason: Counter[str] = Counter()
        money: dict[str, float] = defaultdict(float)
        for row in rows:
            name = str(row.get("reason") or "").strip() or cancel_label("")
            by_reason[name] += 1
            money[name] += float(row.get("value") or 0)
        block = Block(
            "Відмінені закупівлі",
            "Тут перемогу забирає не рішення про учасника, а зникнення самої "
            "закупівлі. Підстава відміни — з переліку, тож її не доводиться "
            "розпізнавати за текстом.")
        block.tiles = [
            ("Відмінено закупівель", count(len({row["tender_id"] for row in rows}))),
            ("Частка вибірки",
             pct(share(len({row["tender_id"] for row in rows}), len(self.tenders)))
             if self.tenders else "—"),
            ("Очікувана вартість", compact(sum(money.values())) + " грн"),
            ("Головна підстава", by_reason.most_common(1)[0][0]),
        ]
        top = by_reason.most_common(TOP_ROWS)
        block.charts = [ChartData(
            "Підстави відміни", "pie",
            [Series("Закупівель", [name for name, _n in top], [n for _name, n in top])],
            unit="шт", money_axis=False)]
        block.tables = [
            ("Підстави", (["Підстава", "Закупівель", "Очікувана вартість, грн"],
                          [[name, n, round(money[name], 2)]
                           for name, n in by_reason.most_common()])),
            ("Випадки", (["Закупівля", "Дата відміни", "Замовник",
                          "Очікувана вартість, грн", "Підстава", "Обґрунтування"],
                         [[row.get("tender_id"), row.get("cancelled") or row.get("date"),
                           row.get("buyer"), round(float(row.get("value") or 0), 2),
                           row.get("reason"), str(row.get("explanation") or "")[:500]]
                          for row in rows[:DETAIL_ROWS]])),
        ]
        return block
