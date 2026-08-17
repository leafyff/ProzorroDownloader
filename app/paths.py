"""Шляхи застосунку: код, дані, кеш, налаштування."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

# Корінь проєкту — тека, де лежить run.bat.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Вбудовані довідники: класифікатор ДК021 та перелік регіонів.
BUNDLED_DATA = PROJECT_ROOT / "data"


def user_data_dir() -> Path:
    """Тека для бази й налаштувань.

    Свідомо всередині проєкту, а не в ``%LOCALAPPDATA%``: так усе, що програма
    створює, лежить в одному місці — теку можна перенести чи скопіювати разом
    із даними, і ніщо не губиться поза нею.
    """
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    return PROJECT_ROOT


def default_output_dir() -> Path:
    """Тека для завантажених файлів і вивантажених таблиць."""
    return PROJECT_ROOT / "downloads"


def export_path(stem: str, suffix: str = ".xlsx") -> Path:
    """Шлях для нової таблиці в теці завантажень, з відміткою часу в назві."""
    folder = default_output_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{stem}-{datetime.now():%Y-%m-%d-%H%M}{suffix}"


SETTINGS_FILE = user_data_dir() / "settings.json"
DB_FILE = user_data_dir() / "prozorro.db"


# --- безпечні імена файлів/тек для Windows -------------------------------

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, max_len: int = 60) -> str:
    """Перетворює довільний рядок на безпечне ім'я файлу/теки Windows."""
    name = (name or "").strip()
    name = _BAD_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "_"
    if name.upper().split(".")[0] in _RESERVED:
        name = "_" + name
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 8:
            keep = max_len - len(ext) - 1
            name = stem[:keep].strip(" .") + "." + ext
        else:
            name = name[:max_len].strip(" .")
    return name or "_"


def long_path(p: Path) -> str:
    r"""Шлях у форматі, який обходить ліміт MAX_PATH (260) у Windows."""
    s = str(p.resolve())
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        if s.startswith("\\\\"):
            return "\\\\?\\UNC" + s[1:]
        return "\\\\?\\" + s
    return s
