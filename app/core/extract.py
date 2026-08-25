"""Розбір картки закупівлі ЦБД на рядки таблиць і перелік файлів.

Файли шукаються рекурсивно по всьому JSON: у Prozorro документи трапляються
не лише в ``tender.documents``, а й у пропозиціях учасників, кваліфікаціях,
договорах, скаргах, скасуваннях, вимогах тощо. Рекурсивний обхід гарантує,
що жоден файл не буде пропущений навіть якщо структура зміниться.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

from .classifiers import cpv_name

#: Корені JSON → область (scope) документа.
SCOPE_BY_ROOT = {
    "documents": "tender",
    "lots": "tender",
    "items": "tender",
    "criteria": "tender",
    "milestones": "tender",
    "bids": "bid",
    "awards": "award",
    "qualifications": "award",
    "contracts": "contract",
    "agreements": "contract",
    "cancellations": "other",
    "complaints": "other",
    "plans": "other",
}

SCOPE_LABELS = {
    "tender": "Закупівля",
    "bid": "Пропозиція",
    "award": "Кваліфікація",
    "contract": "Договір",
    "other": "Інше",
}


def _org(node: dict | None) -> tuple[str, str, str]:
    """``(назва, ЄДРПОУ, регіон)`` з об'єкта організації."""
    if not isinstance(node, dict):
        return "", "", ""
    ident = node.get("identifier") or {}
    name = node.get("name") or ident.get("legalName") or ""
    edrpou = str(ident.get("id") or "")
    region = ((node.get("address") or {}).get("region")) or ""
    return name, edrpou, region


def _first_org(node: dict, *keys: str) -> tuple[str, str, str]:
    for key in keys:
        seq = node.get(key)
        if isinstance(seq, list) and seq:
            return _org(seq[0])
        if isinstance(seq, dict):
            return _org(seq)
    return "", "", ""


def _is_document(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and isinstance(node.get("url"), str)
        and node["url"].startswith("http")
        and ("title" in node or "format" in node or "documentType" in node)
    )


def iter_documents(tender: dict) -> Iterator[dict]:
    """Обходить усю картку і віддає кожен знайдений файл із контекстом."""

    def walk(node: Any, path: list, root: str, owner: tuple[str, str, str]) -> Iterator[dict]:
        if isinstance(node, dict):
            # Оновлюємо «власника» файлів, коли заходимо в пропозицію/нагороду/договір
            new_owner = owner
            found = _first_org(node, "tenderers", "suppliers")
            if found[0] or found[1]:
                new_owner = found
            if _is_document(node):
                yield _document_row(node, path, root, new_owner)
                return
            for key, value in node.items():
                if key in ("raw_json",):
                    continue
                yield from walk(value, path + [key], root or key, new_owner)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                yield from walk(value, path + [i], root, owner)

    for key, value in tender.items():
        yield from walk(value, [key], key, ("", "", ""))


def _document_row(doc: dict, path: list, root: str, owner: tuple[str, str, str]) -> dict:
    container = "".join(
        f"[{p}]" if isinstance(p, int) else (f".{p}" if i else str(p))
        for i, p in enumerate(path[:-1] if isinstance(path[-1], int) else path)
    )
    return {
        "doc_id": str(doc.get("id") or ""),
        "scope": SCOPE_BY_ROOT.get(root, "other"),
        "container": container,
        "owner_name": owner[0],
        "owner_edrpou": owner[1],
        "doc_type": doc.get("documentType") or "",
        "title": doc.get("title") or "",
        "format": doc.get("format") or "",
        "date_published": doc.get("datePublished") or doc.get("dateModified") or "",
        "url": doc.get("url") or "",
        "hash": doc.get("hash") or "",
        "confidentiality": doc.get("confidentiality") or "public",
    }


def latest_versions(docs: list[dict]) -> list[dict]:
    """Лишає найсвіжішу версію кожного файлу (одне ``doc_id`` у межах контейнера)."""
    best: dict[tuple[str, str], dict] = {}
    for doc in docs:
        key = (doc.get("container", ""), doc.get("doc_id") or doc.get("url", ""))
        prev = best.get(key)
        if prev is None or (doc.get("date_published") or "") >= (prev.get("date_published") or ""):
            best[key] = doc
    return list(best.values())


# --- розбір картки на рядки таблиць ---------------------------------------

def parse_tender(tender: dict, *, keep_urls: bool = True) -> dict:
    """Перетворює JSON закупівлі на набір рядків для БД.

    Повний JSON у базі не зберігається: він важить десятки кілобайт на
    закупівлю і на десятках тисяч записів роздув би файл бази на гігабайти.
    За потреби він лягає окремим файлом поруч із документами.
    """
    uuid = tender.get("id") or ""
    tender_id = tender.get("tenderID") or ""
    pe_name, pe_edrpou, pe_region = _org(tender.get("procuringEntity"))
    pe_locality = ((tender.get("procuringEntity") or {}).get("address") or {}).get("locality") or ""
    value = tender.get("value") or {}
    tender_period = tender.get("tenderPeriod") or {}

    lots = [
        (lot.get("id"), uuid, lot.get("title"), lot.get("description"), lot.get("status"),
         _num((lot.get("value") or {}).get("amount")), (lot.get("value") or {}).get("currency"))
        for lot in (tender.get("lots") or []) if isinstance(lot, dict)
    ]

    items = []
    cpvs: list[str] = []
    for item in (tender.get("items") or []):
        if not isinstance(item, dict):
            continue
        cls = item.get("classification") or {}
        code = cls.get("id") or ""
        if code:
            cpvs.append(code)
        items.append((
            item.get("id"), uuid, item.get("relatedLot"), item.get("description"),
            code, cls.get("description") or cpv_name(code),
            _num(item.get("quantity")), ((item.get("unit") or {}).get("name") or ""),
        ))

    bids = []
    for bid in (tender.get("bids") or []):
        if not isinstance(bid, dict):
            continue
        name, edrpou, region = _first_org(bid, "tenderers")
        lot_values = bid.get("lotValues") or []
        if lot_values:
            for lv in lot_values:
                bids.append((
                    f"{bid.get('id')}:{lv.get('relatedLot')}", uuid, lv.get("relatedLot"),
                    lv.get("status") or bid.get("status"), bid.get("date"),
                    _num((lv.get("value") or {}).get("amount")),
                    (lv.get("value") or {}).get("currency"), name, edrpou, region,
                ))
        else:
            bids.append((
                bid.get("id"), uuid, None, bid.get("status"), bid.get("date"),
                _num((bid.get("value") or {}).get("amount")),
                (bid.get("value") or {}).get("currency"), name, edrpou, region,
            ))

    awards = []
    supplier_by_award: dict[str, tuple[str, str]] = {}
    for award in (tender.get("awards") or []):
        if not isinstance(award, dict):
            continue
        name, edrpou, _ = _first_org(award, "suppliers")
        if award.get("id"):
            supplier_by_award[award["id"]] = (name, edrpou)
        awards.append((
            award.get("id"), uuid, award.get("lotID"), award.get("status"), award.get("date"),
            _num((award.get("value") or {}).get("amount")),
            (award.get("value") or {}).get("currency"), name, edrpou,
        ))

    contracts = []
    for contract in (tender.get("contracts") or []):
        if not isinstance(contract, dict):
            continue
        name, edrpou, _ = _first_org(contract, "suppliers")
        if not edrpou:
            # У картці закупівлі `contracts[].suppliers` майже завжди порожній —
            # постачальник вказаний у рішенні про переможця, на яке договір
            # посилається через `awardID`. Без цієї підстановки вся аналітика
            # «хто продав і на яку суму» лишається без ЄДРПОУ.
            name, edrpou = supplier_by_award.get(contract.get("awardID"), (name, edrpou))
        contracts.append((
            contract.get("id"), uuid, contract.get("contractID"), contract.get("status"),
            contract.get("dateSigned") or contract.get("date"),
            _num((contract.get("value") or {}).get("amount")),
            (contract.get("value") or {}).get("currency"), name, edrpou,
        ))

    docs_raw = list(iter_documents(tender))
    doc_rows = []
    for doc in docs_raw:
        key = f"{uuid}|{doc['container']}|{doc['doc_id'] or doc['url'][-32:]}|{doc['date_published']}"
        # Коли файли не качаємо, посилання зберігати нема сенсу: у типовій
        # закупівлі це два десятки підписаних адрес по двісті символів, і на
        # сорока тисячах закупівель вони самі важать більше, ніж усі картки
        # разом. Назва, тип і власник файлу лишаються — саме за ними аналітика
        # впізнає сертифікати й авторизаційні листи.
        doc_rows.append((
            key, doc["doc_id"], uuid, tender_id, doc["scope"], doc["container"],
            doc["owner_name"], doc["owner_edrpou"], doc["doc_type"], doc["title"],
            doc["format"], doc["date_published"], doc["url"] if keep_urls else "",
            doc["hash"], 0, "", "pending", "",
        ))

    row = {
        "uuid": uuid,
        "tender_id": tender_id,
        "title": tender.get("title") or "",
        "description": tender.get("description") or "",
        "status": tender.get("status") or "",
        "method_type": tender.get("procurementMethodType") or "",
        "main_category": tender.get("mainProcurementCategory") or "",
        "date_created": tender.get("dateCreated") or "",
        "date_modified": tender.get("dateModified") or "",
        "tender_start": tender_period.get("startDate") or "",
        "tender_end": tender_period.get("endDate") or "",
        "value_amount": _num(value.get("amount")),
        "value_currency": value.get("currency") or "",
        "vat_included": 1 if value.get("valueAddedTaxIncluded") else 0,
        "pe_name": pe_name,
        "pe_edrpou": pe_edrpou,
        "pe_region": pe_region,
        "pe_locality": pe_locality,
        "n_lots": len(lots),
        "n_bids": len({b[0] for b in bids}),
        "n_docs": len(doc_rows),
        "cpv_list": ",".join(sorted(set(cpvs))),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    return {
        "row": row, "lots": lots, "items": items, "bids": bids,
        "awards": awards, "contracts": contracts, "docs": doc_rows,
        "documents_meta": docs_raw,
    }


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
