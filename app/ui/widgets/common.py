"""Дрібні багаторазові віджети."""
from __future__ import annotations

import re

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDateEdit, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

EDRPOU_RE = re.compile(r"\d{6,10}")


def wrapped_label(text: str, object_name: str = "Muted") -> QLabel:
    """Багаторядковий підпис, який не заважає панелі звужуватися.

    Звичайний ``QLabel`` із ``wordWrap`` повідомляє мінімальну ширину під цілий
    рядок тексту, через що вся колонка перестає стискатися. Політика ``Ignored``
    по горизонталі знімає це обмеження, лишаючи перенесення слів.
    """
    label = QLabel(text)
    label.setObjectName(object_name)
    label.setWordWrap(True)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
    label.setMinimumWidth(80)
    return label


class Card(QFrame):
    """Панель із заголовком і власним вертикальним компонуванням."""

    def __init__(self, title: str = "", hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)
        if title:
            head = QLabel(title)
            head.setObjectName("SectionTitle")
            outer.addWidget(head)
        if hint:
            outer.addWidget(wrapped_label(hint))
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)

    def add(self, widget) -> None:
        if isinstance(widget, QWidget):
            self.body.addWidget(widget)
        else:
            self.body.addLayout(widget)


class StatTile(QFrame):
    """Плитка «значення + підпис» для панелі показників."""

    def __init__(self, label: str, value: str = "—", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        self.caption = QLabel(label)
        self.caption.setObjectName("StatLabel")
        lay.addWidget(self.value)
        lay.addWidget(self.caption)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, value: str) -> None:
        self.value.setText(value)


class DateRange(QWidget):
    """Період «з … по» з кнопками швидкого вибору."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.date_from = QDateEdit(calendarPopup=True)
        self.date_to = QDateEdit(calendarPopup=True)
        for widget in (self.date_from, self.date_to):
            widget.setDisplayFormat("dd.MM.yyyy")
            widget.dateChanged.connect(lambda _: self.changed.emit())
        row.addWidget(QLabel("з"))
        row.addWidget(self.date_from, 1)
        row.addWidget(QLabel("по"))
        row.addWidget(self.date_to, 1)
        lay.addLayout(row)

        quick = QHBoxLayout()
        quick.setSpacing(4)
        presets = [
            ("12 місяців", -365), ("6 місяців", -182), ("3 місяці", -91),
            ("Цей рік", "year"), ("Минулий рік", "prev_year"),
        ]
        for title, span in presets:
            btn = QPushButton(title)
            btn.setObjectName("Link")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, s=span: self._apply(s))
            quick.addWidget(btn)
        quick.addStretch(1)
        lay.addLayout(quick)

    def _apply(self, span) -> None:
        today = QDate.currentDate()
        if span == "year":
            self.date_from.setDate(QDate(today.year(), 1, 1))
            self.date_to.setDate(today)
        elif span == "prev_year":
            self.date_from.setDate(QDate(today.year() - 1, 1, 1))
            self.date_to.setDate(QDate(today.year() - 1, 12, 31))
        else:
            self.date_from.setDate(today.addDays(span))
            self.date_to.setDate(today)

    def values(self) -> tuple[str, str]:
        return (self.date_from.date().toString("yyyy-MM-dd"),
                self.date_to.date().toString("yyyy-MM-dd"))

    def set_values(self, date_from: str, date_to: str) -> None:
        self.date_from.setDate(QDate.fromString(date_from[:10], "yyyy-MM-dd"))
        self.date_to.setDate(QDate.fromString(date_to[:10], "yyyy-MM-dd"))


class EdrpouList(QWidget):
    """Список кодів ЄДРПОУ з додаванням і видаленням."""

    changed = Signal()

    def __init__(self, placeholder: str = "ЄДРПОУ або кілька через кому", parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.input.returnPressed.connect(self._add)
        row.addWidget(self.input, 1)
        add = QPushButton("Додати")
        add.clicked.connect(self._add)
        row.addWidget(add)
        lay.addLayout(row)

        self.list = QListWidget()
        self.list.setMaximumHeight(112)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.list)

        tools = QHBoxLayout()
        remove = QPushButton("Прибрати обране")
        remove.clicked.connect(self._remove)
        clear = QPushButton("Очистити")
        clear.clicked.connect(self.clear)
        tools.addWidget(remove)
        tools.addWidget(clear)
        tools.addStretch(1)
        lay.addLayout(tools)

    def _add(self) -> None:
        codes = EDRPOU_RE.findall(self.input.text())
        existing = set(self.values())
        for code in codes:
            if code not in existing:
                QListWidgetItem(code, self.list)
                existing.add(code)
        self.input.clear()
        self.changed.emit()

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.changed.emit()

    def clear(self) -> None:
        self.list.clear()
        self.changed.emit()

    def values(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_values(self, codes: list[str]) -> None:
        self.list.clear()
        for code in codes or []:
            QListWidgetItem(str(code), self.list)
        self.changed.emit()


class CheckGrid(QWidget):
    """Сітка прапорців за словником ``{значення: підпис}``."""

    changed = Signal()

    def __init__(self, options: dict[str, str], columns: int = 2,
                 hints: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        self.boxes: dict[str, QCheckBox] = {}
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(6)
        for i, (value, label) in enumerate(options.items()):
            box = QCheckBox(label)
            if hints and hints.get(value):
                box.setToolTip(hints[value])
            box.stateChanged.connect(lambda _: self.changed.emit())
            self.boxes[value] = box
            grid.addWidget(box, i // columns, i % columns)

    def values(self) -> list[str]:
        return [value for value, box in self.boxes.items() if box.isChecked()]

    def set_values(self, values: list[str]) -> None:
        wanted = set(values or [])
        for value, box in self.boxes.items():
            box.blockSignals(True)
            box.setChecked(value in wanted)
            box.blockSignals(False)
        self.changed.emit()
