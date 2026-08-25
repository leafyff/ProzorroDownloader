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

Звідки що беремо
----------------

Картки читаємо з **офіційного API каталогу** ``market-api.prozorro.gov.ua``
(документація: https://market-api.prozorro.gov.ua/api/doc). Раніше тут стояв
шлях ``prozorro.gov.ua/api/products/{id}`` — недокументований проксі порталу
над тим самим каталогом: у його відповіді полем ``url`` вказано саме адресу
``market-api``, а фото він і так віддавав з цього ж хоста. Проксі при цьому
збіднював картку — губив код одиниці виміру (``unit.code``) та класифікацію
характеристики за ESPD211 — і натомість додавав порожні поля старої моделі
каталогу (``latestPrice``, ``categoryTitle``).

Пошуку за ДК021 в ``market-api`` немає: ``GET /api/products`` віддає лише
хронологічну стрічку змін (``{id, dateModified}``, сторонні фільтри мовчки
ігноруються), а ``POST /api/search`` — це добір за готовим переліком ``id``,
та й той закритий для анонімних клієнтів. Тож **відбираємо** картки класу
пошуком порталу, а **читаємо** їх з ``market-api``.
"""
from __future__ import annotations

from typing import Iterator

from .classifiers import by_prefix
from .http import Cancelled, HttpClient

#: Офіційний API електронного каталогу — джерело карток товарів.
MARKET_BASE = "https://market-api.prozorro.gov.ua"
PRODUCT_URL = MARKET_BASE + "/api/products/{id}"
CATEGORY_URL = MARKET_BASE + "/api/categories/{id}"

#: Пошук порталу — єдиний спосіб відібрати картки за класом ДК021 (див. вище).
SEARCH_URL = "https://prozorro.gov.ua/api/search/products"

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
        self._categories: dict[str, str] = {}

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
        data = self.c.get_json(PRODUCT_URL.format(id=product_id))
        return _unwrap(data)

    def category_title(self, category_id: str) -> str:
        """Назва категорії каталогу за її кодом.

        Картка знає лише код (``relatedCategory``), а назву тримає окремий
        ресурс. Відповіді кешуємо: категорій на порядки менше, ніж товарів,
        і та сама повторюється в сотнях карток.
        """
        cid = str(category_id or "")
        if not cid:
            return ""
        if cid not in self._categories:
            try:
                node = _unwrap(self.c.get_json(CATEGORY_URL.format(id=cid)))
                self._categories[cid] = str(node.get("title") or "")
            except Cancelled:
                raise
            except Exception:
                # Назва — прикраса, а не дані: без неї картка лишається цілою.
                self._categories[cid] = ""
        return self._categories[cid]


def _unwrap(data) -> dict:
    """``market-api`` загортає об'єкт у ``{"data": ...}``, проксі віддавав його
    плоским. Приймаємо обидва, щоб розбір не залежав від джерела."""
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


def parse_product(card: dict, brief: dict | None = None, *,
                  category_title: str = "") -> tuple[dict, list[tuple]]:
    """Розкладає картку на рядок товару та перелік його характеристик.

    Приймає і відповідь ``market-api``, і стару відповідь проксі порталу:
    формати різняться дрібницями (див. :func:`_spec_values`, :func:`_images_of`),
    а стисла картка з пошуку доповнює те, чого в детальній немає.
    """
    brief = brief or {}
    price = card.get("latestPrice") or brief.get("latestPrice") or {}
    classification = card.get("classification") or brief.get("classification") or {}
    identifier = card.get("identifier") or brief.get("identifier") or {}
    images = _images_of(card) or _images_of(brief)

    specs = card.get("requirementResponses") or brief.get("requirementResponses") or []
    brand = ""
    for spec in specs:
        if str(spec.get("requirement") or "").strip().lower() == "бренд":
            brand = _spec_text(spec)
            break

    product_id = card.get("id") or brief.get("id") or ""
    row = {
        "id": product_id,
        "title": card.get("title") or brief.get("title") or "",
        "brand": brand or _as_text(card.get("brand")),
        "description": _plain_text(card.get("description")),
        "category": (category_title or card.get("categoryTitle")
                     or brief.get("categoryTitle") or ""),
        "cpv": _cpv_of(classification),
        "cpv_name": classification.get("description") or "",
        "barcode": str(identifier.get("id") or ""),
        "barcode_scheme": identifier.get("scheme") or "",
        "status": card.get("status") or brief.get("status") or "",
        "marketplace": card.get("owner") or brief.get("owner") or "",
        "vendor": _as_text(card.get("vendor")) or _as_text(brief.get("vendor")),
        "price_low": _num(price.get("lowerQuartile")),
        "price_high": _num(price.get("upperQuartile")),
        "price_currency": price.get("currency") or "",
        "price_vat": 1 if price.get("valueAddedTaxIncluded") else 0,
        "price_date": (price.get("date") or "")[:10],
        "n_images": len(images),
        "images": " ".join(images),
        "n_specs": len(specs),
        "description_len": len(_plain_text(card.get("description"))),
        "date_created": (card.get("dateCreated") or "")[:10],
        "date_modified": (card.get("dateModified") or brief.get("dateModified") or "")[:10],
        "expiration_date": (card.get("expirationDate") or "")[:10],
        "url": card.get("url") or (PRODUCT_URL.format(id=product_id) if product_id else ""),
    }

    spec_rows = []
    for spec in specs:
        name = str(spec.get("requirement") or "").strip()
        if not name:
            continue
        values = _spec_values(spec)
        number = _num(values[0]) if len(values) == 1 else None
        spec_rows.append((product_id, name, _spec_text(spec), number, _spec_unit(spec)))
    return row, spec_rows


def _spec_values(spec: dict) -> list:
    """Значення характеристики єдиним переліком.

    Каталог кладе одне значення в ``value``, кілька — у ``values``; проксі
    порталу завжди віддавав ``values``, навіть для одного значення.
    """
    values = spec.get("values")
    if isinstance(values, list):
        return values
    if values is not None:
        return [values]
    value = spec.get("value")
    return [] if value is None else [value]


def _spec_text(spec: dict) -> str:
    return ", ".join(_value_text(v) for v in _spec_values(spec))


def _value_text(value) -> str:
    """Текст одного значення характеристики.

    Каталог віддає числа дійсними (``8.0``) навіть там, де вони цілі, тоді як
    проксі порталу віддавав ``8``. Без зведення до спільного вигляду та сама
    діагональ двоїлася б на «8» і «8.0» під час групування у звіті.
    """
    if value is None:
        return ""
    if isinstance(value, bool):            # bool — підклас int, перевіряємо перш за все
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _spec_unit(spec: dict) -> str:
    """Одиниця виміру: ``unit.name`` у каталозі, ``unitName`` у проксі."""
    unit = spec.get("unit")
    if isinstance(unit, dict):
        return str(unit.get("name") or "")
    return str(spec.get("unitName") or "")


def _images_of(card: dict) -> list[str]:
    """Фото картки як абсолютні посилання.

    Каталог дає об'єкти з відносним шляхом (``{"url": "/static/images/…",
    "title": …, "hash": …}``), проксі віддавав готові рядки.
    """
    out: list[str] = []
    for item in (card or {}).get("images") or []:
        if isinstance(item, str):
            url = item
        elif isinstance(item, dict):
            url = str(item.get("url") or "")
        else:
            continue
        if not url:
            continue
        out.append(MARKET_BASE + url if url.startswith("/") else url)
    return out


def _plain_text(value) -> str:
    """У базу має потрапити рядок: опис буває ``null`` або переліком."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return _as_text(value)
    return str(value)


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
