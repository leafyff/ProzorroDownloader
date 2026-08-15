"""Сторінка «Журнал»: перебіг роботи та помилки."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

LEVEL_MARK = {"info": "·", "warn": "!", "error": "×"}
LEVEL_COLOR = {"info": "#98a1b2", "warn": "#f0b429", "error": "#ef5f5f"}

MAX_LINES = 5000


class LogPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        title = QLabel("Журнал")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        hint = QLabel("Хронологія запитів, попереджень і помилок. Стане у пригоді, "
                      "якщо частина файлів не завантажилася.")
        hint.setObjectName("PageHint")
        root.addWidget(hint)

        tools = QHBoxLayout()
        self.only_problems = QCheckBox("Лише попередження та помилки")
        self.only_problems.stateChanged.connect(self._rerender)
        tools.addWidget(self.only_problems)
        tools.addStretch(1)
        save = QPushButton("Зберегти у файл")
        save.clicked.connect(self.save)
        tools.addWidget(save)
        clear = QPushButton("Очистити")
        clear.clicked.connect(self.clear)
        tools.addWidget(clear)
        root.addLayout(tools)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)
        root.addWidget(self.view, 1)

        self._entries: list[tuple[str, str, str]] = []

    def append(self, level: str, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self._entries.append((stamp, level, message))
        if len(self._entries) > MAX_LINES:
            del self._entries[:len(self._entries) - MAX_LINES]
        if self.only_problems.isChecked() and level == "info":
            return
        self._write(stamp, level, message)

    def _write(self, stamp: str, level: str, message: str) -> None:
        color = LEVEL_COLOR.get(level, "#98a1b2")
        mark = LEVEL_MARK.get(level, "·")
        self.view.appendHtml(
            f'<span style="color:#6b7484">{stamp}</span> '
            f'<span style="color:{color}">{mark}</span> '
            f'<span style="color:{color if level != "info" else "#d7dae2"}">{_escape(message)}</span>')
        self.view.moveCursor(QTextCursor.MoveOperation.End)

    def _rerender(self) -> None:
        self.view.clear()
        for stamp, level, message in self._entries:
            if self.only_problems.isChecked() and level == "info":
                continue
            self._write(stamp, level, message)

    def clear(self) -> None:
        self._entries.clear()
        self.view.clear()

    def save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Зберегти журнал",
            str(Path.home() / f"prozorro-журнал-{datetime.now():%Y%m%d-%H%M}.txt"),
            "Текстовий файл (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            for stamp, level, message in self._entries:
                fh.write(f"{stamp} [{level}] {message}\n")


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
