"""Локальна база SQLite: індекс закупівель, картки, файли, історія завдань."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from ..paths import DB_FILE

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- Відповідність tenderID (UA-2026-...) → внутрішній UUID ЦБД.
CREATE TABLE IF NOT EXISTS tender_index (
    tender_id     TEXT PRIMARY KEY,
    uuid          TEXT NOT NULL,
    date_created  TEXT,
    date_modified TEXT,
    status        TEXT,
    method        TEXT,
    pe_name       TEXT,
    pe_edrpou     TEXT,
    pe_region     TEXT
);
-- Пошук завжди йде за tender_id (первинний ключ), тож додаткові індекси тут
-- лише роздували б файл: на мільйонах рядків кожен коштує сотні мегабайт.
DROP INDEX IF EXISTS ix_index_uuid;
DROP INDEX IF EXISTS ix_index_created;

-- Які добові відрізки стрічки змін уже пройдені (щоб не гортати двічі).
CREATE TABLE IF NOT EXISTS index_coverage (
    day      TEXT PRIMARY KEY,   -- YYYY-MM-DD
    records  INTEGER DEFAULT 0,
    done_at  TEXT
);

-- Повні картки закупівель.
CREATE TABLE IF NOT EXISTS tenders (
    uuid           TEXT PRIMARY KEY,
    tender_id      TEXT,
    title          TEXT,
    description    TEXT,
    status         TEXT,
    method_type    TEXT,
    main_category  TEXT,
    date_created   TEXT,
    date_modified  TEXT,
    tender_start   TEXT,
    tender_end     TEXT,
    value_amount   REAL,
    value_currency TEXT,
    vat_included   INTEGER,
    pe_name        TEXT,
    pe_edrpou      TEXT,
    pe_region      TEXT,
    pe_locality    TEXT,
    n_lots         INTEGER DEFAULT 0,
    n_bids         INTEGER DEFAULT 0,
    n_docs         INTEGER DEFAULT 0,
    cpv_list       TEXT,          -- через кому, для швидкого фільтра
    fetched_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_tenders_tid ON tenders(tender_id);
CREATE INDEX IF NOT EXISTS ix_tenders_created ON tenders(date_created);
CREATE INDEX IF NOT EXISTS ix_tenders_pe ON tenders(pe_edrpou);

CREATE TABLE IF NOT EXISTS lots (
    id           TEXT PRIMARY KEY,
    tender_uuid  TEXT NOT NULL,
    title        TEXT,
    description  TEXT,
    status       TEXT,
    value_amount REAL,
    currency     TEXT
);
CREATE INDEX IF NOT EXISTS ix_lots_tender ON lots(tender_uuid);

CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    tender_uuid  TEXT NOT NULL,
    lot_id       TEXT,
    description  TEXT,
    cpv          TEXT,
    cpv_name     TEXT,
    quantity     REAL,
    unit         TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_tender ON items(tender_uuid);
CREATE INDEX IF NOT EXISTS ix_items_cpv ON items(cpv);

CREATE TABLE IF NOT EXISTS bids (
    id            TEXT PRIMARY KEY,
    tender_uuid   TEXT NOT NULL,
    lot_id        TEXT,
    status        TEXT,
    date          TEXT,
    value_amount  REAL,
    currency      TEXT,
    bidder_name   TEXT,
    bidder_edrpou TEXT,
    bidder_region TEXT
);
CREATE INDEX IF NOT EXISTS ix_bids_tender ON bids(tender_uuid);
CREATE INDEX IF NOT EXISTS ix_bids_edrpou ON bids(bidder_edrpou);

CREATE TABLE IF NOT EXISTS awards (
    id              TEXT PRIMARY KEY,
    tender_uuid     TEXT NOT NULL,
    lot_id          TEXT,
    status          TEXT,
    date            TEXT,
    value_amount    REAL,
    currency        TEXT,
    supplier_name   TEXT,
    supplier_edrpou TEXT
);
CREATE INDEX IF NOT EXISTS ix_awards_tender ON awards(tender_uuid);
CREATE INDEX IF NOT EXISTS ix_awards_edrpou ON awards(supplier_edrpou);

CREATE TABLE IF NOT EXISTS contracts (
    id              TEXT PRIMARY KEY,
    tender_uuid     TEXT NOT NULL,
    contract_id     TEXT,
    status          TEXT,
    date_signed     TEXT,
    value_amount    REAL,
    currency        TEXT,
    supplier_name   TEXT,
    supplier_edrpou TEXT
);
CREATE INDEX IF NOT EXISTS ix_contracts_tender ON contracts(tender_uuid);
CREATE INDEX IF NOT EXISTS ix_contracts_edrpou ON contracts(supplier_edrpou);

-- Усі файли, знайдені в картці закупівлі.
CREATE TABLE IF NOT EXISTS documents (
    key            TEXT PRIMARY KEY,   -- tender_uuid|doc_id|datePublished
    doc_id         TEXT,
    tender_uuid    TEXT NOT NULL,
    tender_id      TEXT,
    scope          TEXT,               -- tender / bid / award / contract / other
    container      TEXT,               -- шлях у JSON, напр. bids[2].documents
    owner_name     TEXT,               -- чий файл (учасник/переможець), якщо відомо
    owner_edrpou   TEXT,
    doc_type       TEXT,
    title          TEXT,
    format         TEXT,
    date_published TEXT,
    url            TEXT,
    hash           TEXT,
    size           INTEGER,
    local_path     TEXT,
    -- pending (у черзі) / ok / skipped (завеликий) / filtered (відсіяний
    -- налаштуваннями — при зміні фільтра розглядається знову) / error
    state          TEXT DEFAULT 'pending',
    error          TEXT
);
CREATE INDEX IF NOT EXISTS ix_docs_tender ON documents(tender_uuid);
CREATE INDEX IF NOT EXISTS ix_docs_state ON documents(state);
CREATE INDEX IF NOT EXISTS ix_docs_hash ON documents(hash);

-- Історія запусків.
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    params      TEXT,
    stats       TEXT
);
"""


class Database:
    """Потокобезпечна обгортка над SQLite (одне з'єднання + блокування)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DB_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
                self.conn.close()
            except sqlite3.Error:
                pass

    # --- базові операції --------------------------------------------------

    def execute(self, sql: str, params: Sequence = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params: Sequence = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Sequence = ()) -> Any:
        rows = self.query(sql, params)
        return rows[0][0] if rows else None

    # --- індекс tenderID → uuid ------------------------------------------

    def index_put(self, rows: Iterable[Sequence]) -> None:
        # Відповідність tenderID → uuid незмінна, тож повторні записи можна ігнорувати:
        # це помітно швидше за ON CONFLICT DO UPDATE на мільйонах рядків.
        self.executemany(
            "INSERT OR IGNORE INTO tender_index(tender_id, uuid, date_created, date_modified,"
            " status, method, pe_name, pe_edrpou, pe_region) VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )

    def index_lookup(self, tender_ids: Sequence[str]) -> dict[str, str]:
        """``{tenderID: uuid}`` для наявних у індексі."""
        out: dict[str, str] = {}
        ids = list(tender_ids)
        for start in range(0, len(ids), 800):
            chunk = ids[start:start + 800]
            marks = ",".join("?" * len(chunk))
            for row in self.query(
                f"SELECT tender_id, uuid FROM tender_index WHERE tender_id IN ({marks})", chunk
            ):
                out[row["tender_id"]] = row["uuid"]
        return out

    def index_size(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM tender_index") or 0)

    def coverage_days(self) -> set[str]:
        return {r["day"] for r in self.query("SELECT day FROM index_coverage WHERE done_at IS NOT NULL")}

    def coverage_mark(self, day: str, records: int, done: bool) -> None:
        self.execute(
            "INSERT INTO index_coverage(day, records, done_at) VALUES(?,?,?)"
            " ON CONFLICT(day) DO UPDATE SET records=index_coverage.records+excluded.records,"
            " done_at=COALESCE(excluded.done_at, index_coverage.done_at)",
            (day, records, _now() if done else None),
        )

    # --- закупівлі --------------------------------------------------------

    def tender_exists(self, uuid: str) -> bool:
        return self.scalar("SELECT 1 FROM tenders WHERE uuid=?", (uuid,)) is not None

    def save_tender(self, row: dict, *, lots: list, items: list, bids: list,
                    awards: list, contracts: list, docs: list) -> None:
        cols = list(row.keys())
        marks = ",".join("?" * len(cols))
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "uuid")
        with self._lock:
            self.conn.execute(
                f"INSERT INTO tenders({','.join(cols)}) VALUES({marks})"
                f" ON CONFLICT(uuid) DO UPDATE SET {updates}",
                [row[c] for c in cols],
            )
            uuid = row["uuid"]
            for table in ("lots", "items", "bids", "awards", "contracts"):
                self.conn.execute(f"DELETE FROM {table} WHERE tender_uuid=?", (uuid,))
            self.conn.executemany(
                "INSERT OR REPLACE INTO lots(id,tender_uuid,title,description,status,value_amount,currency)"
                " VALUES(?,?,?,?,?,?,?)", lots)
            self.conn.executemany(
                "INSERT OR REPLACE INTO items(id,tender_uuid,lot_id,description,cpv,cpv_name,quantity,unit)"
                " VALUES(?,?,?,?,?,?,?,?)", items)
            self.conn.executemany(
                "INSERT OR REPLACE INTO bids(id,tender_uuid,lot_id,status,date,value_amount,currency,"
                "bidder_name,bidder_edrpou,bidder_region) VALUES(?,?,?,?,?,?,?,?,?,?)", bids)
            self.conn.executemany(
                "INSERT OR REPLACE INTO awards(id,tender_uuid,lot_id,status,date,value_amount,currency,"
                "supplier_name,supplier_edrpou) VALUES(?,?,?,?,?,?,?,?,?)", awards)
            self.conn.executemany(
                "INSERT OR REPLACE INTO contracts(id,tender_uuid,contract_id,status,date_signed,"
                "value_amount,currency,supplier_name,supplier_edrpou) VALUES(?,?,?,?,?,?,?,?,?)", contracts)
            self.conn.executemany(
                "INSERT INTO documents(key,doc_id,tender_uuid,tender_id,scope,container,owner_name,"
                "owner_edrpou,doc_type,title,format,date_published,url,hash,size,local_path,state,error)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET url=excluded.url", docs)
            self.conn.commit()

    def mark_document(self, key: str, *, state: str, local_path: str = "",
                      size: int = 0, error: str = "") -> None:
        self.execute(
            "UPDATE documents SET state=?, local_path=?, size=?, error=? WHERE key=?",
            (state, local_path, size, error, key),
        )

    def mark_filtered(self, keys: Sequence[str], reason: str) -> None:
        """Позначає документи як відсіяні фільтром (без завантаження)."""
        self.executemany(
            "UPDATE documents SET state='filtered', error=? WHERE key=?",
            [(reason, key) for key in keys],
        )

    def pending_documents(self, tender_uuids: Sequence[str] | None = None,
                          scopes: Sequence[str] | None = None) -> list[sqlite3.Row]:
        # `filtered` теж потрапляє в чергу: якщо користувач змінив фільтр
        # типів файлів, раніше відсіяні документи мають завантажитися.
        sql = "SELECT * FROM documents WHERE state IN ('pending','error','filtered')"
        params: list[Any] = []
        if scopes:
            sql += f" AND scope IN ({','.join('?' * len(scopes))})"
            params += list(scopes)
        if tender_uuids:
            uuids = list(tender_uuids)
            rows: list[sqlite3.Row] = []
            for start in range(0, len(uuids), 500):
                chunk = uuids[start:start + 500]
                rows += self.query(
                    sql + f" AND tender_uuid IN ({','.join('?' * len(chunk))})", params + chunk)
            return rows
        return self.query(sql, params)

    # --- завдання ---------------------------------------------------------

    def job_start(self, params: dict) -> int:
        cur = self.execute(
            "INSERT INTO jobs(created_at, status, params) VALUES(?,?,?)",
            (_now(), "running", json.dumps(params, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def job_finish(self, job_id: int, status: str, stats: dict) -> None:
        self.execute(
            "UPDATE jobs SET finished_at=?, status=?, stats=? WHERE id=?",
            (_now(), status, json.dumps(stats, ensure_ascii=False), job_id),
        )


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
