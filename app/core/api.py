"""Доступ до відкритих API Prozorro.

Використовуються два джерела:

1. **Пошуковий API порталу** ``https://prozorro.gov.ua/api/search/{tenders,contracts,plans}``
   (POST, JSON). Дозволяє фільтрувати за ДК021 (``cpv``), ЄДРПОУ учасника
   (``tenderer``), ЄДРПОУ замовника (``buyer``), статусом і типом процедури.
   Повертає стислу картку закупівлі (без внутрішнього ``id``), по 20 записів
   на сторінку, максимум 500 сторінок (10 000 записів) на запит.
   Результати відсортовані за датою у зворотному порядку — це дозволяє
   зупиняти гортання, щойно дійшли до початку потрібного періоду.

2. **Центральна база даних (ЦБД)** ``https://public.api.openprocurement.org/api/2.5``
   — офіційний відкритий API. Повний JSON закупівлі: лоти, номенклатура з
   кодами ДК021, пропозиції учасників, кваліфікації, договори і, головне,
   прямі посилання на всі файли.

Зв'язок між джерелами: пошук дає ``tenderID`` (UA-2026-...), а ЦБД працює з
внутрішнім UUID. Відповідність будується модулем :mod:`app.core.resolver`.
"""
from __future__ import annotations

from typing import Any, Iterator

from .http import HttpClient

SEARCH_BASE = "https://prozorro.gov.ua/api/search"

#: Картка закупівлі за **людським** номером (``UA-2026-08-25-009929-a``).
#: Єдине відоме джерело, яке приймає номер напряму й повертає внутрішній UUID;
#: сам портал саме ним і користується, відкриваючи сторінку закупівлі.
#: Відповідь — близько 2 КБ, тоді як ``/details`` віддає 100 КБ.
SUMMARY_URL = "https://prozorro.gov.ua/api/tenders/{tender_id}/summary"
CDB_BASE = "https://public.api.openprocurement.org/api/2.5"

SEARCH_PAGE_SIZE = 20
SEARCH_MAX_PAGE = 500          # обмеження серверу
SEARCH_MAX_RESULTS = SEARCH_PAGE_SIZE * SEARCH_MAX_PAGE

#: Поля стрічки змін. ЦБД дозволяє лише невеликий перелік
#: (tenderID, status, procurementMethodType, dateCreated, procuringEntity, contracts).
#:
#: Індексу потрібні тільки ``tenderID`` та ``id`` — більше з таблиці
#: ``tender_index`` ніде не читається. Виміряно: із ``dateCreated,status``
#: запис стрічки важить 209 байт, без них — 135, тобто на 36% менше. Побудова
#: індексу на 99% складається з очікування мережі, тож це прямий виграш.
FEED_OPT_FIELDS = "tenderID"


class SearchApi:
    """Пошуковий API порталу prozorro.gov.ua."""

    def __init__(self, client: HttpClient):
        self.c = client

    # --- низькорівневе ----------------------------------------------------

    def _search(self, entity: str, body: dict) -> dict:
        return self.c.post_json(f"{SEARCH_BASE}/{entity}", body)

    def query(self, entity: str, body: dict) -> dict:
        """Довільний запит до пошуку (тіло формує викликач)."""
        return self._search(entity, body)

    @staticmethod
    def build_body(
        *,
        page: int = 1,
        text: str = "",
        cpv: list[str] | None = None,
        tenderer: list[str] | None = None,
        buyer: list[str] | None = None,
        supplier: list[str] | None = None,
        status: list[str] | None = None,
        proc_type: list[str] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"page": page}
        # API вимагає, щоб `text` був непорожнім рядком, якщо переданий взагалі.
        if text:
            body["text"] = text
        if cpv:
            body["cpv"] = list(cpv)
        if tenderer:
            body["tenderer"] = list(tenderer)
        if buyer:
            body["buyer"] = list(buyer)
        if supplier:
            body["supplier"] = list(supplier)
        if status:
            body["status"] = list(status)
        if proc_type:
            body["proc_type"] = list(proc_type)
        return body

    # --- публічне ---------------------------------------------------------

    def count(self, entity: str = "tenders", **kwargs) -> int:
        """Скільки всього записів відповідає фільтру (максимум 10 000)."""
        data = self._search(entity, self.build_body(page=1, **kwargs))
        return int(data.get("total") or 0)

    def pages(self, entity: str = "tenders", *, max_pages: int = SEARCH_MAX_PAGE, **kwargs
              ) -> Iterator[tuple[int, int, list[dict]]]:
        """Гортає сторінки пошуку.

        Дає кортежі ``(номер сторінки, усього записів, записи)``.
        Зупиняється на порожній сторінці або на ліміті сервера.
        """
        total = -1
        for page in range(1, min(max_pages, SEARCH_MAX_PAGE) + 1):
            self.c.check_cancel()
            data = self._search(entity, self.build_body(page=page, **kwargs))
            rows = data.get("data") or []
            if total < 0:
                total = int(data.get("total") or 0)
            yield page, total, rows
            if not rows or page * SEARCH_PAGE_SIZE >= total:
                break


class CdbApi:
    """Центральна база даних Prozorro (openprocurement 2.5)."""

    def __init__(self, client: HttpClient):
        self.c = client

    def tender(self, uuid: str) -> dict:
        """Повна картка закупівлі."""
        return self.c.get_json(f"{CDB_BASE}/tenders/{uuid}")["data"]

    def contract(self, uuid: str) -> dict:
        """Картка договору (містить ``tender_id`` — UUID закупівлі)."""
        return self.c.get_json(f"{CDB_BASE}/contracts/{uuid}")["data"]

    def feed(self, offset: str, *, limit: int = 1000, opt_fields: str = FEED_OPT_FIELDS
             ) -> Iterator[list[dict]]:
        """Стрічка змін закупівель, починаючи з ``offset``.

        ``offset`` — або дата ``YYYY-MM-DD``, або токен із попередньої відповіді.
        Генератор віддає пачки записів і зупиняється, коли сторінка порожня.
        """
        url = f"{CDB_BASE}/tenders"
        params: dict[str, Any] = {"limit": limit, "opt_fields": opt_fields, "offset": offset}
        while True:
            self.c.check_cancel()
            data = self.c.get_json(url, params=params)
            rows = data.get("data") or []
            yield rows
            if not rows:
                return
            nxt = (data.get("next_page") or {}).get("uri")
            if not nxt:
                return
            url, params = nxt, None
