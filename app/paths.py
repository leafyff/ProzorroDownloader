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

#: Назва єдиної теки, куди програма пише все своє: документи закупівель, книги
#: вивантаження, збережений журнал. Тримаємо саме назву, а не готовий шлях —
#: у ``settings.json`` вона й зберігається відносною (див. ``store_output_dir``).
DOWNLOADS_DIRNAME = "downloads"


def user_data_dir() -> Path:
    """Тека для бази й налаштувань.

    Свідомо всередині проєкту, а не в ``%LOCALAPPDATA%``: так усе, що програма
    створює, лежить в одному місці — теку можна перенести чи скопіювати разом
    із даними, і ніщо не губиться поза нею.
    """
    PROJECT_ROOT.mkdir(parents=True, exist_ok=True)
    return PROJECT_ROOT


def default_output_dir() -> Path:
    """Тека для завантажених файлів і вивантажених таблиць — ``downloads``.

    Створюємо одразу: git не зберігає порожніх тек, тож на свіжій копії її
    немає, а діалогам збереження треба з чогось починати — інакше вони
    відкриються там, де їх лишила система, тобто поза проєктом.
    """
    folder = PROJECT_ROOT / DOWNLOADS_DIRNAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def resolve_output_dir(value: Path | str | None) -> Path:
    """Значення ``Settings.output_dir`` → справжній шлях.

    Порожнє значення й **відносний** шлях відлічуються від кореня проєкту, тож
    типове ``downloads`` лишається в проєкті на будь-якій машині. Абсолютний
    шлях — свідомий вибір користувача в налаштуваннях, його не чіпаємо.

    Раніше типовим значенням був абсолютний шлях, а ``settings.json`` лежить у
    репозиторії: на іншій машині збір ішов у теку з чужого профілю або, якщо
    диска не існує, падав на порожньому переліку книг.
    """
    text = str(value or "").strip()
    if not text:
        return default_output_dir()
    path = Path(text)
    return path if path.is_absolute() else PROJECT_ROOT / path


def store_output_dir(value: Path | str | None) -> str:
    """Значення для запису в ``settings.json``: усередині проєкту — відносне.

    Так налаштування переносяться між машинами, не тягнучи за собою чужого
    ``C:/Users/...``; тека поза проєктом лишається абсолютною, бо коротший
    запис для неї нічого не означав би.
    """
    path = resolve_output_dir(value)
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix() or DOWNLOADS_DIRNAME
    except ValueError:
        return str(path)


def export_path(stem: str, suffix: str = ".xlsx",
                folder: Path | str | None = None) -> Path:
    """Шлях для нової таблиці з відміткою часу в назві.

    ``folder`` — тека вивантаження з налаштувань; без неї береться типова.
    Передавати її треба скрізь, де книгу потім шукає сама програма: сторінка
    аналітики перебирає саме ``Settings.output_dir``, тож книга, збережена
    повз цю теку, звідти не видно.
    """
    folder = resolve_output_dir(folder)
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
