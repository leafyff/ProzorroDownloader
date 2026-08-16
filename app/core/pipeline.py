"""Конвеєр: пошук → розпізнавання → картки закупівель → файли."""
from __future__ import annotations

import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, SearchPreset
from ..paths import long_path
from .api import SEARCH_MAX_RESULTS, SEARCH_PAGE_SIZE, CdbApi, SearchApi
from .classifiers import expand_prefixes
from .db import Database
from .downloader import (
    FileDownloader, document_extension, is_signature, normalize_extensions, tender_folder,
)
from .extract import latest_versions, parse_tender
from .http import Cancelled, HttpClient
from .market import MarketApi, class_codes, parse_product
from .resolver import IndexBuilder, Resolver, tender_id_of

LogCb = Callable[[str, str], None]
ProgressCb = Callable[[str, int, int], None]
StatsCb = Callable[[dict], None]

#: Скільки поспіль «застарих» сторінок терпимо, перш ніж зупинити гортання.
STALE_PAGE_TOLERANCE = 3


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
    def _card_date(card: dict) -> str:
        """Дата оприлюднення закупівлі, витягнута з картки пошуку."""
        for key in ("tenderPeriod", "enquiryPeriod", "auctionPeriod"):
            node = card.get(key) or {}
            if node.get("startDate"):
                return str(node["startDate"])[:10]
        tid = card.get("tenderID") or ""
        return tid[3:13] if len(tid) > 13 else ""

    # --- крок 1: пошук ----------------------------------------------------

    def discover(self) -> dict[str, dict]:
        """Гортає пошук порталу і повертає ``{tenderID: картка}`` у межах періоду."""
        codes = self._cpv_codes()
        queries: list[dict] = []
        base = {
            "text": self.p.text or "",
            "tenderer": [e for e in (self.p.tenderers or []) if e],
            "buyer": [e for e in (self.p.buyers or []) if e],
            "status": list(self.p.statuses or []),
            "proc_type": list(self.p.methods or []),
        }
        if codes:
            # Пошуковий API шукає точний код, тому кожен код — окремий запит.
            queries = [{**base, "cpv": [code]} for code in codes]
        else:
            queries = [base]

        self._log("info", f"Пошук: {len(queries)} запит(ів) до порталу, період "
                          f"{self._date_from} … {self._date_to}.")
        cards: dict[str, dict] = {}
        done = 0

        def run_query(query: dict) -> dict[str, dict]:
            local: dict[str, dict] = {}
            stale = 0
            try:
                for page, total, rows in self.search.pages("tenders", **query):
                    if not rows:
                        break
                    if page == 1 and total >= SEARCH_MAX_RESULTS:
                        self._log("warn", f"За ДК021 {query.get('cpv')} пошук віддає щонайбільше "
                                          f"10 000 записів. Якщо період довгий, частина закупівель "
                                          f"може не потрапити — звузьте період або код.")
                    dates = [self._card_date(row) for row in rows]
                    newest = max((d for d in dates if d), default="")
                    for row, published in zip(rows, dates):
                        tid = row.get("tenderID")
                        if not tid:
                            continue
                        if published and not (self._date_from <= published <= self._date_to):
                            continue
                        local[tid] = row
                    # Порожній `newest` означає, що дати визначити не вдалося:
                    # гортати далі наосліп безглуздо, тож рахуємо це застарілим.
                    if not newest or newest < self._date_from:
                        stale += 1
                        if stale >= STALE_PAGE_TOLERANCE:
                            break
                    else:
                        stale = 0
            except Cancelled:
                raise
            except Exception as exc:
                self._log("error", f"Помилка пошуку {query.get('cpv') or ''}: {exc}")
            return local

        with ThreadPoolExecutor(self.s.search_concurrency) as pool:
            futures = [pool.submit(run_query, q) for q in queries]
            for future in as_completed(futures):
                done += 1
                cards.update(future.result())
                self._progress("Пошук закупівель", done, len(queries))
                if done % 5 == 0:
                    self._tick()
        self.result.found = len(cards)
        self._log("info", f"Знайдено закупівель за фільтром: {len(cards):,}".replace(",", " "))
        self._tick()
        return cards

    # --- крок 2: розпізнавання UUID --------------------------------------

    def resolve(self, tender_ids: list[str]) -> dict[str, str]:
        mapping = self.resolver.from_index(tender_ids)
        if mapping:
            self._log("info", f"З локального індексу розпізнано {len(mapping):,}."
                      .replace(",", " "))
        missing = [t for t in tender_ids if t not in mapping]

        if missing and self.s.resolve_mode in ("auto", "contracts"):
            codes = self._cpv_codes()
            # Гуртовий збір коштує один запит на двадцять закупівель, точковий —
            # один на кожну, а портал обмежує темп до одиниць запитів на секунду.
            # Тому щойно закупівель більше, ніж записів на сторінці, йдемо гуртом.
            harvested = bool(codes) and len(missing) > SEARCH_PAGE_SIZE
            if harvested:
                mapping.update(self._harvest_contracts(set(missing), codes))
                missing = [t for t in tender_ids if t not in mapping]
            elif missing:
                # Точково має сенс лише тоді, коли гуртом було нічим: без коду
                # ДК021 (наприклад, пошук суто за ЄДРПОУ конкурента). Після
                # гуртового проходу той самий індекс договорів уже переглянуто.
                mapping.update(self.resolver.from_contracts_search(missing, self._progress))
                missing = [t for t in tender_ids if t not in mapping]

        if missing and self.s.resolve_mode in ("auto", "index"):
            if self.s.resolve_mode == "index" or len(missing) > 0.4 * max(1, len(tender_ids)):
                self._log("info", f"Лишилося нерозпізнаних: {len(missing):,}. "
                                  f"Будую індекс стрічки змін ЦБД.".replace(",", " "))
                builder = IndexBuilder(
                    self.client, self.db, shards=self.s.index_concurrency,
                    on_log=self._log, keep_all=self.s.keep_full_index, wanted=set(missing),
                )
                builder.build(_as_date(self._date_from), date.today(), self._progress)
                mapping.update(self.resolver.from_index(missing))
                missing = [t for t in tender_ids if t not in mapping]

        self.result.resolved = len(mapping)
        self.result.unresolved = missing
        if missing:
            self._log("warn",
                      f"Розпізнано {len(mapping)} із {len(tender_ids)} закупівель. "
                      f"Решта {len(missing)} — це ті, за якими ще немає договору: "
                      f"звіти без вкладень, скасовані та ті, що тривають. "
                      f"Щоб охопити і їх, побудуйте локальний індекс у налаштуваннях — "
                      f"він працює через Центральну базу, де немає обмеження темпу.")
        self._tick()
        return mapping

    def _harvest_contracts(self, wanted: set[str], codes: list[str]) -> dict[str, str]:
        """Гуртом збирає UUID договорів через пошук — 1 запит на 20 закупівель."""
        self._log("info", "Збираю UUID через пошук договорів…")
        contract_uuids: dict[str, str] = {}
        done = 0

        def run(code: str) -> dict[str, str]:
            local: dict[str, str] = {}
            stale = 0
            try:
                for _page, _total, rows in self.search.pages(
                        "contracts", cpv=[code], text=self.p.text or ""):
                    if not rows:
                        break
                    newest = ""
                    for row in rows:
                        contract_id = row.get("contractID") or ""
                        # Дата підписання буває порожня — тоді орієнтуємось
                        # на дату в номері закупівлі (UA-РРРР-ММ-ДД-…).
                        signed = str(row.get("dateSigned") or "")[:10] or contract_id[3:13]
                        newest = max(newest, signed)
                        tid = tender_id_of(contract_id)
                        if tid in wanted and row.get("id"):
                            local[tid] = row["id"]
                    if not newest or newest < self._date_from:
                        stale += 1
                        if stale >= STALE_PAGE_TOLERANCE:
                            break
                    else:
                        stale = 0
            except Cancelled:
                raise
            except Exception as exc:
                self._log("warn", f"Пошук договорів {code}: {exc}")
            return local

        with ThreadPoolExecutor(self.s.search_concurrency) as pool:
            futures = [pool.submit(run, code) for code in codes]
            for future in as_completed(futures):
                done += 1
                contract_uuids.update(future.result())
                self._progress("Пошук договорів", done, len(codes))
        if not contract_uuids:
            return {}
        return self.resolver.from_contract_uuids(contract_uuids.values(), self._progress)

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
            parsed = parse_tender(data)
            if not self._passes_filters(parsed):
                return None
            self.db.save_tender(
                parsed["row"], lots=parsed["lots"], items=parsed["items"],
                bids=parsed["bids"], awards=parsed["awards"],
                contracts=parsed["contracts"], docs=parsed["docs"],
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
                self._progress("Завантаження карток закупівель", done, total)
                if done % 10 == 0:
                    self._tick()
        self.result.tenders_loaded = len(rows)
        self._log("info", f"Завантажено карток: {len(rows):,}, документів у них: "
                          f"{self.result.documents:,}".replace(",", " "))
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
        for pid in todo:
            self.client.check_cancel()
            try:
                card = self.market.product(pid)
            except Cancelled:
                raise
            except Exception as exc:
                self._log("warn", f"Картка {pid[:8]}…: {exc}")
                done += 1
                continue
            row, specs = parse_product(card, brief.get(pid))
            row["fetched_at"] = datetime.now().isoformat(timespec="seconds")
            self.db.save_product(row, specs)
            saved += 1
            done += 1
            self._progress("Картки товарів", done, len(todo))
            if done % 20 == 0:
                self._tick()
        self.result.products = saved + skipped
        self._log("info", f"Картки товарів: збережено {saved:,}, усього в базі "
                          f"{self.db.scalar('SELECT COUNT(*) FROM products') or 0:,}"
                  .replace(",", " "))
        self._tick()
        return saved

    # --- запуск -----------------------------------------------------------

    def run(self) -> JobResult:
        job_id = self.db.job_start(self.p.to_dict())
        status = "ok"
        try:
            cards = self.discover()
            if not cards:
                self._log("warn", "За заданими фільтрами нічого не знайдено.")
                return self.result
            mapping = self.resolve(list(cards.keys()))
            if not mapping:
                return self.result
            rows = self.load_tenders(mapping)
            if self.p.download_files and rows:
                self.download_files([row["uuid"] for row in rows])
            if self.p.collect_market:
                self.collect_products()
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


def _write_json(folder: Path, tender_id: str, data: dict) -> None:
    """Кладе повну картку закупівлі поруч із її файлами."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{tender_id}.json"
        with open(long_path(path), "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except (OSError, ValueError, TypeError):
        pass


def _as_date(value: str) -> date:
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return date.today()
