"""Відповідність ``tenderID`` (UA-2026-…) → внутрішній UUID Центральної бази.

Пошуковий API порталу віддає лише людський номер закупівлі, а всі файли
живуть у ЦБД, яка адресує закупівлі за UUID. Тут три способи це подолати:

* :meth:`Resolver.from_index` — локальний індекс (найшвидше, якщо побудований);
* :meth:`Resolver.from_contracts` — через договір: у пошуку договорів є UUID
  договору, а картка договору в ЦБД містить ``tender_id``. Один запит на
  двадцять закупівель + один запит на закупівлю. Працює для закупівель,
  за якими вже є договір (переважна більшість завершених);
* :class:`IndexBuilder` — повний обхід стрічки змін ЦБД за період. Повільніше,
  зате покриває геть усі закупівлі, зокрема скасовані та ті, що тривають.
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Callable, Iterable

from .api import SUMMARY_URL, CdbApi, SearchApi
from .db import Database
from .http import Cancelled, HttpClient

#: ``UA-2026-08-13-007779-a-a1`` → ``UA-2026-08-13-007779-a``
CONTRACT_SUFFIX = re.compile(r"-[a-z]\d+$", re.IGNORECASE)

ProgressCb = Callable[[str, int, int], None]
LogCb = Callable[[str, str], None]


def tender_id_of(contract_id: str) -> str:
    return CONTRACT_SUFFIX.sub("", contract_id or "")


class Resolver:
    def __init__(self, client: HttpClient, db: Database, *,
                 concurrency: int = 8, on_log: LogCb | None = None):
        self.client = client
        self.db = db
        self.search = SearchApi(client)
        self.cdb = CdbApi(client)
        self.concurrency = max(1, concurrency)
        #: Запити до пошукового порталу йдуть меншим числом потоків: темп однаково
        #: обмежує сервер, а сплеск паралельних запитів лише ловить 429.
        self.search_concurrency = max(1, min(3, concurrency))
        self._log = on_log or (lambda level, msg: None)
        self._lock = threading.Lock()

    # --- 1. локальний індекс ---------------------------------------------

    def from_index(self, tender_ids: Iterable[str]) -> dict[str, str]:
        return self.db.index_lookup(list(tender_ids))

    # --- 2. прямий розв'язувач порталу ------------------------------------

    def from_summary(self, tender_ids: Iterable[str],
                     progress: ProgressCb | None = None) -> dict[str, str]:
        """``{tenderID: uuid}`` — по одному запиту на закупівлю.

        Портал приймає людський номер напряму (:data:`SUMMARY_URL`) і віддає
        картку з внутрішнім ``id``. Влучність — стовідсоткова, на відміну від
        колишнього шляху через договори, який знаходив лише ті закупівлі, за
        якими договір уже є.

        Ціна — квота порталу: один запит на закупівлю з 58 доступних на
        хвилину. Тому спосіб вигідний лише для невеликих вибірок; вибір між
        ним і повним індексом робить :meth:`Pipeline.resolve`.
        """
        ids = list(dict.fromkeys(tender_ids))
        found: dict[str, str] = {}
        done = 0

        def worker(tid: str) -> tuple[str, str] | None:
            try:
                data = self.client.get_json(SUMMARY_URL.format(tender_id=tid))
            except Cancelled:
                raise
            except Exception as exc:
                self._log("warn", f"{tid}: картку не знайдено — {exc}")
                return None
            uuid = (data or {}).get("id") or ""
            return (tid, uuid) if uuid else None

        with ThreadPoolExecutor(self.search_concurrency) as pool:
            futures = [pool.submit(worker, tid) for tid in ids]
            for future in as_completed(futures):
                done += 1
                result = future.result()
                if result:
                    found[result[0]] = result[1]
                if progress:
                    progress("Розпізнавання закупівель", done, len(ids))
        self._store(found)
        return found

    # --- збереження ------------------------------------------------------

    def _store(self, mapping: dict[str, str]) -> None:
        if mapping:
            self.db.index_put([(tid, uuid, None, None, None, None, None, None, None)
                               for tid, uuid in mapping.items()])


class IndexBuilder:
    """Повний обхід стрічки змін ЦБД для побудови індексу за період.

    Стрічка впорядкована за ``dateModified``, тож щоб знайти всі закупівлі,
    створені з дати ``since``, треба пройти стрічку від ``since`` до сьогодні.
    Діапазон ріжеться на місячні відрізки, які обходяться паралельно, а
    пройдені доби позначаються в таблиці ``index_coverage`` — тому побудову
    можна зупинити й продовжити пізніше.
    """

    def __init__(self, client: HttpClient, db: Database, *,
                 shards: int = 6, on_log: LogCb | None = None,
                 keep_all: bool = True, wanted: set[str] | None = None):
        self.client = client
        self.db = db
        self.cdb = CdbApi(client)
        self.shards = max(1, shards)
        self._log = on_log or (lambda level, msg: None)
        self.keep_all = keep_all
        self.wanted = wanted
        self.records = 0
        self._lock = threading.Lock()

    @staticmethod
    def month_chunks(since: date, until: date) -> list[tuple[date, date]]:
        """Період помісячно. Лишено для сумісності; ``build`` бере :meth:`date_chunks`."""
        chunks: list[tuple[date, date]] = []
        cur = since.replace(day=1)
        while cur <= until:
            nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
            chunks.append((max(cur, since), min(nxt - timedelta(days=1), until)))
            cur = nxt
        return chunks

    @staticmethod
    def date_chunks(since: date, until: date, shards: int) -> list[tuple[date, date]]:
        """Ріже період на відрізки — принаймні по одному на потік.

        Стрічка гортається курсором (``next_page``), тож усередині відрізка
        паралелізму немає: єдиний спосіб задіяти кілька потоків — дати кожному
        свій відрізок. Раніше різали строго помісячно, і на короткому періоді
        виходив один відрізок: вісім діб стрічки гортались одним потоком, хоч
        у налаштуваннях і стояло чотири. Виміряно, що побудова індексу на 99%
        складається з очікування мережі, тож саме тут і лежить пришвидшення.
        """
        total_days = (until - since).days + 1
        if total_days <= 0:
            return []
        parts = max(1, min(max(1, shards), total_days))
        size = -(-total_days // parts)          # ділення з округленням угору
        chunks: list[tuple[date, date]] = []
        cur = since
        while cur <= until:
            end = min(cur + timedelta(days=size - 1), until)
            chunks.append((cur, end))
            cur = end + timedelta(days=1)
        return chunks

    def missing_days(self, since: date, until: date) -> list[str]:
        covered = self.db.coverage_days()
        days: list[str] = []
        cur = since
        while cur <= until:
            key = cur.isoformat()
            if key not in covered:
                days.append(key)
            cur += timedelta(days=1)
        return days

    def build(self, since: date, until: date | None = None,
              progress: ProgressCb | None = None) -> int:
        """Будує індекс за період. Повертає кількість записів, доданих цього разу."""
        until = until or date.today()
        chunks = [c for c in self.date_chunks(since, until, self.shards)
                  if self.missing_days(c[0], c[1])]
        if not chunks:
            self._log("info", "Індекс за цей період уже побудовано.")
            return 0

        total_days = sum((c[1] - c[0]).days + 1 for c in chunks)
        done_days = 0
        seen_records = 0
        self._log("info", f"Побудова індексу: {len(chunks)} відрізків, {total_days} діб.")

        def note() -> None:
            """Звіт про поступ на кожній сторінці стрічки.

            Повідомляти лише на зміні доби не годиться: відрізок завдовжки в
            одну добу не змінює її жодного разу, і вікно завмирало б на
            попередньому етапі на весь час індексації.

            Відсоток рахуємо за завершеними добами, а рух між ними показуємо
            лічильником записів. Дату в підписі не показуємо навмисно: відрізки
            обходяться паралельно, і вона стрибала б туди-сюди між потоками.
            """
            if not progress:
                return
            count = f"{seen_records:,}".replace(",", " ")
            progress(f"Індексація: {count} записів стрічки",
                     min(done_days, total_days), total_days)

        def worker(chunk: tuple[date, date]) -> int:
            nonlocal done_days, seen_records
            start, end = chunk
            count = 0
            batch: list[tuple] = []
            last_day = start.isoformat()
            try:
                for rows in self.cdb.feed(start.isoformat()):
                    if not rows:
                        break
                    with self._lock:
                        seen_records += len(rows)
                    note()
                    stop = False
                    for row in rows:
                        modified = (row.get("dateModified") or "")[:10]
                        if modified and modified > end.isoformat():
                            stop = True
                            break
                        tid = row.get("tenderID")
                        uuid = row.get("id")
                        if tid and uuid and (self.keep_all or (self.wanted and tid in self.wanted)):
                            # Крім пари tender_id → uuid зі стрічки береться
                            # тип процедури: ним добираються закупівлі, назву
                            # процедури яких пошук порталу не приймає. Решта
                            # колонок лишається порожньою з часів, коли стрічку
                            # тягнули з усіма полями.
                            batch.append((tid, uuid, "", modified, "",
                                          row.get("procurementMethodType") or "",
                                          "", "", ""))
                        count += 1
                        if modified and modified != last_day:
                            # Добу можна вважати покритою лише тоді, коли ми
                            # зберегли всі її записи: інакше наступний запуск
                            # пропустить її й не знайде інших закупівель.
                            with self._lock:
                                if self.keep_all:
                                    self.db.coverage_mark(last_day, 0, True)
                                done_days += 1
                            note()
                            last_day = modified
                    if len(batch) >= 20000:
                        self.db.index_put(batch)
                        batch.clear()
                    if stop:
                        break
                if batch:
                    self.db.index_put(batch)
                # Поточну добу не позначаємо завершеною — вона ще триває.
                if self.keep_all and end < date.today():
                    with self._lock:
                        self.db.coverage_mark(end.isoformat(), count, True)
            except Cancelled:
                raise
            except Exception as exc:
                self._log("error", f"Індексація {start}–{end} перервалася: {exc}")
            return count

        with ThreadPoolExecutor(min(self.shards, len(chunks))) as pool:
            futures = {pool.submit(worker, chunk): chunk for chunk in chunks}
            for future in as_completed(futures):
                self.records += future.result()
                start, end = futures[future]
                with self._lock:
                    # Доби всередині відрізка вже полічені на зміні дати;
                    # лишається остання, якою відрізок і завершується.
                    done_days = min(total_days, done_days + 1)
                note()
        if progress:
            progress("Індексацію завершено", total_days, total_days)
        self._log("info", f"Індекс: опрацьовано {self.records:,} записів стрічки.".replace(",", " "))
        return self.records
