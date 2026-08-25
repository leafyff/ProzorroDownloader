"""Сторінка «Аналітика»: розбір вивантаженої книги Excel.

Аналіз навмисно відв'язаний від бази: на вхід іде готовий файл із теки
завантажень, тому можна порівнювати періоди, брати вивантаження з іншої
машини й перераховувати звіт, нічого не завантажуючи з мережі.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
)

from ...config import Settings
from ...core.exporter import write_xlsx
from ...core.report import Block, ChartData, Profile, Report
from ...core.xlsxload import find_workbooks
from ...paths import export_path
from ..widgets.charts import ChartBase, build as build_chart
from ..widgets.common import Card, StatTile, wrapped_label
from ..widgets.table import DataTable
from ..workers import AnalysisWorker

#: Графіки, яким потрібна вся ширина: у них або багато рядків, або довга вісь.
WIDE_KINDS = ("line", "area", "scatter")
#: Скільки рядків у смуговому графіку ще вміщається в половину ширини.
WIDE_ROWS = 9
#: Розділи, які показуємо зі списком гравців збоку.
PEOPLE_SECTIONS = {"Конкуренти": "competitors", "Наші ТОВ": "ours"}


class LazyPages(QStackedWidget):
    """Стос сторінок, кожна з яких збирається під час першого показу.

    Портретів конкурентів у звіті два десятки, і в кожному свої графіки й
    таблиці. Одразу побудувати їх усі — це кілька секунд замороженого вікна
    заради сторінок, більшість яких ніхто не відкриє.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._builders: dict[int, Callable[[], QWidget]] = {}
        self.currentChanged.connect(self._fill)

    def add(self, builder: Callable[[], QWidget]) -> int:
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        index = self.addWidget(holder)
        self._builders[index] = builder
        if self.currentIndex() == index:      # перша сторінка вже видима
            self._fill(index)
        return index

    def _fill(self, index: int) -> None:
        builder = self._builders.pop(index, None)
        if builder is None:
            return
        holder = self.widget(index)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            holder.layout().addWidget(builder())
        finally:
            QApplication.restoreOverrideCursor()


class AnalyticsPage(QWidget):
    """Вибір файлу, запуск аналізу та показ готового звіту."""

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.report: Report | None = None
        self.worker: AnalysisWorker | None = None
        self._picked: Path | None = None
        self._tab_builders: dict[int, Callable[[], QWidget]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        title = QLabel("Аналітика")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        hint = QLabel("Оберіть книгу з теки завантажень — програма очистить дані, "
                      "порахує ринок, розбере конкурентів і порівняє їх із вашими ТОВ.")
        hint.setObjectName("PageHint")
        root.addWidget(hint)

        root.addWidget(self._build_source_card())

        self.progress = QProgressBar()
        self.progress.setFormat("Готово до аналізу")
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.stack = QStackedWidget()
        self.placeholder = self._build_placeholder()
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._build_tab)
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.tabs)
        root.addWidget(self.stack, 1)

        bottom = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("Muted")
        bottom.addWidget(self.summary, 1)
        self.btn_export = QPushButton("Зберегти звіт у Excel")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self.export)
        bottom.addWidget(self.btn_export)
        root.addLayout(bottom)

        self.reload_files()

    # --- побудова верхньої частини ---------------------------------------

    def _build_source_card(self) -> Card:
        card = Card("Джерело даних",
                    "Підходить будь-яке вивантаження «Усі дані» з теки завантажень. "
                    "Що більше в ньому аркушів (номенклатура, пропозиції, документи), "
                    "то глибший вийде розбір.")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.files = QComboBox()
        self.files.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.files, 1)
        browse = QPushButton("Огляд…")
        browse.clicked.connect(self.pick_file)
        row.addWidget(browse)
        refresh = QPushButton("Оновити список")
        refresh.clicked.connect(self.reload_files)
        row.addWidget(refresh)
        card.add(row)

        options = QHBoxLayout()
        options.setSpacing(14)
        self.drop_outliers = QCheckBox("Виключати статистичні викиди з підсумків")
        self.drop_outliers.setChecked(True)
        self.drop_outliers.setToolTip(
            "Структурний брак (не гривня, нуль, дублікат) вилучається завжди.\n"
            "Ця галочка керує лише статистичними викидами — сумами, які надто\n"
            "далеко відхиляються від медіани своєї галузі.")
        options.addWidget(self.drop_outliers)
        options.addWidget(QLabel("Конкурентів у розборі:"))
        self.top_competitors = QSpinBox()
        self.top_competitors.setRange(3, 60)
        self.top_competitors.setValue(20)
        options.addWidget(self.top_competitors)
        options.addStretch(1)
        self.btn_run = QPushButton("Аналізувати")
        self.btn_run.setObjectName("Primary")
        self.btn_run.clicked.connect(self.run)
        options.addWidget(self.btn_run)
        card.add(options)
        return card

    def _build_placeholder(self) -> QWidget:
        holder = QFrame()
        holder.setObjectName("Card")
        lay = QVBoxLayout(holder)
        lay.addStretch(1)
        label = QLabel("Звіту ще немає.\nОберіть файл угорі та натисніть «Аналізувати».")
        label.setObjectName("Muted")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(label)
        lay.addStretch(1)
        return holder

    # --- файли ------------------------------------------------------------

    def reload_files(self) -> None:
        current = self.current_path()
        self.files.clear()
        found = find_workbooks(self.s.output_dir)
        if self._picked and self._picked not in found and self._picked.exists():
            found.insert(0, self._picked)
        for path in found:
            size = path.stat().st_size / 1048576
            self.files.addItem(f"{path.name}  ·  {size:.1f} МБ", str(path))
        if not found:
            self.files.addItem("У теці завантажень немає файлів .xlsx", "")
        if current:
            index = self.files.findData(str(current))
            if index >= 0:
                self.files.setCurrentIndex(index)

    def current_path(self) -> Path | None:
        data = self.files.currentData()
        return Path(data) if data else None

    def pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Оберіть книгу з даними", str(self.s.output_dir),
            "Книга Excel (*.xlsx)")
        if not path:
            return
        self._picked = Path(path)
        self.reload_files()
        index = self.files.findData(path)
        if index >= 0:
            self.files.setCurrentIndex(index)

    # --- запуск -----------------------------------------------------------

    def run(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        path = self.current_path()
        if not path or not path.exists():
            QMessageBox.information(self, "Немає файлу",
                                    "Оберіть книгу .xlsx із вивантаженими даними.")
            return
        self.btn_run.setEnabled(False)
        self.btn_export.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("Читаємо файл…")
        worker = AnalysisWorker(path, self.s, self.drop_outliers.isChecked(),
                                self.top_competitors.value(), self)
        worker.progress.connect(self._on_progress)
        worker.finished_job.connect(self._on_finished)
        self.worker = worker
        worker.start()

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.progress.setFormat(f"{stage} — %p%")

    def _on_finished(self, result) -> None:
        self.btn_run.setEnabled(True)
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setVisible(False)
        report, error = result if isinstance(result, tuple) else (None, "невідома помилка")
        if report is None and not error:            # зупинено користувачем
            return
        if error:
            QMessageBox.critical(self, "Не вдалося проаналізувати",
                                 f"Помилка розбору файлу:\n{error}")
            return
        self.report = report
        self.show_report(report)
        self.btn_export.setEnabled(True)

    # --- показ звіту ------------------------------------------------------

    def show_report(self, report: Report) -> None:
        # Звіт на десять тисяч закупівель — це понад сотня таблиць і графіків.
        # Будувати їх усі одразу означає підвісити вікно на кілька секунд, тож
        # вкладка збирається тоді, коли її вперше відкрили.
        # QTabWidget.clear() лише знімає вкладки, не видаляючи віджетів: без
        # явного видалення кожен повторний аналіз лишав би в пам'яті цілий
        # попередній звіт із усіма таблицями.
        for index in reversed(range(self.tabs.count())):
            widget = self.tabs.widget(index)
            self.tabs.removeTab(index)
            widget.deleteLater()
        self._tab_builders.clear()
        for name, blocks in report.sections.items():
            kind = PEOPLE_SECTIONS.get(name)
            people = (report.competitors if kind == "competitors"
                      else report.ours if kind else [])
            holder = QWidget()
            layout = QVBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            index = self.tabs.addTab(holder, name)
            if kind:
                self._tab_builders[index] = lambda b=blocks, p=people: self._people_tab(b, p)
            else:
                self._tab_builders[index] = (
                    lambda b=blocks: self._scroll(self._blocks_widget(b)))
        self._build_tab(self.tabs.currentIndex())
        self.stack.setCurrentWidget(self.tabs)
        source = Path(report.source).name
        self.summary.setText(
            f"{source}  ·  період {report.period[0] or '—'} — {report.period[1] or '—'}"
            f"  ·  звіт складено {report.generated}")

    def _build_tab(self, index: int) -> None:
        """Наповнює вкладку під час першого показу."""
        builder = self._tab_builders.pop(index, None)
        if builder is None:
            return
        holder = self.tabs.widget(index)
        layout = holder.layout() if holder else None
        if layout is None:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            layout.addWidget(builder())
        finally:
            QApplication.restoreOverrideCursor()

    def _scroll(self, widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(widget)
        return area

    def _blocks_widget(self, blocks: list[Block]) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(2, 2, 12, 2)
        lay.setSpacing(12)
        for block in blocks:
            for widget in self._render_block(block):
                lay.addWidget(widget)
        lay.addStretch(1)
        return holder

    def _render_block(self, block: Block) -> list[QWidget]:
        """Один блок звіту — це кілька карток: показники, висновки, графіки, таблиці."""
        widgets: list[QWidget] = []
        head = Card(block.title, block.hint)
        if block.tiles:
            head.add(self._tiles(block.tiles))
        for note in block.notes:
            head.add(wrapped_label("•  " + note))
        widgets.append(head)

        if block.charts:
            widgets.append(self._charts_card(block.charts))
        for name, sheet in block.tables:
            if not sheet or not sheet[1]:
                continue
            card = Card(name)
            card.add(DataTable(sheet[0], sheet[1],
                               height=340 if len(sheet[1]) > 6 else 160))
            widgets.append(card)
        return widgets

    def _tiles(self, tiles: list[tuple[str, str]]) -> QGridLayout:
        grid = QGridLayout()
        grid.setSpacing(8)
        columns = 4
        for i, (label, value) in enumerate(tiles):
            tile = StatTile(label, value)
            tile.value.setToolTip(f"{label}: {value}")
            grid.addWidget(tile, i // columns, i % columns)
        return grid

    def _charts_card(self, charts: list[ChartData]) -> QWidget:
        holder = QFrame()
        holder.setObjectName("Card")
        grid = QGridLayout(holder)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        row = column = 0
        for chart in charts:
            wide = chart.kind in WIDE_KINDS or (
                chart.kind == "hbar" and chart.series
                and len(chart.series[0].labels) > WIDE_ROWS)
            if wide and column == 1:
                row += 1
                column = 0
            grid.addWidget(self._chart_widget(chart), row, column, 1, 2 if wide else 1)
            if wide:
                row += 1
                column = 0
            else:
                column += 1
                if column > 1:
                    column = 0
                    row += 1
        return holder

    def _chart_widget(self, chart: ChartData) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        title = QLabel(chart.title)
        title.setObjectName("SectionTitle")
        lay.addWidget(title)
        if chart.hint:
            lay.addWidget(wrapped_label(chart.hint))
        lay.addWidget(build_chart(chart, self.s.theme), 1)
        return holder

    def _people_tab(self, blocks: list[Block], people: list[Profile]) -> QWidget:
        """Розділ зі списком гравців: огляд ліворуч, портрет — праворуч."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        listing = QListWidget()
        listing.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        listing.setMinimumWidth(230)
        listing.setMaximumWidth(340)

        pages = LazyPages()
        listing.addItem(QListWidgetItem("Огляд розділу"))
        pages.add(lambda: self._scroll(self._blocks_widget(blocks)))

        for profile in people:
            # Рядок списку двоповерховий, і Qt його не вкорочує сам —
            # обрізаємо назву вручну, повна лишається у підказці.
            label = profile.name or profile.edrpou
            if len(label) > 28:
                label = label[:27] + "…"
            item = QListWidgetItem(f"{label}\n{profile.edrpou}")
            item.setToolTip(profile.label)
            listing.addItem(item)
            pages.add(lambda p=profile: self._scroll(self._profile_widget(p)))

        listing.currentRowChanged.connect(pages.setCurrentIndex)
        listing.setCurrentRow(0)
        splitter.addWidget(listing)
        splitter.addWidget(pages)
        splitter.setStretchFactor(1, 1)
        return splitter

    def _profile_widget(self, profile: Profile) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(2, 2, 12, 2)
        lay.setSpacing(12)
        if profile.block:
            for widget in self._render_block(profile.block):
                lay.addWidget(widget)
        if profile.strengths or profile.weaknesses:
            card = Card("Сильні та слабкі сторони",
                        "Виведено з вимірюваних ознак: портфель ТМ, документи, ціна, "
                        "результативність, географія, стійкість бази замовників.")
            card.add(QLabel("Сильні сторони", objectName="SectionTitle"))
            for line in profile.strengths or ["не виявлено за наявними даними"]:
                card.add(wrapped_label("+  " + line))
            card.add(QLabel("Слабкі сторони", objectName="SectionTitle"))
            for line in profile.weaknesses or ["не виявлено за наявними даними"]:
                card.add(wrapped_label("−  " + line))
            lay.addWidget(card)
        lay.addStretch(1)
        return holder

    # --- інше -------------------------------------------------------------

    def apply_theme(self) -> None:
        for chart in self.findChildren(ChartBase):
            chart.set_theme(self.s.theme)

    def export(self) -> None:
        if not self.report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Зберегти звіт аналітики",
            str(export_path("prozorro-аналітика", folder=self.s.output_dir)),
            "Книга Excel (*.xlsx)")
        if not path:
            return
        sheets = self.report.sheets()
        if not sheets:
            QMessageBox.information(self, "Немає таблиць", "У звіті немає жодної таблиці.")
            return
        try:
            write_xlsx(Path(path), sheets)
        except Exception as exc:
            QMessageBox.critical(self, "Помилка", f"Не вдалося зберегти файл:\n{exc}")
            return
        listing = "\n".join(f"  · {name} — {len(rows):,} рядків".replace(",", " ")
                            for name, (_h, rows) in list(sheets.items())[:24])
        QMessageBox.information(self, "Готово",
                                f"Збережено:\n{path}\n\nАркушів: {len(sheets)}\n{listing}")

    def stop(self) -> None:
        """Просить робочий потік зупинитись і чекає на нього перед виходом."""
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(5000)
