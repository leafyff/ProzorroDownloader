"""Швидке потокове читання XLSX.

``openpyxl`` читає книгу через шар описових класів: кожна клітинка з текстом
перетворюється на об'єкт ``Text``, кожен рядок — на кортеж ``Cell``. На книзі з
двохсот тисяч рядків це коштує майже хвилину, і майже весь той час іде не на
розбір XML, а на створення об'єктів, які нам не потрібні: аналітиці досить
готових значень.

Тут XLSX читається напряму — zip + потоковий розбір XML. Формат простий:

* ``xl/workbook.xml`` — перелік аркушів і їхні ``r:id``;
* ``xl/_rels/workbook.xml.rels`` — до якого файлу веде кожен ``r:id``;
* ``xl/sharedStrings.xml`` — спільна таблиця рядків (якщо є);
* ``xl/styles.xml`` — формати, з яких видно, котрі числа насправді дати;
* ``xl/worksheets/sheetN.xml`` — самі клітинки.

Модуль навмисно нічого не знає про наші таблиці: він віддає рядки значень, а
що з ними робити — справа :mod:`app.core.xlsxload`. Якщо книга виявиться
незвичною, виклик підніме виняток, і виклик згори просто повернеться до
``openpyxl``.
"""
from __future__ import annotations

import math
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from xml.etree import ElementTree as ET

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
RELS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_ROW = MAIN + "row"
_CELL = MAIN + "c"
_VALUE = MAIN + "v"
_TEXT = MAIN + "t"

#: Вбудовані формати дати й часу за ECMA-376.
BUILTIN_DATE_FORMATS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47})

#: Excel рахує дні від 1900-01-01, але помилково вважає 1900 рік високосним,
#: тому нульовою точкою для перерахунку беруть 1899-12-30.
EPOCH = datetime(1899, 12, 30)

#: Скільки байтів аркуша подаємо розбирачеві за раз.
CHUNK = 1 << 20

_DIGITS = "0123456789"


def _column(ref: str) -> int:
    """``'AB12'`` → ``27`` (номер колонки з нуля)."""
    index = 0
    for char in ref:
        position = ord(char) - 64          # 'A' → 1
        if not 1 <= position <= 26:
            break
        index = index * 26 + position
    return index - 1


def _from_serial(serial: float) -> datetime | float:
    """Порядковий номер Excel → дата з часом.

    Дробову частину переводимо в цілі мілісекунди, а не віддаємо
    ``timedelta(days=...)`` як є: 0,572916666… доби — це 13:45:00, але у
    подвійній точності воно виходить 13:44:59,999999, і час у книзі читався б
    на секунду раніше, ніж його бачить Excel.
    """
    try:
        days = math.floor(serial)
        millis = round((serial - days) * 86_400_000)
        return EPOCH + timedelta(days=days, milliseconds=millis)
    except (OverflowError, ValueError):
        return serial


def _is_date_format(code: str) -> bool:
    """Чи описує рядок формату дату.

    Літери дати можуть ховатися в лапках («"грн"») і в дужках ([$-uk-UA]),
    тому такі шматки спершу прибираємо, інакше будь-яка сума в гривнях
    зійшла б за дату через літеру «н»… точніше, через латинські ``d``/``y``
    у назвах валют.
    """
    cleaned: list[str] = []
    skip = False
    quote = ""
    for char in code or "":
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "\"'":
            quote = char
            continue
        if char == "[":
            skip = True
            continue
        if char == "]":
            skip = False
            continue
        if not skip:
            cleaned.append(char)
    plain = "".join(cleaned).lower()
    return any(letter in plain for letter in "ydh") or "mm" in plain


class _SheetTarget:
    """Ціль розбору аркуша: збирає рядки значень, не будуючи дерева.

    ``ElementTree`` з ``iterparse`` створює об'єкт на кожен елемент і проганяє
    кожен через чергу подій — на двох мільйонах клітинок це десятки секунд
    самої лише службової роботи. Ціль розбору дозволяє прискорювачеві
    викликати три прості методи й не створювати нічого зайвого.
    """

    __slots__ = ("ready", "keep", "_strings", "_dates", "_pool", "_cells",
                 "_capture", "_buffer", "_kind", "_style", "_index", "_next",
                 "_columns", "_wanted")

    def __init__(self, strings: list[str], date_styles: frozenset[int],
                 pool: dict[str, str]):
        self.ready: list[list[Any]] = []
        #: Номери потрібних колонок; ``None`` — брати всі.
        self.keep: set[int] | None = None
        self._wanted = True
        self._strings = strings
        self._dates = date_styles
        self._pool = pool
        self._cells: list[Any] | None = None
        self._capture = False
        self._buffer: list[str] = []
        self._kind = None
        self._style = None
        self._index = 0
        self._next = 0
        self._columns: dict[str, int] = {}

    def start(self, tag: str, attrib: dict) -> None:
        if tag == _CELL:
            reference = attrib.get("r")
            if reference:
                # «AB12» → «AB» → 27. Літерна частина повторюється в кожному
                # рядку, тому номер колонки рахуємо один раз на колонку, а не
                # на кожну з двох мільйонів клітинок.
                letters = reference.rstrip(_DIGITS)
                index = self._columns.get(letters)
                if index is None:
                    index = _column(letters)
                    self._columns[letters] = index
                self._index = index if index >= 0 else self._next
            else:
                self._index = self._next
            keep = self.keep
            self._wanted = keep is None or self._index in keep
            self._kind = attrib.get("t")
            self._style = attrib.get("s")
            self._buffer.clear()
        elif tag == _VALUE or tag == _TEXT:
            self._capture = True
        elif tag == _ROW:
            self._cells = []
            self._next = 0

    def data(self, text: str) -> None:
        if self._capture and self._wanted:
            self._buffer.append(text)

    def end(self, tag: str) -> None:
        if tag == _CELL:
            cells = self._cells
            if cells is None:
                return
            index = self._index
            if index >= len(cells):
                cells.extend([None] * (index - len(cells) + 1))
            if self._wanted:
                cells[index] = self._value()
            self._next = index + 1
        elif tag == _VALUE or tag == _TEXT:
            self._capture = False
        elif tag == _ROW:
            if self._cells is not None:
                self.ready.append(self._cells)
            self._cells = None

    def _value(self) -> Any:
        buffer = self._buffer
        if not buffer:
            return None
        raw = buffer[0] if len(buffer) == 1 else "".join(buffer)
        kind = self._kind
        if kind == "inlineStr" or kind == "str":
            return self._pool.setdefault(raw, raw)
        if kind == "s":
            try:
                return self._strings[int(raw)]
            except (ValueError, IndexError):
                return ""
        if kind == "b":
            return raw not in ("0", "", "false", "FALSE")
        if kind == "e":
            return self._pool.setdefault(raw, raw)
        try:
            number = float(raw)
        except ValueError:
            return self._pool.setdefault(raw, raw)
        # Стиль дивимось лише тоді, коли в книзі взагалі є формати дат:
        # у наших вивантаженнях їх немає, а числових клітинок — мільйони.
        style = self._style
        if self._dates and style is not None and int(style) in self._dates:
            return _from_serial(number)
        return int(number) if "." not in raw and number.is_integer() else number

    def close(self) -> None:
        return None


class FastWorkbook:
    """Книга, відкрита для потокового читання значень."""

    def __init__(self, path: Path | str):
        self.zip = zipfile.ZipFile(str(path))
        try:
            self._parts = self._sheet_parts()
            self.names = list(self._parts)
            self._strings = self._shared_strings()
            self._date_styles = self._date_styles()
        except Exception:
            self.zip.close()
            raise
        #: Однакові рядки в різних клітинках мають бути одним об'єктом —
        #: інакше книга на двісті тисяч рядків з'їдає сотні мегабайт на копіях.
        self._pool: dict[str, str] = {}

    # --- службові частини книги ------------------------------------------

    def _sheet_parts(self) -> dict[str, str]:
        book = ET.fromstring(self.zip.read("xl/workbook.xml"))
        targets: dict[str, str] = {}
        for relation in ET.fromstring(self.zip.read("xl/_rels/workbook.xml.rels")):
            target = relation.get("Target") or ""
            if target.startswith("/"):
                target = target[1:]
            elif not target.startswith("xl/"):
                target = "xl/" + target.lstrip("./")
            targets[relation.get("Id") or ""] = target

        parts: dict[str, str] = {}
        for sheet in book.iter(MAIN + "sheet"):
            name = sheet.get("name") or ""
            part = targets.get(sheet.get(RELS + "id") or "")
            if name and part:
                parts[name] = part
        if not parts:
            raise ValueError("у книзі не знайдено жодного аркуша")
        return parts

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.zip.namelist():
            return []
        strings: list[str] = []
        with self.zip.open("xl/sharedStrings.xml") as handle:
            for event, element in ET.iterparse(handle, events=("end",)):
                if element.tag == MAIN + "si":
                    strings.append("".join(node.text or "" for node in element.iter(_TEXT)))
                    element.clear()
        return strings

    def _date_styles(self) -> frozenset[int]:
        if "xl/styles.xml" not in self.zip.namelist():
            return frozenset()
        root = ET.fromstring(self.zip.read("xl/styles.xml"))
        custom = {int(fmt.get("numFmtId") or 0): fmt.get("formatCode") or ""
                  for fmt in root.iter(MAIN + "numFmt")}
        formats = root.find(MAIN + "cellXfs")
        if formats is None:
            return frozenset()
        dates: set[int] = set()
        for index, style in enumerate(formats):
            number_format = int(style.get("numFmtId") or 0)
            if number_format in BUILTIN_DATE_FORMATS or (
                    number_format in custom and _is_date_format(custom[number_format])):
                dates.add(index)
        return frozenset(dates)

    # --- читання ----------------------------------------------------------

    def rows(self, name: str,
             on_header: Callable[[list[Any]], set[int] | None] | None = None
             ) -> Iterator[list[Any]]:
        """Рядки аркуша як списки значень; порожні клітинки — ``None``.

        Аркуш подається розбирачеві частинами, а готові рядки забираються
        після кожної: так пам'ять не залежить від розміру книги.

        ``on_header`` отримує перший рядок і може повернути номери колонок,
        які насправді потрібні. Решту клітинок розбирач далі пропускає — на
        реєстрі документів це майже мільйон значень, які нікому не потрібні.
        """
        target = _SheetTarget(self._strings, self._date_styles, self._pool)
        parser = ET.XMLParser(target=target)
        ready = target.ready
        seen_header = on_header is None
        with self.zip.open(self._parts[name]) as handle:
            while True:
                chunk = handle.read(CHUNK)
                if not chunk:
                    break
                parser.feed(chunk)
                if ready:
                    if not seen_header:
                        seen_header = True
                        target.keep = on_header(ready[0])
                    yield from ready
                    ready.clear()
        parser.close()
        if ready:
            if not seen_header:
                target.keep = on_header(ready[0])
            yield from ready
            ready.clear()

    def close(self) -> None:
        self.zip.close()
        self._pool.clear()

    def __enter__(self) -> "FastWorkbook":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
