"""Сторінка «Файли»: перелік знайдених і завантажених документів."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ...core.db import Database
from ...core.extract import SCOPE_LABELS
from ..widgets.common import wrapped_label

STATE_LABELS = {
    "ok": "Завантажено",
    "pending": "У черзі",
    "filtered": "Відсіяно фільтром",
    "skipped": "Пропущено",
    "error": "Помилка",
}

COLUMNS = [
    ("tender_id", "Закупівля", 175),
    ("scope", "Розділ", 110),
    ("owner_name", "Чий файл", 220),
    ("title", "Назва файлу", 300),
    ("doc_type", "Тип документа", 150),
    ("size", "Розмір", 90),
    ("state", "Стан", 110),
    ("local_path", "Шлях на диску", 380),
]

#: Перелік документів може налічувати сотні тисяч рядків — у таблицю
#: беремо лише верхівку, а підсумки рахуємо запитом по всій базі.
MAX_ROWS = 20000

SQL = """
SELECT d.tender_id, d.scope, d.owner_name, d.title, d.doc_type, d.size, d.state,
       d.local_path, d.error, d.url
FROM documents d
{where}
ORDER BY d.tender_id DESC, d.scope, d.title
LIMIT ?
"""

TOTALS_SQL = """
SELECT COUNT(*) AS n,
       SUM(state = 'ok') AS ok,
       SUM(state = 'error') AS failed,
       SUM(state = 'filtered') AS filtered,
       COALESCE(SUM(size), 0) AS bytes
FROM documents {where}
"""


class DocumentModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section][1]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        key = COLUMNS[index.column()][0]
        value = row.get(key)
        if role == Qt.ItemDataRole.DisplayRole:
            if key == "scope":
                return SCOPE_LABELS.get(str(value), str(value or ""))
            if key == "state":
                return STATE_LABELS.get(str(value), str(value or ""))
            if key == "size":
                return _human(value)
            return str(value or "")
        if role == Qt.ItemDataRole.UserRole:
            return value if value is not None else ""
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.get("error") or str(value or "")
        return None

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


def _human(size) -> str:
    size = int(size or 0)
    if size <= 0:
        return ""
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


class FilesPage(QWidget):
    retry_failed = Signal()

    def __init__(self, db: Database, get_output_dir, parent=None):
        super().__init__(parent)
        self.db = db
        self.get_output_dir = get_output_dir

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        title = QLabel("Файли")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        root.addWidget(wrapped_label(
            "Усі документи, знайдені в картках закупівель: тендерна документація, "
            "пропозиції учасників, протоколи та договори. Подвійний клац відкриває файл.",
            "PageHint"))

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фільтр за назвою файлу, закупівлею або учасником…")
        self.filter.textChanged.connect(lambda t: self.proxy.setFilterFixedString(t.strip()))
        tools.addWidget(self.filter, 1)

        self.state_filter = QComboBox()
        self.state_filter.addItem("Усі стани", "")
        for value, label in STATE_LABELS.items():
            self.state_filter.addItem(label, value)
        self.state_filter.currentIndexChanged.connect(self.reload)
        tools.addWidget(self.state_filter)

        refresh = QPushButton("Оновити")
        refresh.clicked.connect(self.reload)
        tools.addWidget(refresh)
        self.btn_retry = QPushButton("Повторити невдалі")
        self.btn_retry.setToolTip("Завантажити ще раз лише ті файли, які не вдалося взяти")
        self.btn_retry.clicked.connect(self.retry_failed.emit)
        tools.addWidget(self.btn_retry)
        open_dir = QPushButton("Відкрити теку")
        open_dir.setObjectName("Primary")
        open_dir.clicked.connect(self.open_output_dir)
        tools.addWidget(open_dir)
        root.addLayout(tools)

        self.model = DocumentModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self._open_selected)
        for i, (_k, _l, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, width)
        root.addWidget(self.table, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        root.addWidget(self.summary)

    # --- дані -------------------------------------------------------------

    def reload(self) -> None:
        state = self.state_filter.currentData()
        params = [state] if state else []

        rows = [dict(r) for r in self.db.query(
            SQL.format(where="WHERE d.state = ?" if state else ""), params + [MAX_ROWS])]
        self.model.set_rows(rows)

        totals = self.db.query(
            TOTALS_SQL.format(where="WHERE state = ?" if state else ""), params)[0]
        summary = (f"Документів: {totals['n']:,}  ·  завантажено {totals['ok'] or 0:,}"
                   f"  ·  відсіяно {totals['filtered'] or 0:,}"
                   f"  ·  з помилками {totals['failed'] or 0:,}"
                   f"  ·  загальний обсяг {_human(totals['bytes']) or '0 Б'}")
        if len(rows) >= MAX_ROWS:
            summary += f"  ·  у таблиці перші {MAX_ROWS:,}"
        self.summary.setText(summary.replace(",", " "))

    # --- дії --------------------------------------------------------------

    def _open_selected(self, index) -> None:
        source = self.proxy.mapToSource(index)
        row = self.model.rows[source.row()]
        path = row.get("local_path")
        if not path or not os.path.exists(path):
            QMessageBox.information(self, "Файл недоступний",
                                    "Файл ще не завантажено або його переміщено.")
            return
        _open_in_explorer(Path(path))

    def open_output_dir(self) -> None:
        path = Path(self.get_output_dir())
        path.mkdir(parents=True, exist_ok=True)
        _open_in_explorer(path)


def _open_in_explorer(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))                      # noqa: S606 — штатний спосіб у Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass
