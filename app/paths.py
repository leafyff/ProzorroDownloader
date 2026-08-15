"""Шляхи застосунку: код, дані, кеш, налаштування."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Корінь проєкту (тека, де лежить run.bat)
if getattr(sys, "frozen", False):  # зібраний .exe
    PROJECT_ROOT = Path(sys.executable).parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

BUNDLED_DATA = PROJECT_ROOT / "data"


def user_data_dir() -> Path:
    """Тека користувача для БД, налаштувань і логів."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        p = Path(base) / "ProzorroDownloader"
    else:
        p = Path.home() / ".prozorro-downloader"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_output_dir() -> Path:
    """Тека за замовчуванням для завантажених файлів."""
    return PROJECT_ROOT / "downloads"


SETTINGS_FILE = user_data_dir() / "settings.json"
DB_FILE = user_data_dir() / "prozorro.db"
LOG_DIR = user_data_dir() / "logs"


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
