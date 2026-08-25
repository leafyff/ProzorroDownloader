"""Розпізнавання торгових марок і змісту документів за назвами.

Картка закупівлі не має поля «бренд»: торгова марка живе в тексті позиції
(«Ноутбук Vinga Iron S140»), а іноді лише в назві лінійки моделей
(«ThinkPad E14»). Тому ТМ шукаємо словником із довідника ``data/brands.json``,
де до кожної канонічної назви прив'язані її написання — кирилицею, з дефісом
і назвами модельних рядів.

Другий канал сигналів — назви файлів у закупівлі. Учасник кладе в пропозицію
сертифікати, авторизаційні листи виробника, технічні специфікації; за іменем
файлу видно, які саме документи він подає, а це вже характеристика гравця
(підтверджена якість, статус партнера виробника, глибина підготовки).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache

from ..paths import BUNDLED_DATA


@lru_cache(maxsize=1)
def _book() -> dict:
    try:
        with open(BUNDLED_DATA / "brands.json", "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def catalog() -> dict[str, list[str]]:
    """``{канонічна ТМ: [написання…]}`` — без службових ключів."""
    raw = _book().get("brands") or {}
    return {name: list(aliases or []) for name, aliases in raw.items()
            if not name.startswith("_")}


@lru_cache(maxsize=1)
def own_tm() -> dict[str, dict]:
    """ТМ, які довідник позначає як власні марки конкретних компаній."""
    raw = _book().get("own_tm") or {}
    return {name: dict(info or {}) for name, info in raw.items() if not name.startswith("_")}


@lru_cache(maxsize=1)
def distributors() -> dict[str, list[str]]:
    """``{ТМ: [дистриб'ютори…]}`` із довідника — те, що вніс користувач."""
    raw = _book().get("distributors") or {}
    out: dict[str, list[str]] = {}
    for name, value in raw.items():
        if name.startswith("_"):
            continue
        out[name] = [str(v) for v in value] if isinstance(value, list) else [str(value)]
    return out


@lru_cache(maxsize=1)
def stopwords() -> frozenset[str]:
    return frozenset(str(w).lower() for w in (_book().get("stopwords") or []))


#: Розділювач слів: усе, що не літера й не цифра. Пробіли, дефіси й розділові
#: знаки в написанні ТМ рівнозначні — у текстах закупівель трапляється і
#: «TP-Link», і «TP Link», і «TPLink».
_WORDS = re.compile(r"[^0-9A-Za-zА-Яа-яЁёЇїІіЄєҐґ]+")


def _key(text: str) -> str:
    """Написання ТМ у вигляді, придатному для порівняння."""
    return "".join(_WORDS.split(str(text).lower()))


@lru_cache(maxsize=1)
def _lookup() -> dict[str, str]:
    """``{написання без розділювачів: канонічна ТМ}``."""
    out: dict[str, str] = {}
    for canonical, aliases in catalog().items():
        for text in [canonical, *aliases]:
            key = _key(text)
            if key:
                out.setdefault(key, canonical)
    return out


@lru_cache(maxsize=1)
def _span() -> int:
    """Найдовше написання ТМ у словах — «APC by Schneider» це три."""
    longest = 1
    for canonical, aliases in catalog().items():
        for text in [canonical, *aliases]:
            words = [w for w in _WORDS.split(str(text)) if w]
            longest = max(longest, len(words))
    return longest


def detect(text: str) -> list[str]:
    """Канонічні назви ТМ, знайдені в тексті. Порядок — як у тексті.

    Пошук іде не одним великим регулярним виразом, а за словами: текст
    розбивається на токени, і кожен ланцюжок із одного-трьох слів шукається у
    словнику. На двадцяти тисячах позицій це різниця між частками секунди й
    десятками, бо альтернатива з двохсот написань перевірялася б у кожній
    позиції кожного рядка.

    Довші сполучення пробуються першими: «Cooler Master» має вигравати в
    «Cooler», а «APC by Schneider» — в «APC».
    """
    if not text:
        return []
    tokens = [token for token in _WORDS.split(str(text).lower()) if token]
    if not tokens:
        return []
    lookup = _lookup()
    span = _span()
    found: list[str] = []
    total = len(tokens)
    for start in range(total):
        for size in range(min(span, total - start), 0, -1):
            key = tokens[start] if size == 1 else "".join(tokens[start:start + size])
            canonical = lookup.get(key)
            if canonical:
                if canonical not in found:
                    found.append(canonical)
                break
    return found


def main_brand(texts: list[str]) -> str:
    """Найчастіша ТМ у наборі текстів; порожньо — якщо жодної не впізнано."""
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(detect(text))
    return counter.most_common(1)[0][0] if counter else ""


_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,19}")


def candidates(texts: list[str], min_count: int = 3) -> list[tuple[str, int]]:
    """Латинські слова, схожі на ТМ, яких ще немає в довіднику.

    Потрібні, щоб довідник не старів: якщо на ринку з'явився новий бренд,
    він одразу видно в цьому списку і його можна дописати у ``brands.json``.
    """
    known = set(_lookup())
    counter: Counter[str] = Counter()
    stop = stopwords()
    for text in texts:
        for token in _TOKEN.findall(str(text or "")):
            plain = token.lower().replace("-", "")
            if plain in stop or plain in known or plain.isdigit():
                continue
            if len(plain) < 3:
                continue
            counter[token.strip("-")] += 1
    return [(word, n) for word, n in counter.most_common(60) if n >= min_count]


# --- зміст документів за назвою файлу -------------------------------------

#: ``категорія → (регулярний вираз, пояснення)``. Порядок важливий: перша
#: категорія, що збіглася, і виграє.
DOC_KINDS: dict[str, str] = {
    "Підпис КЕП": r"\.p7s$|^sign\b|^signature",
    "Сертифікат / декларація відповідності":
        r"сертифік|certificat|декларац|відповідност|соответств|iso\s*\d|дсту|висновок сес|"
        r"санітарн|гігієніч",
    "Лист виробника / авторизація":
        r"авториз|authoriz|лист.{0,20}виробник|виробник.{0,20}лист|дилер|dealer|"
        r"дистриб|distribut|партнер|partner|гарантійн.{0,15}лист",
    "Технічна специфікація":
        r"технічн|техническ|специфікац|specificat|характеристик|datasheet|опис товар",
    "Цінова пропозиція": r"цінов|ценов|прайс|price|кошторис|розрахунок ціни",
    "Досвід / аналогічні договори": r"аналогічн|досвід|референс|reference|виконан.{0,15}договор",
    "Гарантія та сервіс": r"гарант|сервіс|обслуговуванн|warranty",
    "Установчі та реєстраційні": r"статут|витяг|виписк|наказ|довіреніст|протокол|"
                                 r"свідоцтв|реєстрац|єдрпоу|податков",
    "Пропозиція учасника": r"пропозиц|offer|заявка",
    "Тендерна документація": r"тендерн|документац|оголошенн|проєкт договор|проект договор|"
                             r"додаток|додатк|форма",
    "Договір": r"договір|договор|contract|угода|специфікація до договор",
}

_DOC_RE = {kind: re.compile(rule, re.IGNORECASE) for kind, rule in DOC_KINDS.items()}


@lru_cache(maxsize=100_000)
def document_kind(title: str, doc_format: str = "") -> str:
    """Що це за файл, судячи з назви. ``''`` — не вдалося віднести.

    Назви файлів у закупівлях повторюються десятками тисяч («sign.p7s»,
    «Тендерна пропозиція.pdf»), а перевірка проганяє десяток регулярних
    виразів, тож без кеша це найдорожче місце всього аналізу.
    """
    text = str(title or "")
    if not text:
        return ""
    if "pkcs7" in str(doc_format or "").lower():
        return "Підпис КЕП"
    for kind, rule in _DOC_RE.items():
        if rule.search(text):
            return kind
    return ""


#: Категорії, наявність яких у пропозиції — сильний сигнал про гравця.
STRONG_KINDS = ("Сертифікат / декларація відповідності", "Лист виробника / авторизація")
