"""Універсальна таблиця для будь-якого аркуша звіту.

Аналітика складається з двох десятків різних таблиць, і писати під кожну свою
модель немає сенсу: на вхід іде ``(заголовки, рядки)``, а як показувати число —
таблиця вирішує за назвою колонки. «…, грн» — гроші з розрядами, «Частка» чи
«Дисконт» — відсотки, решта чисел — цілі з пробілом між тисячами.
"""
from __future__ import annotations

from typing import Any, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ...core.report import money, pct

#: Слова в назві колонки, за якими число показується відсотком.
PERCENT_WORDS = ("частка", "дисконт", "результативність", "розрив", "динаміка",
                 "повторні", "без торгів", "економія", "охоплення")
#: Скільки рядків узагалі віддаємо у віджет — далі таблиця стає некерованою,
#: а повний зріз завжди є у вивантаженні в Excel.
MAX_ROWS = 5000
#: Скільки рядків Qt дозволено переглянути, добираючи ширину колонок.
AUTOSIZE_ROWS = 120


def _is_percent(header: str) -> bool:
    low = str(header or "").lower()
    return any(word in low for word in PERCENT_WORDS)


def _is_money(header: str) -> bool:
    return "грн" in str(header or "").lower()


class SheetModel(QAbstractTableModel):
    def __init__(self, headers: Sequence[str] = (), rows: Sequence[Sequence[Any]] = ()):
        super().__init__()
        self.headers = list(headers)
        self.rows = [list(r) for r in rows]
        self._percent = [_is_percent(h) for h in self.headers]
        self._money = [_is_money(h) for h in self.headers]

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.headers[section] if section < len(self.headers) else ""
        return section + 1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        column = index.column()
        value = row[column] if column < len(row) else None
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(column, value)
        if role == Qt.ItemDataRole.UserRole:
            return value if value is not None else ""
        if role == Qt.ItemDataRole.TextAlignmentRole and isinstance(value, (int, float)) \
                and not isinstance(value, bool):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            text = self._display(column, value)
            return text if len(text) > 28 else None
        return None

    def _display(self, column: int, value: Any) -> str:
        if value is None or value == "":
            return ""
        if isinstance(value, bool):
            return "так" if value else ""
        if isinstance(value, (int, float)):
            if column < len(self._percent) and self._percent[column]:
                return pct(value)
            if column < len(self._money) and self._money[column]:
                # Копійки показуємо лише там, де вони є: у ціні за одиницю
                # 1 234,50 округлення до гривні спотворює порівняння, а в сумі
                # договору на 90 000 два нулі після коми — просто шум.
                return money(value, 0 if float(value).is_integer() else 2)
            if isinstance(value, float) and not value.is_integer():
                return money(value, 2)
            return money(value)
        return str(value)

    def set_sheet(self, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        self.beginResetModel()
        self.headers = list(headers)
        self.rows = [list(r) for r in rows[:MAX_ROWS]]
        self._percent = [_is_percent(h) for h in self.headers]
        self._money = [_is_money(h) for h in self.headers]
        self.endResetModel()


class DataTable(QWidget):
    """Таблиця з фільтром, сортуванням і копіюванням у буфер."""

    def __init__(self, headers: Sequence[str] = (), rows: Sequence[Sequence[Any]] = (),
                 searchable: bool = True, height: int = 300, parent=None):
        super().__init__(parent)
        self._all_rows = list(rows)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.model = SheetModel(headers, self._all_rows[:MAX_ROWS])
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        if searchable:
            tools = QHBoxLayout()
            tools.setSpacing(8)
            self.filter = QLineEdit()
            self.filter.setPlaceholderText("Фільтр за будь-яким полем…")
            self.filter.textChanged.connect(self._filter)
            tools.addWidget(self.filter, 1)
            copy = QPushButton("Копіювати")
            copy.setToolTip("Скопіювати видимі рядки у буфер обміну")
            copy.clicked.connect(self.copy_visible)
            tools.addWidget(copy)
            self.count = QLabel("")
            self.count.setObjectName("Muted")
            tools.addWidget(self.count)
            layout.addLayout(tools)
        else:
            self.filter = None
            self.count = None

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # Остання колонка не розтягується: у таблиці «Повнота даних» це
        # роздуло б «Частку» на пів екрана, лишивши назви полів обрізаними.
        self.view.horizontalHeader().setStretchLastSection(False)
        self.view.setMinimumHeight(height)
        layout.addWidget(self.view, 1)
        self._autosize()
        self._update_count()

    def _autosize(self) -> None:
        # Ширину колонок Qt рахує, заглядаючи в кожен рядок моделі; на таблиці
        # у кілька тисяч рядків це помітна пауза, а перших сотні рядків цілком
        # досить, щоб підібрати ширину.
        self.view.horizontalHeader().setResizeContentsPrecision(AUTOSIZE_ROWS)
        self.view.resizeColumnsToContents()
        for column in range(self.model.columnCount()):
            # Підказка Qt не враховує відступи клітинки з таблиці стилів,
            # тому додаємо їх самі — інакше текст обрізається трьома крапками.
            width = self.view.columnWidth(column)
            self.view.setColumnWidth(column, min(max(width + 26, 76), 360))

    def _filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text.strip())
        self._update_count()

    def _update_count(self) -> None:
        if not self.count:
            return
        shown = self.proxy.rowCount()
        total = len(self._all_rows)
        text = f"{shown} з {total}"
        if total > MAX_ROWS:
            text += f" (у таблиці перші {MAX_ROWS}, повний зріз — у вивантаженні)"
        self.count.setText(text)

    def copy_visible(self) -> None:
        lines = ["\t".join(self.model.headers)]
        for row in range(self.proxy.rowCount()):
            cells = []
            for column in range(self.proxy.columnCount()):
                index = self.proxy.index(row, column)
                cells.append(str(self.proxy.data(index, Qt.ItemDataRole.DisplayRole) or ""))
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))

    def set_sheet(self, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        self._all_rows = list(rows)
        self.model.set_sheet(headers, self._all_rows)
        self._autosize()
        self._update_count()
