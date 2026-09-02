"""Конвеєр: пошук → розпізнавання → картки закупівель → файли."""
from __future__ import annotations

import json
import threading
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ..config import METHOD_LABELS, Settings, SearchPreset
from ..paths import export_path, long_path
from .api import (
    SEARCH_MAX_PAGE, SEARCH_MAX_RESULTS, SEARCH_PAGE_SIZE, CdbApi, SearchApi,
)
from .classifiers import expand_prefixes
from .db import Database
from .downloader import (
    FileDownloader, document_extension, is_signature, normalize_extensions, tender_folder,
)
from .exporter import write_xlsx
from .extract import latest_versions, parse_tender
from .http import Cancelled, HttpClient
from .market import MarketApi, class_codes, parse_product
from .resolver import IndexBuilder, Resolver

LogCb = Callable[[str, str], None]
ProgressCb = Callable[[str, int, int], None]
StatsCb = Callable[[dict], None]

#: Максимальна довжина значення proc_type у пошуку — сервер відхиляє довші
#: з HTTP 422. Одна назва процедури (`closeFrameworkAgreementSelectionUA`) у неї
#: не вміщається, тож у розрізі за процедурами вона недоступна.
PROC_TYPE_MAX_LEN = 30

#: Скільки поспіль «застарих» сторінок терпимо, перш ніж зупинити гортання.
STALE_PAGE_TOLERANCE = 3

#: Скільки невдалих карток каталогу називаємо поштучно. Далі — лише підсумок:
#: журнал на дві тисячі однакових рядків не читає ніхто, а перші кілька
#: показують, що саме сталося.
MARKET_WARN_LIMIT = 5

#: На скільки днів гортаємо глибше за початок періоду.
#:
#: Відколи відбір, зупинка й упорядкування видачі спираються на одну дату
#: (:meth:`Pipeline._published`), запас потрібен лише на межу доби: дати в
#: номері й у ``dateCreated`` записані в місцевому часі, а картка могла бути
#: створена опівночі. Раніше тут стояло 30 днів — саме стільки доводилося
#: гортати намарно, бо пошук фільтрував за однією датою, а сортував за іншою.
LOOKBACK_SLACK_DAYS = 2


@dataclass
class JobResult:
    found: int = 0                 # знайдено в пошуку (після фільтра за періодом)
    resolved: int = 0              # для скількох вдалося визначити UUID
    unresolved: list[str] = field(default_factory=list)
    tenders_loaded: int = 0        # завантажено повних карток
    documents: int = 0             # усього документів у картках
    files_ok: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    files_filtered: int = 0        # відсіяно фільтром типів (підписи тощо)
    products: int = 0              # карток товарів е-каталогу
    bytes: int = 0
    table: str = ""                # шлях до створеної таблиці
    cancelled: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["unresolved"] = len(self.unresolved)
        return d


class Pipeline:
    def __init__(self, settings: Settings, preset: SearchPreset, db: Database, *,
                 on_log: LogCb | None = None, on_progress: ProgressCb | None = None,
                 on_stats: StatsCb | None = None, on_tender: Callable[[dict], None] | None = None,
                 cancel_event: threading.Event | None = None):
        self.s = settings
        self.p = preset
        self.db = db
        self.cancel = cancel_event or threading.Event()
        self._log = on_log or (lambda level, msg: None)
        self._progress = on_progress or (lambda stage, done, total: None)
        self._stats = on_stats or (lambda stats: None)
        self._on_tender = on_tender or (lambda row: None)

        self.client = HttpClient(
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
            rps=settings.rate_limit_rps,
            pool_size=max(16, settings.download_concurrency + settings.detail_concurrency),
            cancel_event=self.cancel,
            on_log=self._log,
        )
        self.search = SearchApi(self.client)
        self.cdb = CdbApi(self.client)
        self.market = MarketApi(self.client)
        self.resolver = Resolver(self.client, db, concurrency=settings.detail_concurrency,
                                 on_log=self._log)
        self.result = JobResult()
        self._lock = threading.Lock()
        #: Процедури, які довелося пропустити в розрізі за процедурою, бо
        #: пошук не приймає їхніх назв. Наповнює крок пошуку, читає
        #: :meth:`pickup_unnamed`.
        self.unnamed_methods: set[str] = set()
        self._codes_cache: list[str] | None = None
        self._codes_set: set[str] = set()
        self._status_set = set(preset.statuses or [])
        self._method_set = set(preset.methods or [])
        self._region_set = set(preset.regions or [])
        self._tenderer_set = {e for e in (preset.tenderers or []) if e}

    # --- допоміжне --------------------------------------------------------

    def _tick(self) -> None:
        self._stats({**self.client.stats.snapshot(), **self.result.as_dict()})

    @property
    def _date_from(self) -> str:
        return (self.p.date_from or "1900-01-01")[:10]

    @property
    def _date_to(self) -> str:
        return (self.p.date_to or date.today().isoformat())[:10]

    def _cpv_codes(self) -> list[str]:
        """Повний перелік кодів ДК021 за обраними гілками (рахується один раз)."""
        if self._codes_cache is None:
            codes = set(self.p.cpv_codes or [])
            codes.update(expand_prefixes(self.p.cpv_prefixes or []))
            self._codes_cache = sorted(codes)
            self._codes_set = set(self._codes_cache)
        return self._codes_cache

    @staticmethod
    def _published(card: dict) -> str:
        """Дата оприлюднення закупівлі, як її бачить пошук.

        Це наріжна дата всього пошуку, і вона одна: за нею портал упорядковує
        видачу, за нею ж ми відбираємо закупівлі в період, і їй дорівнює
        ``dateCreated`` у Центральній базі — саме за ним потім фільтрує
        :meth:`_passes_filters`. Перевірено на живих даних: збіг повний.

        Надійніше за все вона закодована в самому номері
        (``UA-2026-08-21-010792-a``); ``enquiryPeriod.startDate`` — запасний
        варіант для карток із нетиповим номером.

        Раніше тут поверталося ``tenderPeriod.startDate`` — початок подання
        пропозицій. Це інша дата: закупівлю оголошують 21 серпня, а пропозиції
        приймають з 27-го. Через розбіжність пошук викидав закупівлі, які
        остаточний відбір мав прийняти, і доводилося гортати на місяць глибше
        за період, аби їх наздогнати.
        """
        tid = card.get("tenderID") or ""
        if len(tid) > 13:
            return tid[3:13]
        for key in ("enquiryPeriod", "tenderPeriod", "auctionPeriod"):
            node = card.get(key) or {}
            if node.get("startDate"):
                return str(node["startDate"])[:10]
        return ""

    @property
    def _stop_before(self) -> str:
        """Дата, глибше за яку гортати вже немає сенсу."""
        return (_as_date(self._date_from) - timedelta(days=LOOKBACK_SLACK_DAYS)).isoformat()

    # --- крок 1: пошук ----------------------------------------------------

    def discover(self) -> dict[str, dict]:
        """Гортає пошук порталу і повертає ``{tenderID: картка}`` у межах періоду."""
        return _Discovery(self).run()

    # --- крок 2: розпізнавання UUID --------------------------------------

    def resolve(self, tender_ids: list[str]) -> dict[str, str]:
        """``tenderID`` → внутрішній UUID, потрібний Центральній базі.

        Способів два, і вони мають протилежну вартість:

        * **прямий розв'язувач порталу** — один запит на закупівлю, влучність
          стовідсоткова, але витрачає квоту порталу (58 запитів на хвилину);
        * **повний індекс** — обхід стрічки змін ЦБД за період. Квоти не
          витрачає зовсім, зате коштує пропорційно довжині періоду, а не
          розміру вибірки: за добу в стрічці близько 13 тисяч записів.

        Тому вибираємо за розміром: поки закупівель менше, ніж коштує індекс,
        дешевше опитати кожну; далі виграє індекс.

        Раніше тут був третій шлях — через пошук договорів. Заміряно, що він
        витіснений обома: на тій самій вибірці з 504 закупівель він разом з
        індексом дав ті самі 477 за 80 секунд і 19 запитів до порталу, тоді як
        сам лише індекс дав ті самі 477 за 13 секунд і без жодного запиту.
        Він ще й знаходив тільки ті закупівлі, за якими договір уже укладено.
        """
        mapping = self.resolver.from_index(tender_ids)
        if mapping:
            self._log("info", f"З локального індексу розпізнано {_spaced(len(mapping))}.")
        missing = [t for t in tender_ids if t not in mapping]
        mode = self.s.resolve_mode

        if missing and mode in ("auto", "summary"):
            if mode == "summary" or len(missing) <= self._summary_budget():
                self._log("info", f"Розпізнаю {_spaced(len(missing))} закупівель "
                                  f"напряму через портал.")
                mapping.update(self.resolver.from_summary(missing, self._progress))
                missing = [t for t in tender_ids if t not in mapping]

        if missing and mode in ("auto", "index"):
            self._log("info", f"Лишилося нерозпізнаних: {_spaced(len(missing))}. "
                              f"Будую індекс стрічки змін ЦБД.")
            builder = IndexBuilder(
                self.client, self.db, shards=self.s.index_concurrency,
                on_log=self._log, keep_all=self.s.keep_full_index, wanted=set(missing),
            )
            builder.build(_as_date(self._date_from), date.today(), self._progress)
            mapping.update(self.resolver.from_index(missing))
            missing = [t for t in tender_ids if t not in mapping]

            # Кількох закупівель у стрічці змін не буває — зазвичай це щойно
            # оголошені. Портал їх знає, і Центральна база картку віддає, тож
            # решту добираємо поштучно: на місячному періоді це десятки
            # запитів, які піднімають повноту з 98% до 100%.
            if missing and len(missing) <= self._summary_budget():
                self._log("info", f"Добираю {_spaced(len(missing))} закупівель, яких "
                                  f"немає у стрічці змін, напряму через портал.")
                mapping.update(self.resolver.from_summary(missing, self._progress))
                missing = [t for t in tender_ids if t not in mapping]

        self.result.resolved = len(mapping)
        self.result.unresolved = missing
        if missing:
            self._log("warn",
                      f"Розпізнано {_spaced(len(mapping))} із {_spaced(len(tender_ids))} "
                      f"закупівель. Решта {len(missing)} не знайшлася ні у стрічці змін "
                      f"Центральної бази, ні поштучно на порталі — таке буває з "
                      f"чернетками та щойно скасованими.")
        self._tick()
        return mapping

    def pickup_unnamed(self, known: dict[str, str]) -> dict[str, str]:
        """Добирає з індексу закупівлі процедур, яких пошук не вміє назвати.

        Пошук порталу відхиляє значення ``proc_type`` довші за 30 символів
        (``The proc_type.0 may not be greater than 30 characters``), а
        ``closeFrameworkAgreementSelectionUA`` має 34 — тож «Відбір за рамковою
        угодою» через пошук недосяжний, і розріз за процедурою його губить.

        Зате він є у стрічці змін ЦБД, з якої будується індекс: там уже лежать
        і номер, і UUID, і тип процедури. Беремо звідти всі такі закупівлі
        періоду — їх у стрічці 0,05%, тобто сотні на рік, — а відсіє їх далі
        той самий :meth:`_passes_filters`, що й решту: за ДК021, датою,
        регіоном. Дешевше, ніж здається: коди ДК021 стрічка не віддає, тому
        іншого способу, ніж узяти картки й подивитися, немає.
        """
        methods = sorted(self.unnamed_methods)
        if not methods:
            return {}
        names = ", ".join(f"«{METHOD_LABELS.get(m, m)}»" for m in methods)
        blocked = (f"Процедуру {names} пошук назвати не може (назва довша за "
                   f"{PROC_TYPE_MAX_LEN} символів), тому вона добирається з індексу.")
        if not self.s.keep_full_index:
            # З неповним індексом добирати нема звідки: у ньому лежить лише те,
            # що знайшов пошук, а саме цих закупівель він і не знаходить.
            self._log("warn", f"{blocked} Але індекс зберігає лише знайдене пошуком "
                              f"(«повний індекс» у налаштуваннях вимкнено), тож ці "
                              f"закупівлі до вибірки не потраплять.")
            return {}
        rows, with_method = self.db.index_method_coverage(self._date_from, self._date_to)
        if not with_method:
            self._log("warn",
                      f"{blocked} Але в індексі за цей період типу процедури немає — його "
                      f"зібрано раніше, ніж програма почала його зберігати. Щоб добрати ці "
                      f"закупівлі, перебудуйте індекс за період у «Налаштуваннях» "
                      f"(«Очистити індекс» → «Побудувати індекс за період»). Зараз вони до "
                      f"вибірки не потраплять.")
            return {}
        found = self.db.index_by_method(methods, self._date_from, self._date_to)
        fresh = {tid: uuid for tid, uuid in found.items() if tid not in known}
        share = with_method / rows if rows else 0
        gap = ("" if share > 0.99 else
               f" Увага: тип процедури відомий лише для {share:.0%} індексу за період, "
               f"решту зібрано раніше — там могло лишитися ще кілька таких закупівель.")
        self._log("info",
                  f"Процедуру {names} пошук назвати не може, тому добираю її зі стрічки "
                  f"змін: у індексі за період таких закупівель {_spaced(len(found))}, "
                  f"нових для цієї вибірки — {_spaced(len(fresh))}. Далі вони пройдуть "
                  f"ті самі фільтри, що й решта.{gap}")
        return fresh

    def _summary_budget(self) -> int:
        """Скільки закупівель ще дешевше опитати поштучно, ніж будувати індекс.

        Індекс коштує ``діб × 13 000 / 8 500`` секунд, прямий розв'язувач —
        ``закупівель × 60/58`` секунд. Прирівнявши, дістаємо приблизно півтори
        закупівлі на добу періоду. Числа виміряні на живих API; точність тут не
        потрібна — важливо не переплутати порядок величини.
        """
        days = max(1, (_as_date(self._date_to) - _as_date(self._date_from)).days + 1)
        return int(days * 1.5)

    # --- крок 3: повні картки --------------------------------------------

    def load_tenders(self, mapping: dict[str, str]) -> list[dict]:
        """Тягне повні картки з ЦБД, зберігає в БД, повертає рядки для таблиці."""
        pairs = list(mapping.items())
        rows: list[dict] = []
        done = 0
        total = len(pairs)

        root = Path(self.s.output_dir)

        def worker(pair: tuple[str, str]) -> dict | None:
            tender_id, uuid = pair
            try:
                data = self.cdb.tender(uuid)
            except Cancelled:
                raise
            except Exception as exc:
                self._log("warn", f"{tender_id}: не вдалося прочитати картку — {exc}")
                return None
            parsed = parse_tender(data, keep_urls=self.p.download_files)
            if not self._passes_filters(parsed):
                return None
            self.db.save_tender(
                parsed["row"], lots=parsed["lots"], items=parsed["items"],
                bids=parsed["bids"], awards=parsed["awards"],
                contracts=parsed["contracts"], docs=parsed["docs"],
                cancellations=parsed["cancellations"],
            )
            # У режимі «тільки дані» на диску не з'являється нічого: тека з
            # самою лише карткою — це шум, а самі дані вже в базі.
            if self.s.save_tender_json and self.p.download_files:
                row = parsed["row"]
                _write_json(tender_folder(root, row["tender_id"], row["title"],
                                          row["date_created"]), tender_id, data)
            with self._lock:
                self.result.documents += len(parsed["docs"])
            return parsed["row"]

        with ThreadPoolExecutor(self.s.detail_concurrency) as pool:
            futures = [pool.submit(worker, pair) for pair in pairs]
            for future in as_completed(futures):
                done += 1
                row = future.result()
                if row:
                    rows.append(row)
                    self._on_tender(row)
                    # Плитка має рости разом із роботою, а не стрибнути в кінці.
                    self.result.tenders_loaded = len(rows)
                self._progress("Завантаження карток закупівель", done, total)
                if done % 5 == 0:
                    self._tick()
        self.result.tenders_loaded = len(rows)
        self._log("info", f"Завантажено карток: {_spaced(len(rows))}, документів у них: "
                          f"{_spaced(self.result.documents)}")
        self._tick()
        return rows

    def _passes_filters(self, parsed: dict) -> bool:
        row = parsed["row"]
        created = (row.get("date_created") or "")[:10]
        if created and not (self._date_from <= created <= self._date_to):
            return False
        if self._status_set and row.get("status") not in self._status_set:
            return False
        if self._method_set and row.get("method_type") not in self._method_set:
            return False
        if self._region_set and (row.get("pe_region") or "") not in self._region_set:
            return False
        amount = row.get("value_amount") or 0
        if self.p.value_min is not None and amount < self.p.value_min:
            return False
        if self.p.value_max is not None and amount > self.p.value_max:
            return False
        self._cpv_codes()                      # наповнює self._codes_set
        if self._codes_set:
            have = {c for c in (row.get("cpv_list") or "").split(",") if c}
            if have and not (have & self._codes_set):
                return False
        if self._tenderer_set:
            have = {b[8] for b in parsed["bids"] if b[8]} | {a[8] for a in parsed["awards"] if a[8]}
            if not (have & self._tenderer_set):
                return False
        return True

    # --- крок 4: файли ----------------------------------------------------

    def download_files(self, uuids: list[str]) -> None:
        scopes = list(self.p.doc_scopes or [])
        docs = [dict(r) for r in self.db.pending_documents(uuids, scopes)]
        if not self.s.download_all_versions:
            per_tender: dict[str, list[dict]] = {}
            for doc in docs:
                per_tender.setdefault(doc["tender_uuid"], []).append(doc)
            docs = [d for group in per_tender.values() for d in latest_versions(group)]
        self._download(self._apply_file_filter(docs))

    def _apply_file_filter(self, docs: list[dict]) -> list[dict]:
        """Відсіює підписи КЕП і зайві типи файлів, не викидаючи їх із бази."""
        wanted = normalize_extensions(self.p.only_extensions)
        if not self.p.skip_signatures and not wanted:
            return docs

        keep: list[dict] = []
        signatures: list[str] = []
        other: list[str] = []
        for doc in docs:
            if self.p.skip_signatures and is_signature(doc):
                signatures.append(doc["key"])
            elif wanted and document_extension(doc) not in wanted:
                other.append(doc["key"])
            else:
                keep.append(doc)

        if signatures:
            self.db.mark_filtered(signatures, "файл електронного підпису")
            self._log("info", f"Пропущено підписів КЕП: {len(signatures):,}".replace(",", " "))
        if other:
            self.db.mark_filtered(other, "тип файлу поза переліком")
            self._log("info", f"Пропущено за типом файлу: {len(other):,}".replace(",", " "))
        with self._lock:
            self.result.files_filtered = len(signatures) + len(other)
        return keep

    def download_missing(self) -> JobResult:
        """Довантажує все, чого ще немає на диску.

        Сюди потрапляють і файли, що не долетіли, і ті, які лише зафіксовані в
        базі: у режимі «тільки дані» документи зберігаються з позначкою «у
        черзі», і без цього кроку взяти їх було б нічим. Відсіяні фільтром
        типів проходять повторний відбір, тож підписи КЕП і далі не качаються.
        """
        docs = [dict(r) for r in self.db.query(
            "SELECT * FROM documents WHERE state IN ('pending', 'error', 'filtered')")]
        try:
            self._download(self._apply_file_filter(docs))
        except Cancelled:
            self.result.cancelled = True
            self._log("warn", "Зупинено користувачем.")
        except Exception as exc:
            self.result.error = str(exc)
            self._log("error", f"Помилка: {exc}")
        finally:
            self.client.close()
            self._tick()
        return self.result

    def _download(self, docs: list[dict]) -> None:
        if not docs:
            self._log("info", "Файлів для завантаження немає.")
            return
        uuids = sorted({d["tender_uuid"] for d in docs})

        root = Path(self.s.output_dir)
        root.mkdir(parents=True, exist_ok=True)
        dl = FileDownloader(self.client, self.db, root,
                            max_file_mb=self.s.max_file_mb,
                            skip_existing=self.s.skip_existing,
                            on_log=self._log)

        dirs = {
            row["uuid"]: tender_folder(root, row["tender_id"], row["title"], row["date_created"])
            for row in _query_in(
                self.db, "SELECT uuid, tender_id, title, date_created FROM tenders", uuids)
        }

        self._log("info", f"До завантаження {len(docs):,} файл(ів).".replace(",", " "))
        done = 0
        total = len(docs)

        def worker(doc: dict) -> None:
            folder = dirs.get(doc["tender_uuid"]) or (root / "інше")
            dl.fetch(doc, folder)

        with ThreadPoolExecutor(self.s.download_concurrency) as pool:
            futures = [pool.submit(worker, doc) for doc in docs]
            for future in as_completed(futures):
                done += 1
                future.result()
                self._progress("Завантаження файлів", done, total)
                if done % 5 == 0:
                    self.result.files_ok = dl.ok
                    self.result.files_skipped = dl.skipped
                    self.result.files_failed = dl.failed
                    self.result.bytes = dl.bytes
                    self._tick()
        self.result.files_ok = dl.ok
        self.result.files_skipped = dl.skipped
        self.result.files_failed = dl.failed
        self.result.bytes = dl.bytes
        self._log("info", f"Файли: завантажено {dl.ok}, пропущено {dl.skipped}, "
                          f"з помилками {dl.failed}, обсяг {dl.bytes / 1048576:.1f} МБ.")
        self._tick()

    # --- крок 5: картки товарів е-каталогу --------------------------------

    def collect_products(self) -> int:
        """Тягне картки товарів каталогу за тими самими класами ДК021.

        Каталог класифікує товари на рівні класу, тож обрані коди підіймаються
        до нього. Уже відомі й незмінені картки не перечитуються.
        """
        codes = class_codes(self._cpv_codes())
        if not codes:
            self._log("info", "Клас ДК021 не задано — картки товарів пропускаємо.")
            return 0
        self._log("info", f"Каталог товарів: класи {', '.join(codes)}.")

        brief: dict[str, dict] = {}
        for code in codes:
            try:
                for page, total, rows in self.market.search(cpv=[code]):
                    for row in rows:
                        if row.get("id"):
                            brief[row["id"]] = row
                    self._progress(f"Каталог товарів ({code})", len(brief), total or len(brief))
            except Cancelled:
                raise
            except Exception as exc:
                self._log("warn", f"Каталог {code}: {exc}")

        statuses = Counter((row.get("status") or "—") for row in brief.values())
        self._log("info", "Стан карток: " + ", ".join(
            f"{name} — {n}" for name, n in statuses.most_common()))

        # Фільтр застосовуємо лише якщо чинні картки справді є: інакше назва
        # статусу могла змінитися на боці каталогу, і ми б лишились ні з чим.
        if self.p.market_active_only and statuses.get("active"):
            before = len(brief)
            brief = {pid: row for pid, row in brief.items() if row.get("status") == "active"}
            self._log("info", f"Беремо лише чинні картки: {len(brief):,} із {before:,}"
                      .replace(",", " "))

        known = self.db.known_products()
        todo = [pid for pid, row in brief.items()
                if known.get(pid) != (row.get("dateModified") or "")[:10]]
        skipped = len(brief) - len(todo)
        self._log("info", f"До читання карток: {len(todo):,}"
                          f"{f'; без змін: {skipped:,}' if skipped else ''}"
                  .replace(",", " "))

        done = 0
        saved = 0
        gone = 0                       # картки, яких каталог більше не віддає
        failed = 0                     # решта — справжні збої, їх видно поштучно
        for pid in todo:
            self.client.check_cancel()
            try:
                card = self.market.product(pid)
            except Cancelled:
                raise
            except Exception as exc:
                if _card_gone(exc):
                    gone += 1
                else:
                    failed += 1
                    if failed <= MARKET_WARN_LIMIT:
                        self._log("warn", f"Картка {pid[:8]}…: {exc}")
                    elif failed == MARKET_WARN_LIMIT + 1:
                        self._log("warn", "Далі невдалі картки не перелічую — "
                                          "їхня кількість буде в підсумку.")
                done += 1
                continue
            # Назву категорії картка тримає кодом — розкриваємо її окремо
            # (відповіді кешуються, тож на сотні товарів це кілька запитів).
            row, specs = parse_product(
                card, brief.get(pid),
                category_title=self.market.category_title(card.get("relatedCategory") or ""))
            row["fetched_at"] = datetime.now().isoformat(timespec="seconds")
            self.db.save_product(row, specs)
            saved += 1
            done += 1
            self._progress("Картки товарів", done, len(todo))
            if done % 20 == 0:
                self._tick()
        if gone:
            self._log("warn",
                      f"Каталог не віддав {_spaced(gone)} карток із {_spaced(len(todo))} "
                      f"({gone / max(len(todo), 1):.0%}): товар посилається на категорію, "
                      f"якої вже немає в довіднику — HTTP 404 «Category not found». Це не "
                      f"збій зв'язку й не наша помилка: так само не читаються картки, "
                      f"узяті з власної стрічки каталогу. Здебільшого це давні картки, "
                      f"які лишилися в пошуку зі статусом «active».")
        if failed:
            self._log("warn", f"Ще {_spaced(failed)} карток не прочиталися з інших причин.")
        self.result.products = saved + skipped
        in_base = self.db.scalar("SELECT COUNT(*) FROM products") or 0
        self._log("info", f"Картки товарів: збережено {_spaced(saved)}, "
                          f"усього в базі {_spaced(in_base)}")
        self._tick()
        return saved

    # --- крок 6: таблиця з даними -----------------------------------------

    def write_table(self) -> Path | None:
        """Складає таблицю з усіма зібраними даними в теку завантажень."""
        from .rawexport import build_sheets

        sheets = build_sheets(self.db)
        if not sheets:
            return None
        path = export_path("prozorro-дані", folder=self.s.output_dir)
        try:
            write_xlsx(path, sheets)
        except Exception as exc:
            self._log("error", f"Не вдалося записати таблицю: {exc}")
            return None
        self.result.table = str(path)
        rows = sum(len(r) for _h, r in sheets.values())
        self._log("info", f"Таблиця: {path.name} — {len(sheets)} аркушів, "
                          f"{_spaced(rows)} рядків.")
        self._tick()
        return path

    # --- запуск -----------------------------------------------------------

    def run(self) -> JobResult:
        job_id = self.db.job_start(self.p.to_dict())
        status = "ok"
        try:
            if self.s.fresh_start:
                removed = self.db.reset_collected()
                if removed:
                    total = sum(removed.values())
                    self._log("info", f"Базу очищено перед збором: прибрано "
                                      f"{total:,} рядків попередніх вибірок."
                              .replace(",", " "))
            cards = self.discover()
            # Порожній пошук ще не кінець: процедури, назв яких пошук не
            # приймає, беруться зі стрічки й після нього.
            mapping = self.resolve(list(cards.keys())) if cards else {}
            mapping.update(self.pickup_unnamed(mapping))
            if not mapping:
                self._log("warn", "За заданими фільтрами нічого не знайдено.")
                return self.result
            rows = self.load_tenders(mapping)
            if self.p.download_files and rows:
                self.download_files([row["uuid"] for row in rows])
            if self.p.collect_market:
                self.collect_products()
            self.write_table()
            self._log("info", "Готово.")
        except Cancelled:
            status = "cancelled"
            self.result.cancelled = True
            self._log("warn", "Зупинено користувачем.")
        except Exception as exc:                      # неочікуване
            status = "error"
            self.result.error = str(exc)
            self._log("error", f"Помилка: {exc}")
        finally:
            self.db.job_finish(job_id, status, self.result.as_dict())
            self.client.close()
            self._tick()
        return self.result


def _query_in(db: Database, sql: str, values: list[str], *, column: str = "uuid",
              chunk: int = 500) -> list:
    """``SELECT … WHERE <column> IN (…)`` без ризику впертися в ліміт параметрів."""
    rows: list = []
    for start in range(0, len(values), chunk):
        part = values[start:start + chunk]
        marks = ",".join("?" * len(part))
        rows += db.query(f"{sql} WHERE {column} IN ({marks})", part)
    return rows


def _card_gone(exc: Exception) -> bool:
    """Чи це «такої картки більше немає», а не тимчасовий збій.

    Каталог відповідає ``404 {"errors": ["Category not found"]}`` на товар, чия
    категорія вилучена з довідника. Перевірено на живому API 01.09.2026: так
    відповідають і картки, узяті з власної стрічки каталогу (усі вісім
    найдавніших — 404, усі вісім найновіших — 200), тож це стан їхніх даних, а
    не наша помилка. Недокументований проксі порталу, який колись віддавав ті
    самі картки, тепер відповідає 503, тож запасного джерела немає.
    """
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def _write_json(folder: Path, tender_id: str, data: dict) -> None:
    """Кладе повну картку закупівлі поруч із її файлами."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{tender_id}.json"
        with open(long_path(path), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except (OSError, ValueError, TypeError):
        pass



class _Discovery:
    """Крок пошуку: складання плану запитів і гортання видачі.

    Винесено з :meth:`Pipeline.discover` окремим об'єктом, бо крок має власний
    стан — знайдені картки, лічильник сторінок, оцінку плану, — і разом із ним
    метод розростався до двохсот рядків.

    Пошук має дві властивості, які й визначають усю будову кроку:

    * кілька кодів ДК021 в одному запиті об'єднуються через **АБО**, тож увесь
      набір іде одним запитом, а не запитом на кожен код;
    * видача **обрізана 500 сторінками (10 000 записів)**, і ця стеля спільна
      для всього запиту.

    Через стелю один запит може просто не дістати до початку періоду. Раніше це
    з'ясовувалося аж після 500 сторінок гортання — тобто після восьми хвилин
    квоти, — і тоді набір ділився навпіл, а половини гортали те саме наново. На
    річному періоді це давало години марної роботи.

    Тепер глибина з'ясовується :meth:`_probe` — двома запитами: сторінки
    адресуються номером, тож досить спитати останню доступну й подивитися її
    дату.
    """

    def __init__(self, pipe: "Pipeline"):
        self.pipe = pipe
        self.codes = pipe._cpv_codes()
        # Процедуру із задовгою назвою не можна класти в тіло запиту навіть
        # тоді, коли користувач сам позначив її у фільтрі: сервер відповідає
        # 422 і валить увесь збір. Відкладаємо її на добір зі стрічки.
        wanted = list(pipe.p.methods or [])
        self.methods = [m for m in wanted if len(m) <= PROC_TYPE_MAX_LEN]
        self.unnamed_asked = {m for m in wanted if len(m) > PROC_TYPE_MAX_LEN}
        self.base = {
            "text": pipe.p.text or "",
            "tenderer": [e for e in (pipe.p.tenderers or []) if e],
            "buyer": [e for e in (pipe.p.buyers or []) if e],
            "status": list(pipe.p.statuses or []),
        }
        self.cards: dict[str, dict] = {}
        #: Процедури, назви яких пошук не приймає (довші за 30 символів) і які
        #: через це випали — чи з розрізу за процедурою, чи прямо з фільтра
        #: користувача. Їх добирає :meth:`Pipeline.pickup_unnamed`.
        self.unnamed: set[str] = set(self.unnamed_asked)
        #: Скільки сторінок пройдено і скільки їх очікується; верхню оцінку
        #: дає проба, бо разом із нею приходить `total`.
        self.pages = 0
        self.planned = 0
        #: Оцінка сторінок за вмістом плану — не за ``id()`` об'єкта: відкинуті
        #: плани збирає складальник сміття, і той самий ``id()`` міг би
        #: дістатися новому кортежу.
        self.page_hint: dict[tuple, int] = {}
        #: Скільки записів має план за пробою (сервер обрізає на стелі).
        self.totals: dict[tuple, int] = {}
        #: Найдавніша дата, до якої план дістає, якщо стелі не вистачило.
        self.horizons: dict[tuple, str] = {}
        #: Плани, яким стелі не вистачило на весь період.
        self.short: list = []
        #: Чи попереду ще друга хвиля з розрізом за замовником.
        self.buyers_ahead = True

    # --- службове ---------------------------------------------------------

    def _query_of(self, plan) -> dict:
        """Аргументи запиту для плану.

        Тіло з них складає :meth:`SearchApi.build_body` — і проба, і гортання
        мусять іти через нього: він відкидає порожні поля, а сервер порожній
        ``status`` чи ``text`` відхиляє з HTTP 422.
        """
        batch, procs, buyers = plan
        q = dict(self.base)
        if batch:
            q["cpv"] = list(batch)
        if procs:
            q["proc_type"] = list(procs)
        if buyers:
            # Перелік замовників плану вужчий за фільтр користувача (він із
            # нього ж і зроблений), тож заміщає його, а не додається.
            q["buyer"] = list(buyers)
        return q

    @staticmethod
    def _describe(plan) -> str:
        batch, procs, buyers = plan
        what = f"{len(batch)} код(ів)" if batch else "без кодів"
        if len(procs) == 1:
            what += f", {procs[0]}"
        if buyers:
            what += f", {len(buyers)} замовник(ів)"
        return what

    @staticmethod
    def _key(plan) -> tuple:
        batch, procs, buyers = plan
        return (tuple(batch), tuple(procs), tuple(buyers))

    @staticmethod
    def _buyer_of(row: dict) -> str:
        """ЄДРПОУ замовника з картки пошуку — основа розрізу за замовником."""
        entity = row.get("procuringEntity") or {}
        return str((entity.get("identifier") or {}).get("id") or "").strip()

    def _ask(self, plan, page: int) -> dict:
        body = SearchApi.build_body(page=page, **self._query_of(plan))
        data = self.pipe.search.query("tenders", body)
        with self.pipe._lock:
            self.pages += 1
        return data

    def _note(self, stage: str = "Пошук закупівель") -> None:
        pipe = self.pipe
        with pipe._lock:
            pipe.result.found = len(self.cards)
            done, planned = self.pages, self.planned
        pipe._progress(stage, done, max(planned, done + 1))
        pipe._tick()

    # --- фаза 1: план -----------------------------------------------------

    def _probe(self, plan) -> tuple[bool, int, str]:
        """``(чи покриє план період, скільки сторінок, найдавніша досяжна дата)``.

        Перший запит дає ``total``, другий — дату на останній доступній
        сторінці. Якщо записів менше за стелю, другий запит не потрібен, і
        дата ні до чого: план і так дістає до кінця.
        """
        first = self._ask(plan, 1)
        total = int(first.get("total") or 0)
        pages = max(1, min(SEARCH_MAX_PAGE, -(-total // SEARCH_PAGE_SIZE)))
        self.page_hint[self._key(plan)] = pages
        self.totals[self._key(plan)] = total
        if total < SEARCH_MAX_RESULTS:
            return True, pages, ""
        last = self._ask(plan, pages)
        deepest = min((d for d in (self.pipe._published(r) for r in last.get("data") or []) if d),
                      default="")
        self.horizons[self._key(plan)] = deepest
        return (not deepest or deepest <= self.pipe._date_from), pages, deepest

    def _refine(self, plan) -> list | None:
        """Уточнює план, який не дістає до початку періоду.

        Спершу ділимо коди — це найдешевше й завжди коректно. Коли код лишився
        один, беремо інший розріз: тип процедури. Він розбиває видачу без
        перетинів (перевірено) і дає глибину на роки замість місяців, бо стеля
        рахується для кожного запиту окремо.
        """
        batch, procs, buyers = plan
        if len(batch) > 1:
            half = len(batch) // 2
            return [(batch[:half], procs, buyers), (batch[half:], procs, buyers)]
        parts = self._by_procedure(plan)
        if parts:
            return parts
        if len(buyers) > 1:
            # Третій розріз — за замовником. Кожна закупівля має рівно одного,
            # а список ЄДРПОУ сервер об'єднує через АБО (перевірено: 1 + 1 = 2)
            # і приймає їх щонайменше 20 000 в одному запиті.
            half = len(buyers) // 2
            return [(batch, procs, buyers[:half]), (batch, procs, buyers[half:])]
        return None

    def _by_procedure(self, plan) -> list | None:
        """Розріз за процедурою — лише поки її ще не задано."""
        batch, procs, buyers = plan
        if procs:
            return None
        pool = self.methods or list(METHOD_LABELS)
        usable = [m for m in pool if len(m) <= PROC_TYPE_MAX_LEN]
        # Довгі назви пошук не приймає взагалі, тож розріз за процедурою їх
        # губить. Тут лише запам'ятовуємо котрі — добирає їх зі стрічки змін
        # :meth:`Pipeline.pickup_unnamed`, а сказати про це досить один раз на
        # весь пошук, а не на кожен поділений запит.
        self.unnamed.update(m for m in pool if len(m) > PROC_TYPE_MAX_LEN)
        return [(batch, [m], buyers) for m in usable] if len(usable) > 1 else None

    def _plan_out(self, plan) -> list | None:
        """Проба → або план готовий до гортання, або його треба уточнити."""
        covers, _pages, horizon = self._probe(plan)
        if covers:
            return None
        parts = self._refine(plan)
        if parts:
            return parts
        with self.pipe._lock:
            self.short.append(plan)
        if not plan[2] and self.buyers_ahead:
            # Розріз за замовником ще попереду: перелік ЄДРПОУ збереться з
            # самої видачі, тож зараз лякати користувача нема чим — скаже
            # :meth:`_by_buyer`, коли стане видно, що з цього вийшло.
            return None
        self._warn_ceiling(plan, horizon)
        return None

    def _warn_ceiling(self, plan, horizon: str = "") -> None:
        """Каже, докуди запит дістає, коли ділити його вже нема чим.

        У пошуку немає ані фільтра за датою, ані за регіоном чи сумою, розмір
        сторінки й порядок видачі незмінні, а сторінки за 500-ту не існує
        (перевірено на живому API). Тому вікно в минуле не зсувається, і чесна
        відповідь — назвати дату, до якої запит дістав.
        """
        horizon = horizon or self.horizons.get(self._key(plan), "")
        reach = (f" Він дістає лише до {horizon}, а період починається "
                 f"{self.pipe._date_from} — усе, що давніше, не потрапить."
                 if horizon else "")
        self.pipe._log("warn", f"Запит ({self._describe(plan)}) упирається у стелю пошуку "
                               f"— {_spaced(SEARCH_MAX_RESULTS)} записів.{reach} "
                               f"Портал віддає видачу тільки від найновіших і не має "
                               f"фільтра за датою, тож зсунути вікно в минуле не вийде: "
                               f"зарадить лише пізніша дата «з» або вужчий перелік кодів.")

    def _build_plan(self, seeds=None) -> list:
        """Складає перелік запитів, готових до гортання.

        Спершу з'ясовуємо глибину всіх запитів і лише потім гортаємо: так до
        початку гортання відома загальна кількість сторінок, а отже смуга
        показує чесний відсоток. Планування коштує один-два запити на план — на
        річному періоді це близько тридцяти запитів за кілька секунд.

        ``seeds`` — з чого починати; друга хвиля подає сюди ті самі запити,
        але вже з переліком замовників.
        """
        pipe = self.pipe
        plans = deque(seeds if seeds is not None else [(list(self.codes), self.methods, ())])
        ready: list = []
        with ThreadPoolExecutor(pipe.s.search_concurrency) as pool:
            while plans:
                wave = [plans.popleft()
                        for _ in range(min(len(plans), pipe.s.search_concurrency))]
                futures = {pool.submit(self._plan_out, plan): plan for plan in wave}
                for future in as_completed(futures):
                    plan = futures[future]
                    parts = future.result()
                    if parts:
                        pipe._log("info", f"Запит ({self._describe(plan)}) не дістає до "
                                          f"початку періоду — ділю на {len(parts)}.")
                        plans.extend(parts)
                    else:
                        ready.append(plan)
                    self._note("Планую пошук")
        with pipe._lock:
            self.planned = self.pages + sum(self.page_hint.get(self._key(p), 1)
                                            for p in ready)
        pipe._log("info", f"План пошуку: {len(ready)} запит(ів), "
                          f"до {_spaced(self.planned - self.pages)} сторінок.")
        return ready

    # --- фаза 2: гортання -------------------------------------------------

    def _keep(self, rows) -> tuple[list, list]:
        """Ділить сторінку на «беремо» та перелік дат для рішення про зупинку."""
        keep, stream = [], []
        for row in rows:
            published = self.pipe._published(row)
            if published:
                stream.append(published)
            tid = row.get("tenderID")
            if not tid:
                continue
            if published and not (self.pipe._date_from <= published <= self.pipe._date_to):
                continue
            keep.append((tid, row))
        return keep, stream

    def _walk(self, plan) -> None:
        """Гортає план до початку періоду, складаючи знайдене у спільний набір."""
        pipe = self.pipe
        stale = 0
        try:
            for _page, _total, rows in pipe.search.pages("tenders", **self._query_of(plan)):
                with pipe._lock:
                    self.pages += 1
                if not rows:
                    break
                keep, stream = self._keep(rows)
                with pipe._lock:
                    self.cards.update(keep)
                self._note()
                newest = max(stream, default="")
                # Порожній `newest` означає, що дати визначити не вдалося:
                # гортати далі наосліп безглуздо, тож рахуємо це застарілим.
                if not newest or newest < pipe._stop_before:
                    stale += 1
                    if stale >= STALE_PAGE_TOLERANCE:
                        break
                else:
                    stale = 0
        except Cancelled:
            raise
        except Exception as exc:
            pipe._log("error", f"Помилка пошуку ({self._describe(plan)}): {exc}")

    # --- усе разом --------------------------------------------------------

    def _crawl(self, ready: list) -> None:
        with ThreadPoolExecutor(self.pipe.s.search_concurrency) as pool:
            for _ in pool.map(self._walk, ready):
                self.pipe._tick()

    def _by_buyer(self) -> None:
        """Друга хвиля: переповнені запити перегортаються за замовниками.

        Стеля пошуку — не на тому, скільки записів брати, а на тому, які
        взагалі мають адресу: сторінки за 500-ту не існує
        (`The page may not be greater than 500`). Тому дістатися глибших
        записів можна лише запитом, у якому вони опиняться в перших 10 000, —
        тобто вужчим фільтром. Замовник для цього годиться: у кожної закупівлі
        він рівно один, а перелік ЄДРПОУ пошук об'єднує через АБО.

        Перелік береться з карток, які й так перегорнуто: пошук віддає
        ``procuringEntity`` у кожному записі, тож це не коштує жодного запиту.
        Звідси й межа способу — замовника, якого не було видно в межах стелі,
        у розрізі не буде. Про це кажемо прямо.
        """
        pipe = self.pipe
        if not self.short:
            return
        self.buyers_ahead = False
        short, self.short = list(self.short), []
        buyers = sorted({b for b in map(self._buyer_of, self.cards.values()) if b})
        limit = set(self.base.get("buyer") or ())
        if limit:                      # користувач і сам звузив коло замовників
            buyers = [b for b in buyers if b in limit]
        if len(buyers) < 2:
            pipe._log("warn", "Розрізати переповнені запити за замовником не вийшло: "
                              "у видачі не видно достатньо різних ЄДРПОУ замовників.")
            for plan in short:
                self._warn_ceiling(plan)
            return
        pipe._log("info",
                  f"{len(short)} запит(ів) уперлися у стелю. Перегортаю їх, розрізавши "
                  f"за {_spaced(len(buyers))} замовниками, яких видно у вибірці — "
                  f"кожна група дістає власні {_spaced(SEARCH_MAX_RESULTS)} записів.")
        before = len(self.cards)
        ready = self._build_plan([(codes, procs, buyers) for codes, procs, _b in short])
        self._crawl(ready)
        gained = len(self.cards) - before
        real = sum(self.totals.get(self._key(p), 0) for p in ready)
        pipe._log("info",
                  f"Розріз за замовником додав {_spaced(gained)} закупівель. У цих "
                  f"запитах щонайменше {_spaced(real)} записів — стеля показувала "
                  f"{_spaced(SEARCH_MAX_RESULTS)}. Замовники, яких не було видно в межах "
                  f"стелі, у розріз не потрапили, тож повноти це не гарантує.")

    def run(self) -> dict[str, dict]:
        pipe = self.pipe
        if self.unnamed_asked and not self.methods:
            # У фільтрі лишилися самі лише процедури, яких пошук не знає:
            # гортати нема чого — все одно довелося б викинути кожен запис.
            pipe.unnamed_methods = set(self.unnamed)
            pipe._log("info", "У фільтрі лише процедури, назв яких пошук не приймає, "
                              "тож шукати нема чого — беру їх тільки зі стрічки змін.")
            return {}
        pipe._log("info", f"Пошук: {len(self.codes) or '—'} код(ів) ДК021, період "
                          f"{pipe._date_from} … {pipe._date_to}.")
        self._crawl(self._build_plan())
        self._by_buyer()
        pipe.result.found = len(self.cards)
        pipe.unnamed_methods = set(self.unnamed)
        pipe._progress("Пошук закупівель", self.pages, self.pages)
        pipe._log("info", f"Знайдено закупівель за фільтром: {_spaced(len(self.cards))} "
                          f"(сторінок пройдено: {_spaced(self.pages)})")
        pipe._tick()
        return self.cards


def _spaced(number) -> str:
    """``12345`` → ``12 345``.

    Раніше тисячі відокремлювали через ``f"…{n:,}…".replace(",", " ")``, але
    заміна зачіпала весь рядок — і звичайні коми в тексті теж ставали
    пробілами («знайдено 20 закупівель, з них…» → «…закупівель з них…»).
    Тут заміна стосується лише числа.
    """
    return f"{number:,}".replace(",", " ")


def _as_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return date.today()
