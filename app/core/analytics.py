"""Аналітичні зрізи по завантажених закупівлях.

Усе рахується з локальної бази — файли для цього не потрібні: замовник,
постачальник, суми, коди ДК021 та учасники лежать у самій картці закупівлі.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Sequence

from . import rejections as rj
from .classifiers import cpv_name, significant_prefix
from .db import Database

Sheet = tuple[Sequence[str], list[list[Any]]]

#: Договори в цих станах вважаємо чинними грошима.
LIVE_CONTRACTS = ("active", "terminated")


def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _top_cpv(pairs: dict[str, int], limit: int = 3) -> str:
    """«30213300-8 Настільні комп'ютери (12); …» — найчастіші коди групи."""
    best = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return "; ".join(f"{code} {cpv_name(code)} ({n})".strip() for code, n in best)


def _primary_cpv(db: Database) -> dict[str, str]:
    """Основний код ДК021 для кожної закупівлі — найчастіший серед позицій.

    Закупівля може містити позиції з різними кодами; щоб гроші не подвоювались
    у розрізі галузей, кожну зараховуємо лише до одного, головного коду.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in db.query("SELECT tender_uuid, cpv FROM items WHERE cpv <> ''"):
        counts[row["tender_uuid"]][row["cpv"]] += 1
    primary: dict[str, str] = {}
    for uuid, codes in counts.items():
        primary[uuid] = sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    for row in db.query("SELECT uuid, cpv_list FROM tenders WHERE cpv_list <> ''"):
        primary.setdefault(row["uuid"], row["cpv_list"].split(",")[0])
    return primary


def _cpv_by_party(db: Database, sql: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in db.query(sql):
        if row["party"] and row["cpv"]:
            out[row["party"]][row["cpv"]] += 1
    return out


# --- аркуші ---------------------------------------------------------------

def suppliers(db: Database) -> Sheet:
    """Хто продає: обсяг підписаних договорів у розрізі постачальників."""
    marks = ",".join("?" * len(LIVE_CONTRACTS))
    rows = db.query(f"""
        SELECT c.supplier_edrpou AS edrpou,
               MAX(c.supplier_name) AS name,
               COUNT(DISTINCT c.id) AS n_contracts,
               COUNT(DISTINCT c.tender_uuid) AS n_tenders,
               COUNT(DISTINCT t.pe_edrpou) AS n_buyers,
               SUM(c.value_amount) AS total,
               MIN(substr(t.date_created, 1, 10)) AS first_seen,
               MAX(substr(t.date_created, 1, 10)) AS last_seen
        FROM contracts c
        JOIN tenders t ON t.uuid = c.tender_uuid
        WHERE c.supplier_edrpou <> '' AND c.status IN ({marks})
        GROUP BY c.supplier_edrpou
        ORDER BY total DESC
    """, LIVE_CONTRACTS)

    cpv = _cpv_by_party(db, f"""
        SELECT c.supplier_edrpou AS party, i.cpv AS cpv
        FROM contracts c
        JOIN items i ON i.tender_uuid = c.tender_uuid
        WHERE c.supplier_edrpou <> '' AND c.status IN ({','.join(repr(s) for s in LIVE_CONTRACTS)})
    """)

    headers = ["ЄДРПОУ", "Постачальник", "Договорів", "Закупівель", "Замовників",
               "Сума договорів, грн", "Середній договір, грн", "Основні коди ДК021",
               "Перший договір", "Останній договір"]
    out = []
    for row in rows:
        total = _money(row["total"])
        out.append([
            row["edrpou"], row["name"], row["n_contracts"], row["n_tenders"], row["n_buyers"],
            total, round(total / max(row["n_contracts"], 1), 2),
            _top_cpv(cpv.get(row["edrpou"], {})), row["first_seen"], row["last_seen"],
        ])
    return headers, out


def buyers(db: Database) -> Sheet:
    """Хто купує: обсяг закупівель у розрізі замовників."""
    marks = ",".join("?" * len(LIVE_CONTRACTS))
    rows = db.query(f"""
        SELECT t.pe_edrpou AS edrpou,
               MAX(t.pe_name) AS name,
               MAX(t.pe_region) AS region,
               COUNT(DISTINCT t.uuid) AS n_tenders,
               SUM(t.value_amount) AS expected,
               (SELECT SUM(c.value_amount) FROM contracts c
                  JOIN tenders t2 ON t2.uuid = c.tender_uuid
                 WHERE t2.pe_edrpou = t.pe_edrpou AND c.status IN ({marks})) AS signed,
               (SELECT COUNT(DISTINCT c.supplier_edrpou) FROM contracts c
                  JOIN tenders t2 ON t2.uuid = c.tender_uuid
                 WHERE t2.pe_edrpou = t.pe_edrpou AND c.supplier_edrpou <> '') AS n_suppliers
        FROM tenders t
        WHERE t.pe_edrpou <> ''
        GROUP BY t.pe_edrpou
        ORDER BY signed DESC, expected DESC
    """, LIVE_CONTRACTS)

    cpv = _cpv_by_party(db, """
        SELECT t.pe_edrpou AS party, i.cpv AS cpv
        FROM tenders t JOIN items i ON i.tender_uuid = t.uuid
        WHERE t.pe_edrpou <> ''
    """)

    headers = ["ЄДРПОУ", "Замовник", "Регіон", "Закупівель", "Очікувана вартість, грн",
               "Сума договорів, грн", "Постачальників", "Основні коди ДК021"]
    return headers, [[
        row["edrpou"], row["name"], row["region"], row["n_tenders"],
        _money(row["expected"]), _money(row["signed"]), row["n_suppliers"] or 0,
        _top_cpv(cpv.get(row["edrpou"], {})),
    ] for row in rows]


def industries(db: Database) -> Sheet:
    """Галузі за ДК021. Кожна закупівля зараховується до одного головного коду."""
    primary = _primary_cpv(db)
    signed: dict[str, float] = defaultdict(float)
    for row in db.query(
            f"SELECT tender_uuid, SUM(value_amount) s FROM contracts "
            f"WHERE status IN ({','.join('?' * len(LIVE_CONTRACTS))}) GROUP BY tender_uuid",
            LIVE_CONTRACTS):
        signed[row["tender_uuid"]] = _money(row["s"])

    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "expected": 0.0, "signed": 0.0, "suppliers": set(), "buyers": set()})
    tenders = {r["uuid"]: dict(r) for r in db.query(
        "SELECT uuid, value_amount, pe_edrpou FROM tenders")}
    supplier_by_tender: dict[str, set[str]] = defaultdict(set)
    for row in db.query("SELECT tender_uuid, supplier_edrpou FROM contracts "
                        "WHERE supplier_edrpou <> ''"):
        supplier_by_tender[row["tender_uuid"]].add(row["supplier_edrpou"])

    for uuid, tender in tenders.items():
        code = primary.get(uuid)
        if not code:
            continue
        cell = agg[code]
        cell["n"] += 1
        cell["expected"] += _money(tender["value_amount"])
        cell["signed"] += signed.get(uuid, 0.0)
        cell["suppliers"] |= supplier_by_tender.get(uuid, set())
        if tender["pe_edrpou"]:
            cell["buyers"].add(tender["pe_edrpou"])

    headers = ["Код ДК021", "Назва", "Група", "Закупівель", "Очікувана вартість, грн",
               "Сума договорів, грн", "Постачальників", "Замовників"]
    out = []
    for code, cell in sorted(agg.items(), key=lambda kv: -kv[1]["signed"]):
        group = significant_prefix(code)[:5]
        out.append([code, cpv_name(code), group, cell["n"],
                    round(cell["expected"], 2), round(cell["signed"], 2),
                    len(cell["suppliers"]), len(cell["buyers"])])
    return headers, out


def monthly(db: Database) -> Sheet:
    """Динаміка ринку по місяцях."""
    marks = ",".join("?" * len(LIVE_CONTRACTS))
    rows = db.query(f"""
        SELECT substr(t.date_created, 1, 7) AS month,
               COUNT(DISTINCT t.uuid) AS n_tenders,
               SUM(t.value_amount) AS expected,
               (SELECT COUNT(*) FROM contracts c JOIN tenders t2 ON t2.uuid = c.tender_uuid
                 WHERE substr(t2.date_created, 1, 7) = substr(t.date_created, 1, 7)
                   AND c.status IN ({marks})) AS n_contracts,
               (SELECT SUM(c.value_amount) FROM contracts c JOIN tenders t2 ON t2.uuid = c.tender_uuid
                 WHERE substr(t2.date_created, 1, 7) = substr(t.date_created, 1, 7)
                   AND c.status IN ({marks})) AS signed
        FROM tenders t
        WHERE t.date_created <> ''
        GROUP BY month
        ORDER BY month
    """, LIVE_CONTRACTS * 2)
    headers = ["Місяць", "Закупівель", "Очікувана вартість, грн",
               "Договорів", "Сума договорів, грн", "Середній договір, грн"]
    return headers, [[
        row["month"], row["n_tenders"], _money(row["expected"]),
        row["n_contracts"] or 0, _money(row["signed"]),
        round(_money(row["signed"]) / max(row["n_contracts"] or 0, 1), 2),
    ] for row in rows]


def participants(db: Database) -> Sheet:
    """Хто виходить на торги і як часто виграє.

    Пропозиції видно лише для процедур з відкритими торгами — у звітах про
    укладений договір і спрощених закупівлях учасників у картці немає, тож
    «подань» тут завжди не менше за реальну активність, але не більше.
    """
    submitted = {r["edrpou"]: dict(r) for r in db.query("""
        SELECT bidder_edrpou AS edrpou, MAX(bidder_name) AS name,
               COUNT(DISTINCT tender_uuid) AS n
        FROM bids WHERE bidder_edrpou <> '' GROUP BY bidder_edrpou
    """)}
    won = {r["edrpou"]: dict(r) for r in db.query("""
        SELECT supplier_edrpou AS edrpou, MAX(supplier_name) AS name,
               COUNT(DISTINCT tender_uuid) AS n
        FROM awards WHERE supplier_edrpou <> '' AND status = 'active'
        GROUP BY supplier_edrpou
    """)}
    marks = ",".join("?" * len(LIVE_CONTRACTS))
    money = {r["edrpou"]: _money(r["total"]) for r in db.query(f"""
        SELECT supplier_edrpou AS edrpou, SUM(value_amount) AS total
        FROM contracts WHERE supplier_edrpou <> '' AND status IN ({marks})
        GROUP BY supplier_edrpou
    """, LIVE_CONTRACTS)}

    headers = ["ЄДРПОУ", "Компанія", "Подань (де видно пропозиції)", "Перемог",
               "Частка перемог", "Сума договорів, грн"]
    out = []
    for edrpou in sorted(set(submitted) | set(won),
                         key=lambda e: -money.get(e, 0.0)):
        name = (submitted.get(edrpou, {}).get("name")
                or won.get(edrpou, {}).get("name") or "")
        n_sub = submitted.get(edrpou, {}).get("n", 0)
        n_won = won.get(edrpou, {}).get("n", 0)
        # Перемог може виявитись більше, ніж видимих подань: у звітах про
        # договір і частині спрощених закупівель пропозицій у картці немає.
        # Показувати «133%» — брехня, тож у таких випадках частки не рахуємо.
        share = f"{n_won / n_sub:.0%}" if n_sub >= n_won and n_sub else "—"
        out.append([edrpou, name, n_sub, n_won, share, money.get(edrpou, 0.0)])
    return headers, out


def _rejected_awards(db: Database) -> list[dict]:
    """Відхилені рішення про переможця з розпізнаною причиною.

    Тільки ``unsuccessful``: скасоване рішення — це та сама подія вдруге
    (див. :mod:`app.core.rejections`).
    """
    rows = []
    for row in db.query("""
            SELECT a.supplier_edrpou AS edrpou, a.supplier_name AS name,
                   a.value_amount AS amount, a.reason, a.explanation,
                   t.pe_edrpou AS buyer_edrpou, t.pe_name AS buyer
            FROM awards a JOIN tenders t ON t.uuid = a.tender_uuid
            WHERE a.status = ?""", (rj.REJECTED_STATUS,)):
        cell = dict(row)
        cell["category"] = rj.classify(cell["reason"] or "", cell["explanation"] or "")
        rows.append(cell)
    return rows


def rejection_reasons(db: Database) -> Sheet:
    """Чому перемоги не стають договорами."""
    rows = _rejected_awards(db)
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "amount": 0.0, "firms": set(), "buyers": set()})
    for row in rows:
        cell = agg[row["category"]]
        cell["n"] += 1
        cell["amount"] += _money(row["amount"])
        cell["firms"].add(row["edrpou"])
        cell["buyers"].add(row["buyer_edrpou"])
    headers = ["Причина", "Випадків", "Частка", "Сума, грн", "Компаній", "Замовників"]
    return headers, [[
        name, cell["n"], f"{cell['n'] / len(rows):.0%}" if rows else "—",
        round(cell["amount"], 2), len(cell["firms"]), len(cell["buyers"]),
    ] for name, cell in sorted(agg.items(), key=lambda kv: -kv[1]["n"])]


def rejected_players(db: Database) -> Sheet:
    """Хто втрачає перемоги після того, як уже виграв."""
    rows = _rejected_awards(db)
    won = {r["edrpou"]: r["n"] for r in db.query(
        "SELECT supplier_edrpou AS edrpou, COUNT(DISTINCT tender_uuid) AS n"
        " FROM awards WHERE status = 'active' AND supplier_edrpou <> ''"
        " GROUP BY supplier_edrpou")}
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "n": 0, "amount": 0.0, "reasons": defaultdict(int)})
    for row in rows:
        cell = agg[row["edrpou"]]
        cell["name"] = cell["name"] or row["name"]
        cell["n"] += 1
        cell["amount"] += _money(row["amount"])
        cell["reasons"][row["category"]] += 1
    headers = ["ЄДРПОУ", "Компанія", "Скасовано перемог", "Виграно закупівель",
               "Частка зривів", "Сума, грн", "Головна причина"]
    out = []
    for edrpou, cell in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        wins = won.get(edrpou, 0)
        attempts = wins + cell["n"]
        top = sorted(cell["reasons"].items(), key=lambda kv: -kv[1])[0][0]
        out.append([edrpou, cell["name"], cell["n"], wins,
                    f"{cell['n'] / attempts:.0%}" if attempts else "—",
                    round(cell["amount"], 2), top])
    return headers, out


def overview(db: Database) -> Sheet:
    """Короткий підсумок вибірки."""
    marks = ",".join("?" * len(LIVE_CONTRACTS))
    n_tenders = db.scalar("SELECT COUNT(*) FROM tenders") or 0
    expected = _money(db.scalar("SELECT SUM(value_amount) FROM tenders"))
    signed = _money(db.scalar(
        f"SELECT SUM(value_amount) FROM contracts WHERE status IN ({marks})", LIVE_CONTRACTS))
    n_contracts = db.scalar(
        f"SELECT COUNT(*) FROM contracts WHERE status IN ({marks})", LIVE_CONTRACTS) or 0
    n_suppliers = db.scalar(
        "SELECT COUNT(DISTINCT supplier_edrpou) FROM contracts WHERE supplier_edrpou <> ''") or 0
    n_buyers = db.scalar("SELECT COUNT(DISTINCT pe_edrpou) FROM tenders WHERE pe_edrpou <> ''") or 0
    period = db.query("SELECT MIN(substr(date_created,1,10)) a, MAX(substr(date_created,1,10)) b "
                      "FROM tenders WHERE date_created <> ''")
    first, last = (period[0]["a"], period[0]["b"]) if period else ("", "")

    headers = ["Показник", "Значення"]
    return headers, [
        ["Період вибірки", f"{first} — {last}" if first else "—"],
        ["Закупівель", n_tenders],
        ["Очікувана вартість, грн", expected],
        ["Укладених договорів", n_contracts],
        ["Сума договорів, грн", signed],
        ["Постачальників", n_suppliers],
        ["Замовників", n_buyers],
        ["Середня сума договору, грн", round(signed / max(n_contracts, 1), 2)],
    ]


#: Аркуші, які створюються навіть порожніми: підсумок вибірки має бути
#: завжди, інакше книга без жодного договору виявилася б зовсім порожньою.
ALWAYS = ("Підсумок",)

#: Порядок аркушів у книзі.
SHEETS = {
    "Підсумок": overview,
    "Постачальники": suppliers,
    "Замовники": buyers,
    "Галузі ДК021": industries,
    "По місяцях": monthly,
    "Учасники торгів": participants,
    "Причини відмов": rejection_reasons,
    "Скасовані перемоги": rejected_players,
}


def build_sheets(db: Database) -> dict[str, Sheet]:
    # Порожній аркуш у зведенні — це питання «а де ж дані?», тож розділи,
    # яких у вибірці немає (скажімо, жодної відмови), просто не створюються.
    built = ((name, builder(db)) for name, builder in SHEETS.items())
    return {name: sheet for name, sheet in built if sheet[1] or name in ALWAYS}
