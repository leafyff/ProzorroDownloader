"""Класифікатор ДК021:2015 (CPV) та довідник регіонів.

Коди ДК021 мають вигляд ``30213300-8``: вісім цифр і контрольна цифра.
Ієрархія кодується нулями праворуч: ``30000000`` — розділ, ``30200000`` — група,
``30213000`` — клас, ``30213300`` — категорія. Тому «значущий префікс» коду —
це його цифри без хвостових нулів (мінімум дві), і саме за такими префіксами
зручно і будувати дерево, і фільтрувати.
"""
from __future__ import annotations

import json
from functools import lru_cache

from ..paths import BUNDLED_DATA


@lru_cache(maxsize=1)
def dk021() -> dict[str, str]:
    """Повний словник ``{код: назва}`` ДК021:2015 (≈9 500 позицій)."""
    path = BUNDLED_DATA / "dk021_uk.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=1)
def regions() -> list[str]:
    path = BUNDLED_DATA / "ua_regions.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return list(json.load(fh))
    except (OSError, ValueError):
        return []


def cpv_name(code: str) -> str:
    return dk021().get(code, "")


def significant_prefix(code: str) -> str:
    """``30213300-8`` → ``302133``; ``30000000-9`` → ``30``."""
    digits = "".join(ch for ch in str(code or "").split("-")[0] if ch.isdigit())
    trimmed = digits.rstrip("0")
    return trimmed if len(trimmed) >= 2 else digits[:2]


#: Синонім для читабельності у місцях, де на вході — введений користувачем текст.
normalize_prefix = significant_prefix


@lru_cache(maxsize=1)
def by_prefix() -> dict[str, tuple[str, str]]:
    """``{значущий префікс: (повний код, назва)}``."""
    return {significant_prefix(code): (code, name) for code, name in dk021().items()}


@lru_cache(maxsize=64)
def _expand(norm: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(code for code in dk021()
                        if any(code.split("-")[0].startswith(p) for p in norm)))


def expand_prefixes(prefixes: list[str]) -> list[str]:
    """Розгортає префікси ДК021 у повний перелік конкретних кодів.

    Пошуковий API Prozorro шукає **точний збіг** коду — ієрархії він не знає.
    Тому «розділ 30» треба перетворити на всі 401 коди, що з нього починаються.

    Перебір іде по всіх ≈9 500 кодах, а функцію смикають на кожній закупівлі,
    тож результат кешується за набором префіксів.
    """
    norm = [significant_prefix(p) for p in (prefixes or []) if str(p).strip()]
    norm = tuple(sorted({p for p in norm if p}))
    if not norm:
        return []
    return list(_expand(norm))


@lru_cache(maxsize=1)
def _hierarchy() -> tuple[dict[str, list[str]], dict[str, str], list[str]]:
    """``(діти, батько, корені)`` — за значущими префіксами."""
    keys = sorted(by_prefix())
    children: dict[str, list[str]] = {}
    parent: dict[str, str] = {}
    known = set(keys)
    roots: list[str] = []
    for key in keys:
        found = ""
        for cut in range(len(key) - 1, 1, -1):
            candidate = key[:cut]
            if candidate in known:
                found = candidate
                break
        if found:
            parent[key] = found
            children.setdefault(found, []).append(key)
        else:
            roots.append(key)
    return children, parent, roots


def children_of(prefix: str) -> list[str]:
    return _hierarchy()[0].get(prefix, [])


def parent_of(prefix: str) -> str:
    return _hierarchy()[1].get(prefix, "")


def roots() -> list[str]:
    return _hierarchy()[2]


def ancestors_of(prefix: str) -> list[str]:
    """Від найближчого батька до кореня."""
    out: list[str] = []
    cur = parent_of(prefix)
    while cur:
        out.append(cur)
        cur = parent_of(cur)
    return out


def label_for(prefix: str) -> str:
    """Підпис вузла дерева: «код — назва»."""
    code, name = by_prefix().get(prefix, ("", ""))
    if code:
        return f"{code} — {name}"
    return prefix
