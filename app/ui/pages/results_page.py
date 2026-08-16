"""Сторінка «Результати»: таблиця знайдених закупівель і вивантаження у Excel."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ...config import METHOD_LABELS, STATUS_LABELS
from ...core.analytics import build_sheets as build_summary_sheets
from ...core.db import Database
from ...core.exporter import write_xlsx
from ...core.rawexport import build_sheets as build_raw_sheets

COLUMNS = [
    ("tender_id", "Номер закупівлі", 175),
    ("date_created", "Дата", 92),
    ("title", "Предмет закупівлі", 300),
    ("cpv_list", "ДК021", 130),
    ("pe_name", "Замовник (організатор)", 250),
    ("pe_region", "Регіон", 140),
    ("value_amount", "Очікувана вартість", 130),
    ("suppliers", "Переможець / постачальник", 230),
    ("contract_sum", "Сума договорів", 120),
    ("n_bids", "Учасн.", 62),
    ("status", "Статус", 130),
    ("method_type", "Процедура", 150),
    ("n_docs", "Файлів", 62),
]

#: Скільки рядків тримаємо у таблиці — далі вона стає некерованою,
#: а для повного зрізу є вивантаження в Excel.
MAX_ROWS = 20000

# Договори згортаються один раз підзапитом і приєднуються, а не рахуються
# корельовано для кожного рядка — на десятках тисяч закупівель це різниця
# між миттєвим оновленням і кількома секундами очікування.
SQL = """
SELECT t.uuid, t.tender_id, t.date_created, t.title, t.cpv_list, t.pe_name, t.pe_region,
       t.value_amount, t.status, t.method_type, t.n_bids, t.n_docs,
       c.suppliers, c.contract_sum
FROM tenders t
LEFT JOIN (
    SELECT tender_uuid,
           GROUP_CONCAT(DISTINCT supplier_name) AS suppliers,
           SUM(CASE WHEN status IN ('active', 'terminated') THEN value_amount END) AS contract_sum
    FROM contracts
    GROUP BY tender_uuid
) c ON c.tender_uuid = t.uuid
ORDER BY t.date_created DESC
LIMIT ?
"""


class TenderModel(QAbstractTableModel):
    def __init__(self, rows: list[dict] | None = None):
        super().__init__()
        self.rows: list[dict] = rows or []

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
            return _display(key, value)
        if role == Qt.ItemDataRole.UserRole:                 # для сортування
            return value if value is not None else ""
        if role == Qt.ItemDataRole.TextAlignmentRole and key in (
                "value_amount", "contract_sum", "n_bids", "n_docs"):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            return _display(key, value)
        return None

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


def _display(key: str, value) -> str:
    if value is None or value == "":
        return ""
    if key == "date_created":
        return str(value)[:10]
    if key in ("value_amount", "contract_sum"):
        return f"{float(value):,.0f}".replace(",", " ")
    if key == "status":
        return STATUS_LABELS.get(str(value), str(value))
    if key == "method_type":
        return METHOD_LABELS.get(str(value), str(value))
    if key == "cpv_list":
        codes = [c for c in str(value).split(",") if c]
        return ", ".join(codes[:3]) + (f" +{len(codes) - 3}" if len(codes) > 3 else "")
    return str(value)


class ResultsPage(QWidget):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._pending: list[dict] = []
        self._truncated = False
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(400)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self._flush_pending)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        title = QLabel("Результати")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        self.hint = QLabel("Тут накопичуються всі завантажені закупівлі. "
                           "Таблицю можна сортувати, фільтрувати й вивантажити в Excel.")
        self.hint.setObjectName("PageHint")
        root.addWidget(self.hint)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Фільтр за будь-яким полем: назва, ЄДРПОУ, переможець, номер…")
        self.filter.textChanged.connect(self._apply_filter)
        tools.addWidget(self.filter, 1)
        refresh = QPushButton("Оновити")
        refresh.clicked.connect(self.reload)
        tools.addWidget(refresh)
        summary_btn = QPushButton("Зведення")
        summary_btn.setToolTip("Підсумкові зрізи: рейтинги постачальників, замовників, "
                               "галузей і динаміка по місяцях")
        summary_btn.clicked.connect(self.export_summary)
        tools.addWidget(summary_btn)

        export = QPushButton("Вивантажити всі дані")
        export.setObjectName("Primary")
        export.setToolTip("Один файл із сирими даними: закупівлі, лоти, номенклатура, "
                          "пропозиції, договори, документи та картки товарів каталогу")
        export.clicked.connect(self.export)
        tools.addWidget(export)
        root.addLayout(tools)

        self.model = TenderModel()
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
        for i, (_key, _label, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, width)
        root.addWidget(self.table, 1)

        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        root.addWidget(self.summary)

    # --- дані -------------------------------------------------------------

    def reload(self) -> None:
        self._pending.clear()
        rows = [dict(r) for r in self.db.query(SQL, (MAX_ROWS,))]
        self.model.set_rows(rows)
        self._truncated = len(rows) >= MAX_ROWS
        self._update_summary()

    def add_row(self, row: dict) -> None:
        """Рядок із конвеєра. Вставляється пачками, щоб не смикати таблицю."""
        self._pending.append(dict(row))
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        self.model.beginInsertRows(QModelIndex(), 0, len(batch) - 1)
        self.model.rows[:0] = reversed(batch)
        self.model.endInsertRows()
        self._update_summary()

    def _apply_filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text.strip())
        self._update_summary()

    def _update_summary(self) -> None:
        shown = self.proxy.rowCount()
        total = self.model.rowCount()
        amount = signed = 0.0
        for row in self.model.rows:
            amount += float(row.get("value_amount") or 0)
            signed += float(row.get("contract_sum") or 0)
        text = (f"Показано {shown:,} з {total:,} закупівель  ·  очікувана вартість "
                f"{amount:,.0f} грн  ·  сума договорів {signed:,.0f} грн")
        if self._truncated:
            text += f"  ·  показано перші {MAX_ROWS:,} — повний зріз у вивантаженні"
        self.summary.setText(text.replace(",", " "))

    # --- вивантаження -----------------------------------------------------

    def export(self) -> None:
        """Повне вивантаження сирих даних — без підсумків і рейтингів."""
        self._save(build_raw_sheets, "prozorro-дані", "Усі дані")

    def export_summary(self) -> None:
        """Підсумкові зрізи — окремо, якщо потрібні готові рейтинги."""
        self._save(build_summary_sheets, "prozorro-зведення", "Зведення")

    def _save(self, builder, stem: str, title: str) -> None:
        if not self.model.rows:
            QMessageBox.information(self, "Немає даних", "Спочатку зберіть дані закупівель.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, f"Зберегти: {title}",
            str(Path.home() / f"{stem}-{date.today():%Y-%m-%d}.xlsx"),
            "Книга Excel (*.xlsx)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            sheets = builder(self.db)
            write_xlsx(Path(path), sheets)
        except Exception as exc:
            QMessageBox.critical(self, "Помилка", f"Не вдалося зберегти файл:\n{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()
        listing = "\n".join(f"  · {name} — {len(rows):,} рядків".replace(",", " ")
                            for name, (_h, rows) in sheets.items())
        QMessageBox.information(self, "Готово", f"Збережено:\n{path}\n\n{listing}")


def _export_value(key: str, value):
    if key in ("value_amount", "contract_sum"):
        return float(value) if value not in (None, "") else None
    if key in ("n_bids", "n_docs"):
        return int(value or 0)
    return _display(key, value)
