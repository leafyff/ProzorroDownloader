"""Головне вікно застосунку."""
from __future__ import annotations

from datetime import date, datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QStatusBar, QVBoxLayout, QWidget,
)

from .. import APP_NAME, APP_VERSION
from ..config import Settings
from ..core.db import Database
from .pages.files_page import FilesPage
from .pages.log_page import LogPage
from .pages.results_page import ResultsPage
from .pages.search_page import SearchPage
from .pages.settings_page import SettingsPage
from .theme import palette, stylesheet
from .workers import CountWorker, DownloadWorker, IndexWorker, RetryWorker

NAV = [
    ("Пошук і завантаження", "search"),
    ("Результати", "results"),
    ("Файли", "files"),
    ("Журнал", "log"),
    ("Налаштування", "settings"),
]


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, db: Database):
        super().__init__()
        self.s = settings
        self.db = db
        self.worker: DownloadWorker | CountWorker | None = None
        self.index_worker: IndexWorker | None = None

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1440, 920)
        self.setMinimumSize(1220, 720)
        self.setWindowIcon(_app_icon(palette(settings.theme)["accent"]))

        root = QWidget()
        root.setObjectName("Root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.search_page = SearchPage()
        self.results_page = ResultsPage(db)
        self.files_page = FilesPage(db, lambda: self.s.output_dir)
        self.log_page = LogPage()
        self.settings_page = SettingsPage(settings, db)
        for page in (self.search_page, self.results_page, self.files_page,
                     self.log_page, self.settings_page):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Готово")

        self._wire()
        self.search_page.load_preset(settings.preset)
        self.results_page.reload()
        self.files_page.reload()
        self.apply_theme()

    # --- побудова ---------------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(232)
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 12)
        lay.setSpacing(0)

        brand = QLabel("Prozorro Downloader")
        brand.setObjectName("Brand")
        sub = QLabel("Відкриті дані публічних закупівель")
        sub.setObjectName("BrandSub")
        sub.setWordWrap(True)
        lay.addWidget(brand)
        lay.addWidget(sub)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, (title, _key) in enumerate(NAV):
            btn = QPushButton(title)
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, i=index: self._go(i))
            self.nav_group.addButton(btn, index)
            lay.addWidget(btn)
        self.nav_group.button(0).setChecked(True)

        lay.addStretch(1)
        self.side_note = QLabel("")
        self.side_note.setObjectName("Muted")
        self.side_note.setWordWrap(True)
        self.side_note.setContentsMargins(18, 0, 14, 0)
        lay.addWidget(self.side_note)
        return bar

    def _go(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 1:
            self.results_page.reload()
        elif index == 2:
            self.files_page.reload()
        elif index == 4:
            self.settings_page.refresh_index_info()

    def _wire(self) -> None:
        self.search_page.start_download.connect(self.start_download)
        self.search_page.start_count.connect(self.start_count)
        self.search_page.stop_requested.connect(self.stop_worker)
        self.files_page.retry_failed.connect(self.start_retry)
        self.settings_page.build_index.connect(self.start_index)
        self.settings_page.stop_index.connect(self.stop_index)
        self.settings_page.settings_changed.connect(self.apply_theme)

    def apply_theme(self) -> None:
        self.setStyleSheet(stylesheet(self.s.theme))
        self.setWindowIcon(_app_icon(palette(self.s.theme)["accent"]))

    # --- запуск завдань ---------------------------------------------------

    def _busy(self) -> bool:
        """Одночасно виконуємо лише одне завдання: вони ділять і темп запитів, і базу."""
        for worker, what in ((self.worker, "Попереднє завдання"),
                             (self.index_worker, "Побудова індексу")):
            if worker and worker.isRunning():
                QMessageBox.information(self, "Зачекайте", f"{what} ще виконується.")
                return True
        return False

    def _current_preset(self):
        preset = self.search_page.to_preset()
        self.s.preset = preset
        self.s.save()
        return preset

    def start_download(self) -> None:
        if self._busy():
            return
        preset = self._current_preset()
        if not preset.cpv_prefixes and not preset.text and not preset.tenderers \
                and not preset.buyers:
            answer = QMessageBox.question(
                self, "Надто широкий фільтр",
                "Не задано ані класу ДК021, ані тексту, ані ЄДРПОУ.\n"
                "Пошук по всьому реєстру триватиме дуже довго. Продовжити?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.log_page.append("info", f"— Запуск завантаження {datetime.now():%d.%m.%Y %H:%M} —")
        worker = DownloadWorker(self.s, preset, self.db, self)
        worker.log.connect(self.log_page.append)
        worker.progress.connect(self.search_page.set_progress)
        worker.stats.connect(self.search_page.set_stats)
        worker.tender.connect(self.results_page.add_row)
        worker.finished_job.connect(self._download_finished)
        self.worker = worker
        self.search_page.set_running(True)
        self.statusBar().showMessage("Триває завантаження…")
        worker.start()

    def start_count(self) -> None:
        if self._busy():
            return
        preset = self._current_preset()
        self.log_page.append("info", "— Підрахунок за фільтром —")
        worker = CountWorker(self.s, preset, self.db, self)
        worker.log.connect(self.log_page.append)
        worker.progress.connect(self.search_page.set_progress)
        worker.finished_job.connect(self._count_finished)
        self.worker = worker
        self.search_page.set_running(True)
        self.statusBar().showMessage("Рахую…")
        worker.start()

    def start_retry(self) -> None:
        if self._busy():
            return
        failed = self.db.scalar("SELECT COUNT(*) FROM documents WHERE state='error'") or 0
        if not failed:
            QMessageBox.information(self, "Немає чого повторювати",
                                    "Усі відомі файли вже завантажено.")
            return
        answer = QMessageBox.question(
            self, "Повторити завантаження",
            f"Спробувати ще раз завантажити {failed} файл(ів), які не вдалося взяти?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.log_page.append("info", f"— Повтор {failed} невдалих файлів —")
        worker = RetryWorker(self.s, self.db, self)
        worker.log.connect(self.log_page.append)
        worker.progress.connect(self.search_page.set_progress)
        worker.stats.connect(self.search_page.set_stats)
        worker.finished_job.connect(self._retry_finished)
        self.worker = worker
        self.search_page.set_running(True)
        self.files_page.btn_retry.setEnabled(False)
        self.statusBar().showMessage("Повторюємо завантаження…")
        worker.start()

    def stop_worker(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.statusBar().showMessage("Зупиняємо…")

    def _download_finished(self, result) -> None:
        self.search_page.set_running(False)
        self.files_page.btn_retry.setEnabled(True)
        self.results_page.reload()
        self.files_page.reload()
        self.statusBar().showMessage("Готово")
        if result is None:
            return
        if result.cancelled:
            self.statusBar().showMessage("Зупинено користувачем")
            return
        text = (f"Знайдено закупівель: {result.found}\n"
                f"Завантажено карток: {result.tenders_loaded}\n"
                f"Файлів збережено: {result.files_ok}"
                f" (пропущено {result.files_skipped}, помилок {result.files_failed})\n"
                f"Обсяг: {result.bytes / 1048576:.1f} МБ")
        if result.files_filtered:
            text += (f"\nВідсіяно фільтром типів файлів: {result.files_filtered}"
                     f" (переважно підписи КЕП)")
        if result.unresolved:
            text += (f"\n\nНе вдалося визначити ідентифікатор для {len(result.unresolved)} "
                     f"закупівель — здебільшого це ті, за якими ще немає договору. "
                     f"Побудуйте локальний індекс у налаштуваннях, щоб охопити їх.")
        if result.error:
            text += f"\n\nПомилка: {result.error}"
        QMessageBox.information(self, "Завантаження завершено", text)

    def _retry_finished(self, result) -> None:
        self.search_page.set_running(False)
        self.files_page.btn_retry.setEnabled(True)
        self.files_page.reload()
        self.statusBar().showMessage("Готово")
        if result is None or result.cancelled:
            return
        left = self.db.scalar("SELECT COUNT(*) FROM documents WHERE state='error'") or 0
        QMessageBox.information(
            self, "Повтор завершено",
            f"Дозавантажено файлів: {result.files_ok}"
            f" (обсяг {result.bytes / 1048576:.1f} МБ).\n"
            + (f"Не піддалися: {left}." if left else "Невдалих не лишилося."))

    def _count_finished(self, cards) -> None:
        self.search_page.set_running(False)
        self.statusBar().showMessage("Готово")
        count = len(cards or {})
        self.search_page.tile_found.set(f"{count:,}".replace(",", " "))
        QMessageBox.information(
            self, "Підрахунок завершено",
            f"За цим фільтром знайдено закупівель: {count}.\n\n"
            f"Це кількість карток у пошуку порталу; фактично завантажених може бути менше "
            f"через уточнювальні фільтри (сума, регіон, статус).")

    # --- індекс -----------------------------------------------------------

    def start_index(self, date_from: str, date_to: str) -> None:
        if self._busy():
            return
        since = _as_date(date_from)
        until = _as_date(date_to)
        days = (until - since).days + 1
        answer = QMessageBox.question(
            self, "Побудова індексу",
            f"Буде пройдено стрічку змін Центральної бази за {days} діб "
            f"({since:%d.%m.%Y} – {until:%d.%m.%Y}).\n\n"
            f"Орієнтовно це {days * 17000 / 2000 / 60:.0f} хв і ~{days * 17000 * 80 / 1048576:.0f} МБ "
            f"у локальній базі. Процес можна зупинити й продовжити пізніше — "
            f"пройдені доби не обробляються двічі.\n\nПочати?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        worker = IndexWorker(self.s, self.db, since, until, self)
        worker.log.connect(self.log_page.append)
        worker.progress.connect(self.settings_page.set_index_progress)
        worker.finished_job.connect(self._index_finished)
        self.index_worker = worker
        self.settings_page.set_index_running(True)
        self.statusBar().showMessage("Будуємо індекс…")
        worker.start()

    def stop_index(self) -> None:
        if self.index_worker and self.index_worker.isRunning():
            self.index_worker.stop()

    def _index_finished(self, count) -> None:
        self.settings_page.set_index_running(False)
        self.statusBar().showMessage("Готово")
        self.log_page.append("info", f"Індексацію завершено, оброблено записів: {count}.")

    # --- закриття ---------------------------------------------------------

    def closeEvent(self, event) -> None:
        running = [w for w in (self.worker, self.index_worker) if w and w.isRunning()]
        if running:
            answer = QMessageBox.question(
                self, "Завдання ще виконується",
                "Зупинити роботу й вийти? Завантажені файли залишаться на диску.")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for worker in running:
                worker.stop()
                worker.wait(5000)
        self.settings_page.apply()
        self.s.preset = self.search_page.to_preset()
        self.s.save()
        self.db.close()
        event.accept()


def _as_date(value: str) -> date:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return date.today()


def _app_icon(color: str) -> QIcon:
    """Проста намальована іконка, щоб не тягнути зовнішні ресурси."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(2, 2, 60, 60, 14, 14)
    painter.setPen(QColor("#ffffff"))
    font = QFont("Segoe UI", 30, QFont.Weight.Bold)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "P")
    painter.end()
    return QIcon(pixmap)
