"""Електронний каталог Prozorro Market — картки товарів.

Тендерна картка не містить ані технічних характеристик товару, ані його фото,
ані ціни за одиницю в розрізі ринку. Усе це живе в окремому каталозі, звідки
закупівлі за прямими договорами й беруть позиції. Для аналізу продукції це
основне джерело:

* ``title`` і характеристика «Бренд» — торгова марка та модель;
* ``requirementResponses`` — структуровані технічні параметри з одиницями виміру
  (діагональ, процесор, обсяг пам'яті, гарантійний термін тощо);
* ``images`` — посилання на фото картки;
* ``latestPrice`` з нижнім і верхнім квартилем — цінове позиціонування;
* ``identifier`` — штрихкод EAN, за яким товар можна знайти в інших джерелах;
* ``owner`` — майданчик, який веде картку.

Каталог класифікує товари **на рівні класу** ДК021 (``30210000-4``), тоді як
закупівлі — на рівні конкретного коду (``30213300-8``). Тому перед пошуком
обрані коди підіймаються до свого класу.
"""
from __future__ import annotations

from typing import Iterator

from .classifiers import by_prefix
from .http import HttpClient

SEARCH_URL = "https://prozorro.gov.ua/api/search/products"
PRODUCT_URL = "https://prozorro.gov.ua/api/products/{id}"

PAGE_SIZE = 20
MAX_PAGE = 500


def class_codes(codes: list[str]) -> list[str]:
    """``['30213300-8', '30213100-6']`` → ``['30210000-4']`` — коди рівня класу."""
    out: dict[str, None] = {}
    for code in codes or []:
        digits = "".join(ch for ch in str(code).split("-")[0] if ch.isdigit())
        if len(digits) < 4:
            continue
        full = by_prefix().get(digits[:4].rstrip("0") or digits[:4])
        if full:
            out.setdefault(full[0], None)
    return list(out)


class MarketApi:
    def __init__(self, client: HttpClient):
        self.c = client

    def search(self, *, cpv: list[str] | None = None, text: str = "",
               max_pages: int = MAX_PAGE) -> Iterator[tuple[int, int, list[dict]]]:
        """Гортає каталог. Дає ``(сторінка, усього, картки)``."""
        total = -1
        for page in range(1, min(max_pages, MAX_PAGE) + 1):
            self.c.check_cancel()
            body: dict = {"page": page}
            if cpv:
                body["cpv"] = list(cpv)
            if text:
                body["text"] = text
            data = self.c.post_json(SEARCH_URL, body)
            rows = data.get("data") or []
            if total < 0:
                total = int(data.get("total") or 0)
            yield page, total, rows
            if not rows or page * PAGE_SIZE >= total:
                return

    def product(self, product_id: str) -> dict:
        """Повна картка товару з характеристиками, фото та описом."""
        return self.c.get_json(PRODUCT_URL.format(id=product_id))


def parse_product(card: dict, brief: dict | None = None) -> tuple[dict, list[tuple]]:
    """Розкладає картку на рядок товару та перелік його характеристик."""
    brief = brief or {}
    price = card.get("latestPrice") or brief.get("latestPrice") or {}
    classification = card.get("classification") or brief.get("classification") or {}
    identifier = card.get("identifier") or brief.get("identifier") or {}
    images = [u for u in (card.get("images") or []) if isinstance(u, str)]

    specs = card.get("requirementResponses") or []
    brand = ""
    for spec in specs:
        if str(spec.get("requirement") or "").strip().lower() == "бренд":
            brand = ", ".join(str(v) for v in (spec.get("values") or []))
            break

    product_id = card.get("id") or brief.get("id") or ""
    row = {
        "id": product_id,
        "title": card.get("title") or brief.get("title") or "",
        "brand": brand or _as_text(card.get("brand")),
        "description": card.get("description") or "",
        "category": card.get("categoryTitle") or "",
        "cpv": _cpv_of(classification),
        "cpv_name": classification.get("description") or "",
        "barcode": str(identifier.get("id") or ""),
        "barcode_scheme": identifier.get("scheme") or "",
        "status": card.get("status") or brief.get("status") or "",
        "marketplace": card.get("owner") or "",
        "vendor": _as_text(card.get("vendor")),
        "price_low": _num(price.get("lowerQuartile")),
        "price_high": _num(price.get("upperQuartile")),
        "price_currency": price.get("currency") or "",
        "price_vat": 1 if price.get("valueAddedTaxIncluded") else 0,
        "price_date": (price.get("date") or "")[:10],
        "n_images": len(images),
        "images": " ".join(images),
        "n_specs": len(specs),
        "description_len": len(card.get("description") or ""),
        "date_created": (card.get("dateCreated") or "")[:10],
        "date_modified": (card.get("dateModified") or brief.get("dateModified") or "")[:10],
        "expiration_date": (card.get("expirationDate") or "")[:10],
        "url": card.get("url") or "",
    }

    spec_rows = []
    for spec in specs:
        name = str(spec.get("requirement") or "").strip()
        if not name:
            continue
        values = spec.get("values")
        if not isinstance(values, list):
            values = [values]
        text = ", ".join("" if v is None else str(v) for v in values)
        number = _num(values[0]) if len(values) == 1 else None
        spec_rows.append((product_id, name, text, number, spec.get("unitName") or ""))
    return row, spec_rows


def _cpv_of(classification: dict) -> str:
    """У картці код буває окремим полем або всередині опису."""
    code = classification.get("id")
    if code:
        return str(code)
    description = str(classification.get("description") or "")
    for token in description.replace(":", " ").split():
        if len(token) == 10 and token[8] == "-" and token[:8].isdigit():
            return token
    return ""


def _as_text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("title") or "")
    return "" if value is None else str(value)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
