"""Дрібні багаторазові віджети."""
from __future__ import annotations

import re

from PySide6.QtCore import QDate, QRegularExpression, Qt, Signal
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDateEdit, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

#: ЄДРПОУ юридичної особи — 8 цифр, РНОКПП фізичної — 10. Перевірка меж
#: не дає розрізати один довгий номер на два «коди».
EDRPOU_RE = re.compile(r"(?<!\d)\d{8,10}(?!\d)")

#: Скільки рядків списку ЄДРПОУ показуємо без прокрутки.
_MIN_ROWS = 2
_MAX_ROWS = 6
#: Висота рядка з відступами таблиці стилів. Qt повідомляє її без цих
#: відступів, і порахований по ній список виходить удвічі нижчим за
#: намальований — тому беремо більше з двох значень.
_ROW_HEIGHT = 42


def _as_qdate(value: str, fallback: QDate) -> QDate:
    parsed = QDate.fromString(str(value or "")[:10], "yyyy-MM-dd")
    return parsed if parsed.isValid() else fallback


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


class MoneyEdit(QLineEdit):
    """Поле для суми в гривнях. Порожнє означає «без обмеження».

    Свідомо не ``QDoubleSpinBox``: той при значенні-мінімумі показує
    підказковий текст («від»), і будь-яка натиснута цифра дає «від5», що
    валідатор відхиляє — поле стає незаповнюваним. Тут же приймаються
    цифри з будь-якими роздільниками, а показується акуратно згрупована сума.
    """

    def __init__(self, placeholder: str, parent=None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        # Приймаємо суму з будь-якими роздільниками: користувач може вставити
        # її просто з порталу, де тисячі розділені нерозривним пробілом,
        # якого шаблон \s у PCRE не покриває — звідси окремий \x{00a0}.
        self.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"[\d\s\x{00a0}.,']*")))
        self.editingFinished.connect(self._reformat)

    def value(self) -> float | None:
        digits = "".join(ch for ch in self.text() if ch.isdigit())
        return float(digits) if digits else None

    def set_value(self, value: float | None) -> None:
        self.setText(f"{value:,.0f}".replace(",", " ") if value else "")

    def _reformat(self) -> None:
        self.set_value(self.value())


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
        today = QDate.currentDate()
        self.date_from.setDate(_as_qdate(date_from, today.addDays(-365)))
        self.date_to.setDate(_as_qdate(date_to, today))

    def normalize(self) -> None:
        """Якщо межі переставлені місцями — міняє їх, щоб пошук не був порожнім."""
        if self.date_from.date() > self.date_to.date():
            first, second = self.date_to.date(), self.date_from.date()
            self.date_from.setDate(first)
            self.date_to.setDate(second)


class EdrpouList(QWidget):
    """Список кодів ЄДРПОУ з додаванням і видаленням."""

    changed = Signal()

    def __init__(self, placeholder: str = "ЄДРПОУ або кілька через кому",
                 labels: dict[str, str] | None = None, parent=None):
        super().__init__(parent)
        #: Відомі назви компаній: у списку показуємо «код — назва», щоб рядок
        #: не був німим числом. Назовні віддаємо самі коди.
        self.labels = dict(labels or {})
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
        self.list.setMaximumHeight(_MIN_ROWS * _ROW_HEIGHT)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        lay.addWidget(self.list)

        self.hint = QLabel()
        self.hint.setObjectName("Muted")
        self.hint.setVisible(False)
        lay.addWidget(self.hint)

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
        raw = self.input.text().strip()
        codes = EDRPOU_RE.findall(raw)
        if raw and not codes:
            # Мовчки з'їдений ввід — найгірше, що може зробити таке поле.
            self.hint.setText("Не схоже на код: ЄДРПОУ — 8 цифр, РНОКПП ФОП — 10.")
            self.hint.setVisible(True)
            return
        self.hint.setVisible(False)
        existing = set(self.values())
        for code in codes:
            if code not in existing:
                self._add_item(code)
                existing.add(code)
        self.input.clear()
        self.changed.emit()

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._fit()
        self.changed.emit()

    def clear(self) -> None:
        self.list.clear()
        self._fit()
        self.changed.emit()

    def _add_item(self, code: str) -> None:
        name = self.labels.get(code)
        item = QListWidgetItem(f"{code} — {name}" if name else code, self.list)
        item.setData(Qt.ItemDataRole.UserRole, code)
        self._fit()

    def showEvent(self, event) -> None:
        # Перший показ — єдиний момент, коли висота рядка вже враховує таблицю
        # стилів: у конструкторі її ще немає, і список виходив удвічі нижчим,
        # ніж потрібно.
        super().showEvent(event)
        self._fit()

    def _fit(self) -> None:
        """Висота списку — під його вміст.

        Фіксована висота або лишає пусте місце під двома кодами, або ховає
        четвертий рядок під прокруткою; і те, й інше дратує саме тоді, коли
        на список дивляться.
        """
        measured = self.list.sizeHintForRow(0) if self.list.count() else 0
        row = max(measured, _ROW_HEIGHT)
        rows = min(max(self.list.count(), _MIN_ROWS), _MAX_ROWS)
        self.list.setMaximumHeight(rows * row + 2 * self.list.frameWidth() + 4)

    def values(self) -> list[str]:
        items = (self.list.item(i) for i in range(self.list.count()))
        return [item.data(Qt.ItemDataRole.UserRole) or item.text() for item in items]

    def set_values(self, codes: list[str]) -> None:
        self.list.clear()
        for code in codes or []:
            self._add_item(str(code))
        self._fit()
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
