"""Точка входу застосунку."""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME
from .config import Settings
from .core.db import Database
from .paths import DB_FILE
from .ui.main_window import MainWindow


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("ProzorroDownloader")

    settings = Settings.load()
    try:
        db = Database(DB_FILE)
    except Exception as exc:
        QMessageBox.critical(None, APP_NAME, f"Не вдалося відкрити локальну базу:\n{exc}")
        return 1

    window = MainWindow(settings, db)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
