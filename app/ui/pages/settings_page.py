"""Сторінка «Налаштування»: базові параметри, мережа, локальний індекс."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ...config import COMPETITOR_COMPANIES, OWN_COMPANIES, Settings
from ...core.db import Database
from ..widgets.common import Card, DateRange, EdrpouList, wrapped_label

RESOLVE_MODES = {
    "auto": "Автоматично — за розміром вибірки (рекомендовано)",
    "summary": "Опитувати портал поштучно — швидко для вузьких вибірок",
    "index": "Через повний індекс — вигідно для великих вибірок",
}


def _spaced(number) -> str:
    """``12345`` → ``12 345`` — тисячі пробілом, решта тексту недоторкана."""
    return f"{number:,}".replace(",", " ")


class SettingsPage(QWidget):
    build_index = Signal(str, str)      # дата з, дата по
    stop_index = Signal()
    settings_changed = Signal()

    def __init__(self, settings: Settings, db: Database, parent=None):
        super().__init__(parent)
        self.s = settings
        self.db = db

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(12)

        title = QLabel("Налаштування")
        title.setObjectName("PageTitle")
        outer.addWidget(title)

        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 10, 0)
        lay.setSpacing(12)

        lay.addWidget(self._card_storage())
        lay.addWidget(self._card_companies())
        lay.addWidget(self._card_index())
        lay.addWidget(self._card_network())
        lay.addWidget(self._card_look())
        lay.addStretch(1)
        area.setWidget(holder)
        outer.addWidget(area, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        reset = QPushButton("Скинути до типових")
        reset.clicked.connect(self._reset)
        row.addWidget(reset)
        save = QPushButton("Зберегти налаштування")
        save.setObjectName("Primary")
        save.clicked.connect(self.apply_and_save)
        row.addWidget(save)
        outer.addLayout(row)

        self.load()

    # --- картки -----------------------------------------------------------

    def _card_storage(self) -> Card:
        card = Card("Збереження файлів")
        row = QHBoxLayout()
        self.output_dir = QLineEdit()
        row.addWidget(self.output_dir, 1)
        browse = QPushButton("Обрати…")
        browse.clicked.connect(self._pick_dir)
        row.addWidget(browse)
        card.add(row)

        self.fresh_start = QCheckBox("Очищати зібране перед новим збором")
        self.fresh_start.setToolTip(
            "Без цього база стає архівом усіх колишніх пошуків, і аналітика"
            " рахує ринок по суміші вибірок: свіжі ІТ-закупівлі разом\n"
            "із залишками попереднього збору за іншим класом ДК021.\n\n"
            "Індекс tenderID→UUID очищення не зачіпає — він здобувається"
            " дорого й від предмета збору не залежить.")
        self.skip_existing = QCheckBox("Не перезавантажувати файли, які вже є на диску")
        self.save_json = QCheckBox("Зберігати повний JSON закупівлі поруч із файлами")
        self.all_versions = QCheckBox("Качати всі версії документів, а не лише останню")
        card.add(self.fresh_start)
        card.add(self.skip_existing)
        card.add(self.save_json)
        card.add(self.all_versions)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Не качати файли, більші за (МБ, 0 — без обмежень):"))
        self.max_file_mb = QSpinBox()
        self.max_file_mb.setRange(0, 4096)
        size_row.addWidget(self.max_file_mb)
        size_row.addStretch(1)
        card.add(size_row)
        return card

    def _card_companies(self) -> Card:
        card = Card("Наші компанії та конкуренти",
                    "ЄДРПОУ ваших ТОВ потрібні, щоб в аналітиці відрізняти власні "
                    "закупівлі від чужих. Конкуренти зі списку розбираються поіменно "
                    "завжди — навіть якщо в конкретній вибірці вони за обсягом не "
                    "потрапили б у верхівку рейтингу.")
        card.add(QLabel("Наші ЄДРПОУ", objectName="Muted"))
        self.own_edrpou = EdrpouList("ЄДРПОУ вашого ТОВ", labels=OWN_COMPANIES)
        card.add(self.own_edrpou)
        card.add(QLabel("Конкуренти, яких відстежуємо", objectName="Muted"))
        self.competitors = EdrpouList("ЄДРПОУ конкурента", labels=COMPETITOR_COMPANIES)
        card.add(self.competitors)
        return card

    def _card_index(self) -> Card:
        card = Card(
            "Локальний індекс закупівель",
            "Пошук порталу віддає лише номер закупівлі (UA-…), а файли лежать у Центральній "
            "базі, яка адресує закупівлі за внутрішнім ідентифікатором. Портал уміє "
            "віддати відповідність за номером, але має квоту — 58 запитів на хвилину, "
            "тож поштучне опитування вигідне лише для вузьких вибірок. Індекс — це "
            "локальна таблиця, яку будує обхід стрічки змін: квоти не витрачає, зате "
            "коштує пропорційно довжині періоду. «Автоматично» обирає дешевше.")
        row = QHBoxLayout()
        row.addWidget(QLabel("Спосіб розпізнавання:"))
        self.resolve_mode = QComboBox()
        for value, label in RESOLVE_MODES.items():
            self.resolve_mode.addItem(label, value)
        row.addWidget(self.resolve_mode, 1)
        card.add(row)

        self.index_info = wrapped_label("")
        card.add(self.index_info)

        card.add(QLabel("Період для побудови індексу", objectName="Muted"))
        self.index_dates = DateRange()
        card.add(self.index_dates)

        self.index_progress = QProgressBar()
        self.index_progress.setFormat("Індекс не будується")
        card.add(self.index_progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_index = QPushButton("Побудувати індекс за період")
        self.btn_index.clicked.connect(self._emit_build)
        buttons.addWidget(self.btn_index)
        self.btn_index_stop = QPushButton("Зупинити")
        self.btn_index_stop.setObjectName("Danger")
        self.btn_index_stop.setEnabled(False)
        self.btn_index_stop.clicked.connect(self.stop_index.emit)
        buttons.addWidget(self.btn_index_stop)
        clear = QPushButton("Очистити індекс")
        clear.clicked.connect(self._clear_index)
        buttons.addWidget(clear)
        drop = QPushButton("Прибрати зібране")
        drop.setToolTip("Видалити картки закупівель і товарів, лишивши індекс.")
        drop.clicked.connect(self._clear_collected)
        buttons.addWidget(drop)
        card.add(buttons)
        return card

    def _card_network(self) -> Card:
        card = Card("Мережа та швидкість",
                    "Кожен сервер має власне обмеження, виміряне на живих API, і воно "
                    "діє завжди. Пошуковий портал дає 60 запитів на хвилину — не темп, "
                    "а квоту на вікно, тож зайві потоки там лише чекають у черзі. "
                    "Центральна база квоти не має і тримає близько 30 запитів на секунду "
                    "при 16 з'єднаннях. Загальний темп нижче стосується решти серверів.")
        grid = QVBoxLayout()
        self.rate_limit = _spin(1, 60, " запитів/с")
        self.search_conc = _spin(1, 16, " потоків")
        self.detail_conc = _spin(1, 32, " потоків")
        self.download_conc = _spin(1, 32, " потоків")
        self.index_conc = _spin(1, 12, " відрізків")
        self.timeout = _spin(10, 300, " с")
        self.retries = _spin(0, 10, " спроб")
        for label, widget in [
            ("Темп для інших серверів", self.rate_limit),
            ("Паралельних пошукових запитів", self.search_conc),
            ("Паралельних завантажень карток", self.detail_conc),
            ("Паралельних завантажень файлів", self.download_conc),
            ("Паралельних відрізків індексації", self.index_conc),
            ("Таймаут запиту", self.timeout),
            ("Повторів при помилці", self.retries),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label), 1)
            row.addWidget(widget)
            grid.addLayout(row)
        card.add(grid)
        return card

    def _card_look(self) -> Card:
        card = Card("Вигляд")
        row = QHBoxLayout()
        row.addWidget(QLabel("Тема оформлення:"))
        self.theme = QComboBox()
        self.theme.addItem("Темна", "dark")
        self.theme.addItem("Світла", "light")
        row.addWidget(self.theme)
        row.addStretch(1)
        card.add(row)
        card.add(QLabel("Зміна теми застосується одразу.", objectName="Muted"))
        self.theme.currentIndexChanged.connect(self._theme_changed)
        return card

    # --- обмін даними -----------------------------------------------------

    def load(self) -> None:
        s = self.s
        self.output_dir.setText(s.output_dir)
        self.fresh_start.setChecked(s.fresh_start)
        self.skip_existing.setChecked(s.skip_existing)
        self.save_json.setChecked(s.save_tender_json)
        self.all_versions.setChecked(s.download_all_versions)
        self.max_file_mb.setValue(s.max_file_mb)
        self.own_edrpou.set_values(s.own_edrpou)
        self.competitors.set_values(s.competitors)
        self.resolve_mode.setCurrentIndex(max(0, self.resolve_mode.findData(s.resolve_mode)))
        self.rate_limit.setValue(int(s.rate_limit_rps))
        self.search_conc.setValue(s.search_concurrency)
        self.detail_conc.setValue(s.detail_concurrency)
        self.download_conc.setValue(s.download_concurrency)
        self.index_conc.setValue(s.index_concurrency)
        self.timeout.setValue(s.request_timeout)
        self.retries.setValue(s.max_retries)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        self.index_dates.set_values(s.preset.date_from, s.preset.date_to)
        self.refresh_index_info()

    def apply(self) -> None:
        s = self.s
        s.output_dir = self.output_dir.text().strip() or s.output_dir
        s.fresh_start = self.fresh_start.isChecked()
        s.skip_existing = self.skip_existing.isChecked()
        s.save_tender_json = self.save_json.isChecked()
        s.download_all_versions = self.all_versions.isChecked()
        s.max_file_mb = self.max_file_mb.value()
        s.own_edrpou = self.own_edrpou.values()
        s.competitors = self.competitors.values()
        s.resolve_mode = self.resolve_mode.currentData() or "auto"
        s.rate_limit_rps = float(self.rate_limit.value())
        s.search_concurrency = self.search_conc.value()
        s.detail_concurrency = self.detail_conc.value()
        s.download_concurrency = self.download_conc.value()
        s.index_concurrency = self.index_conc.value()
        s.request_timeout = self.timeout.value()
        s.max_retries = self.retries.value()
        s.theme = self.theme.currentData() or "dark"

    def apply_and_save(self) -> None:
        if not self._check_output_dir():
            return
        self.apply()
        self.s.save()
        self.settings_changed.emit()
        QMessageBox.information(self, "Збережено", "Налаштування збережено.")

    def _check_output_dir(self) -> bool:
        """Переконуємось, що тека для завантажень справді придатна для запису."""
        path = Path(self.output_dir.text().strip() or self.s.output_dir)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".prozorro-write-test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            QMessageBox.warning(
                self, "Тека недоступна",
                f"Не вдалося писати в «{path}»:\n{exc}\n\nОберіть іншу теку.")
            return False
        return True

    def _reset(self) -> None:
        defaults = Settings()
        defaults.preset = self.s.preset
        for field in defaults.__dataclass_fields__:
            if field != "preset":
                setattr(self.s, field, getattr(defaults, field))
        self.load()

    # --- дії --------------------------------------------------------------

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Тека для завантажень",
                                                self.output_dir.text() or str(Path.home()))
        if path:
            self.output_dir.setText(path)

    def _emit_build(self) -> None:
        self.apply()
        self.index_dates.normalize()
        date_from, date_to = self.index_dates.values()
        self.build_index.emit(date_from, date_to)

    def _clear_collected(self) -> None:
        """Ручне очищення — коли базу треба прибрати, не запускаючи збір."""
        rows = sum(self.db.scalar(f"SELECT COUNT(*) FROM {t}") or 0
                   for t in self.db.COLLECTED)
        if not rows:
            QMessageBox.information(self, "Уже порожньо",
                                    "У базі немає зібраних карток.")
            return
        answer = QMessageBox.question(
            self, "Прибрати зібране",
            f"Буде видалено {rows:,} рядків карток закупівель і товарів.\n\n"
            f"Індекс tenderID→UUID лишиться — його не доведеться будувати "
            f"заново. Вивантажені книги .xlsx і завантажені документи на диску "
            f"теж лишаться недоторканими.\n\nПродовжити?".replace(",", " "))
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = self.db.reset_collected()
        QMessageBox.information(
            self, "Готово",
            "Прибрано:\n" + "\n".join(f"  {table} — {n:,}".replace(",", " ")
                                      for table, n in removed.items()))
        self.refresh_index_info()

    def _clear_index(self) -> None:
        size = self.db.index_size()
        answer = QMessageBox.question(
            self, "Очистити індекс",
            f"Вилучити {_spaced(size)} записів індексу та стиснути файл бази?\n\n"
            f"Завантажені картки закупівель і файли не постраждають, але "
            f"індекс доведеться будувати наново. Стиснення великої бази може "
            f"тривати кілька хвилин.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.db.execute("DELETE FROM tender_index")
            self.db.execute("DELETE FROM index_coverage")
            self.db.execute("VACUUM")
        finally:
            QApplication.restoreOverrideCursor()
        self.refresh_index_info()

    def _theme_changed(self) -> None:
        self.s.theme = self.theme.currentData() or "dark"
        self.settings_changed.emit()

    # --- відомості про індекс --------------------------------------------

    def refresh_index_info(self) -> None:
        size = self.db.index_size()
        days = len(self.db.coverage_days())
        db_mb = 0.0
        try:
            db_mb = Path(self.db.path).stat().st_size / 1048576
        except OSError:
            pass
        self.index_info.setText(
            f"У індексі {_spaced(size)} закупівель, покрито діб: {days}. "
            f"Файл бази: {db_mb:.0f} МБ. "
            f"Орієнтовна швидкість побудови — близько 2 000 записів/с, "
            f"тобто повний рік займає 30–50 хвилин і додає ~400 МБ.")

    def set_index_running(self, running: bool) -> None:
        self.btn_index.setEnabled(not running)
        self.btn_index_stop.setEnabled(running)
        if not running:
            self.index_progress.setFormat("Індекс не будується")
            self.index_progress.setValue(0)
            self.refresh_index_info()

    def set_index_progress(self, stage: str, done: int, total: int) -> None:
        if total <= 0:
            self.index_progress.setRange(0, 0)
            self.index_progress.setFormat(stage)
            return
        self.index_progress.setRange(0, total)
        self.index_progress.setValue(done)
        self.index_progress.setFormat(f"{stage} — {done}/{total} діб")


def _spin(minimum: int, maximum: int, suffix: str) -> QSpinBox:
    box = QSpinBox()
    box.setRange(minimum, maximum)
    box.setSuffix(suffix)
    return box
