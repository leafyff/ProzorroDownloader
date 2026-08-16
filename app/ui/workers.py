"""Фонові потоки: конвеєр завантаження та побудова індексу."""
from __future__ import annotations

import threading
from datetime import date

from PySide6.QtCore import QObject, QThread, Signal

from ..config import SearchPreset, Settings
from ..core.db import Database
from ..core.http import Cancelled, HttpClient
from ..core.pipeline import Pipeline
from ..core.resolver import IndexBuilder


class _Bridge(QObject):
    """Сигнали, які можна безпечно кидати з робочого потоку."""
    log = Signal(str, str)              # рівень, текст
    progress = Signal(str, int, int)    # етап, зроблено, усього
    stats = Signal(dict)
    tender = Signal(dict)
    finished = Signal(object)           # JobResult | None


class DownloadWorker(QThread):
    """Виконує повний конвеєр пошуку та завантаження."""

    def __init__(self, settings: Settings, preset: SearchPreset, db: Database, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.preset = preset
        self.db = db
        self.cancel_event = threading.Event()
        self.bridge = _Bridge()
        self.pipeline: Pipeline | None = None

    # зручні псевдоніми, щоб вікно підписувалося на self.log і т. ін.
    @property
    def log(self):
        return self.bridge.log

    @property
    def progress(self):
        return self.bridge.progress

    @property
    def stats(self):
        return self.bridge.stats

    @property
    def tender(self):
        return self.bridge.tender

    @property
    def finished_job(self):
        return self.bridge.finished

    def stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        result = None
        try:
            self.pipeline = Pipeline(
                self.settings, self.preset, self.db,
                on_log=lambda lvl, msg: self.bridge.log.emit(lvl, msg),
                on_progress=lambda st, d, t: self.bridge.progress.emit(st, d, t),
                on_stats=lambda s: self.bridge.stats.emit(s),
                on_tender=lambda row: self.bridge.tender.emit(row),
                cancel_event=self.cancel_event,
            )
            result = self.pipeline.run()
        except Exception as exc:                       # страхувальна сітка
            self.bridge.log.emit("error", f"Несподівана помилка: {exc}")
        finally:
            self.bridge.finished.emit(result)


class CountWorker(QThread):
    """Лише пошук: скільки закупівель відповідає фільтру (без завантаження)."""

    def __init__(self, settings: Settings, preset: SearchPreset, db: Database, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.preset = preset
        self.db = db
        self.cancel_event = threading.Event()
        self.bridge = _Bridge()

    @property
    def log(self):
        return self.bridge.log

    @property
    def progress(self):
        return self.bridge.progress

    @property
    def finished_job(self):
        return self.bridge.finished

    def stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        cards: dict = {}
        try:
            pipeline = Pipeline(
                self.settings, self.preset, self.db,
                on_log=lambda lvl, msg: self.bridge.log.emit(lvl, msg),
                on_progress=lambda st, d, t: self.bridge.progress.emit(st, d, t),
                on_stats=lambda s: self.bridge.stats.emit(s),
                cancel_event=self.cancel_event,
            )
            cards = pipeline.discover()
            pipeline.client.close()
        except Cancelled:
            self.bridge.log.emit("warn", "Підрахунок зупинено.")
        except Exception as exc:
            self.bridge.log.emit("error", f"Помилка підрахунку: {exc}")
        finally:
            self.bridge.finished.emit(cards)


class MissingFilesWorker(QThread):
    """Довантаження файлів, яких ще немає на диску."""

    def __init__(self, settings: Settings, db: Database, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.db = db
        self.cancel_event = threading.Event()
        self.bridge = _Bridge()

    @property
    def log(self):
        return self.bridge.log

    @property
    def progress(self):
        return self.bridge.progress

    @property
    def stats(self):
        return self.bridge.stats

    @property
    def finished_job(self):
        return self.bridge.finished

    def stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        result = None
        try:
            pipeline = Pipeline(
                self.settings, self.settings.preset, self.db,
                on_log=lambda lvl, msg: self.bridge.log.emit(lvl, msg),
                on_progress=lambda st, d, t: self.bridge.progress.emit(st, d, t),
                on_stats=lambda s: self.bridge.stats.emit(s),
                cancel_event=self.cancel_event,
            )
            result = pipeline.download_missing()
        except Exception as exc:
            self.bridge.log.emit("error", f"Несподівана помилка: {exc}")
        finally:
            self.bridge.finished.emit(result)


class IndexWorker(QThread):
    """Будує локальний індекс tenderID → UUID за період."""

    def __init__(self, settings: Settings, db: Database, since: date, until: date, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.db = db
        self.since = since
        self.until = until
        self.cancel_event = threading.Event()
        self.bridge = _Bridge()

    @property
    def log(self):
        return self.bridge.log

    @property
    def progress(self):
        return self.bridge.progress

    @property
    def finished_job(self):
        return self.bridge.finished

    def stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        client = HttpClient(
            timeout=self.settings.request_timeout,
            max_retries=self.settings.max_retries,
            rps=max(self.settings.rate_limit_rps, 20.0),
            pool_size=32,
            cancel_event=self.cancel_event,
            on_log=lambda lvl, msg: self.bridge.log.emit(lvl, msg),
        )
        count = 0
        try:
            builder = IndexBuilder(
                client, self.db, shards=self.settings.index_concurrency,
                on_log=lambda lvl, msg: self.bridge.log.emit(lvl, msg),
                keep_all=True,
            )
            count = builder.build(
                self.since, self.until,
                lambda st, d, t: self.bridge.progress.emit(st, d, t),
            )
        except Cancelled:
            self.bridge.log.emit("warn", "Побудову індексу зупинено.")
        except Exception as exc:
            self.bridge.log.emit("error", f"Помилка індексації: {exc}")
        finally:
            client.close()
            self.bridge.finished.emit(count)
