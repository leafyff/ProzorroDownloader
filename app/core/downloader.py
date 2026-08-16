"""Завантаження файлів закупівлі на диск."""
from __future__ import annotations

import mimetypes
import os
import threading
from pathlib import Path
from typing import Callable

from .db import Database
from .extract import SCOPE_LABELS
from .http import Cancelled, HttpClient
from ..paths import long_path, safe_name

LogCb = Callable[[str, str], None]

#: Скільки разів перечитувати файл цілком, якщо потік обірвався.
FILE_ATTEMPTS = 3

CHUNK_SIZE = 256 * 1024


class TooLarge(Exception):
    """Файл більший за дозволений ліміт — повторювати немає сенсу."""


#: Розширення відокремлених підписів КЕП. Самі по собі вони не містять
#: жодного змісту — лише криптографічний підпис до сусіднього файлу.
SIGNATURE_EXTENSIONS = {".p7s", ".p7b", ".p7m", ".sig", ".sign", ".cades", ".pkcs7"}

#: MIME-типи тих самих підписів — надійніший спосіб, ніж назва файлу.
SIGNATURE_FORMATS = {
    "application/pkcs7-signature",
    "application/x-pkcs7-signature",
    "application/pkcs7-mime",
    "application/x-pkcs7-mime",
    "application/pkcs7",
}

#: Текстові документи — найцінніше в закупівлі.
TEXT_EXTENSIONS = ["pdf", "docx", "doc", "rtf", "odt"]

#: Те саме плюс таблиці й архіви. Архіви навмисно тут: учасники часто
#: вантажать усю пропозицію одним zip/rar, і без них губиться змістовна частина
#: (на перевіреному зрізі це 70 файлів і 210 МБ).
DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS + ["xlsx", "xls", "ods", "zip", "rar", "7z"]

#: Розширення за MIME, якщо в назві файлу його немає.
_EXTRA_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.ms-excel": ".xls",
    "application/msword": ".doc",
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "application/x-rar-compressed": ".rar",
    "application/x-7z-compressed": ".7z",
    "application/zip": ".zip",
    "text/plain": "",   # Prozorro часто віддає text/plain для будь-чого
}


class FileDownloader:
    """Качає документи, тримає структуру тек і оновлює стан у БД."""

    def __init__(self, client: HttpClient, db: Database, root: Path, *,
                 max_file_mb: int = 0, skip_existing: bool = True,
                 on_log: LogCb | None = None):
        self.client = client
        self.db = db
        self.root = Path(root)
        self.max_bytes = max_file_mb * 1024 * 1024 if max_file_mb else 0
        self.skip_existing = skip_existing
        self._log = on_log or (lambda level, msg: None)
        self._lock = threading.Lock()
        self._taken: set[str] = set()

        self.ok = 0
        self.skipped = 0
        self.failed = 0
        self.bytes = 0

    # --- шляхи ------------------------------------------------------------

    def tender_dir(self, tender_id: str, title: str, date_created: str) -> Path:
        return tender_folder(self.root, tender_id, title, date_created)

    def target_path(self, doc: dict, tender_dir: Path) -> Path:
        # Якщо шлях уже обирався раніше (наприклад, це повтор після збою) —
        # беремо той самий, інакше порядок обробки міг би дати іншу назву.
        # Але тільки якщо він досі в поточній теці завантажень: користувач
        # міг змінити її в налаштуваннях між запусками.
        recorded = (doc.get("local_path") or "").strip()
        if recorded and _is_within(recorded, self.root):
            with self._lock:
                self._taken.add(recorded.lower())
            return Path(recorded)

        scope_dir = safe_name(SCOPE_LABELS.get(doc.get("scope") or "other", "Інше"), 40)
        parts = [tender_dir, scope_dir]
        owner = (doc.get("owner_name") or "").strip()
        if doc.get("scope") in ("bid", "award", "contract") and owner:
            tag = doc.get("owner_edrpou") or ""
            parts.append(safe_name(f"{tag} {owner}".strip(), max_len=60))
        name = self._file_name(doc)
        path = Path(*parts) / name
        with self._lock:
            key = str(path).lower()
            if key in self._taken:
                stem, dot, ext = name.rpartition(".")
                base = stem if dot else name
                suffix = f".{ext}" if dot else ""
                i = 2
                while True:
                    candidate = Path(*parts) / f"{base}_{i}{suffix}"
                    if str(candidate).lower() not in self._taken:
                        path = candidate
                        break
                    i += 1
            self._taken.add(str(path).lower())
        return path

    def _file_name(self, doc: dict) -> str:
        return file_name_for(doc)

    # --- завантаження -----------------------------------------------------

    def fetch(self, doc: dict, tender_dir: Path) -> tuple[str, str]:
        """Качає один документ. Повертає ``(стан, повідомлення)``.

        Обрив посеред тіла відповіді — звична річ для сервера документів
        Prozorro під навантаженням, і ретраї всередині :class:`HttpClient`
        його не ловлять: там повторюється лише саме з'єднання, а не читання
        потоку. Тому кожен файл додатково перечитується цілком до
        :data:`FILE_ATTEMPTS` разів.
        """
        url = doc.get("url") or ""
        if not url:
            self._bump("skipped")
            return "skipped", "немає посилання"

        path = self.target_path(doc, tender_dir)
        if self.skip_existing:
            try:
                st = os.stat(long_path(path))
                if st.st_size > 0:
                    self.db.mark_document(doc["key"], state="ok",
                                          local_path=str(path), size=st.st_size)
                    self._bump("skipped")
                    return "skipped", "вже завантажено"
            except OSError:
                pass

        last_error = ""
        for attempt in range(FILE_ATTEMPTS):
            try:
                written = self._stream(url, path)
            except Cancelled:
                _unlink(path.with_name(path.name + ".part"))
                raise
            except TooLarge as exc:
                self.db.mark_document(doc["key"], state="skipped",
                                      local_path=str(path), error=str(exc))
                self._bump("skipped")
                return "skipped", str(exc)
            except Exception as exc:
                last_error = str(exc)
                self.client.note_error()
                if attempt + 1 < FILE_ATTEMPTS:
                    self._log("warn", f"«{doc.get('title')}»: {exc} — пробуємо ще раз")
                    self.client.backoff(attempt)
                    continue
                break
            self.db.mark_document(doc["key"], state="ok", local_path=str(path), size=written)
            self.client.note_bytes(written)
            with self._lock:
                self.ok += 1
                self.bytes += written
            return "ok", str(path)

        # Шлях запам'ятовуємо навіть при збої — щоб повторна спроба
        # поклала файл рівно туди, куди він і мав потрапити.
        self.db.mark_document(doc["key"], state="error", local_path=str(path),
                              error=last_error[:300])
        self._bump("failed")
        self._log("warn", f"Не завантажено «{doc.get('title')}»: {last_error}")
        return "error", last_error

    def _stream(self, url: str, path: Path) -> int:
        """Завантажує тіло відповіді у тимчасовий файл і перейменовує його.

        Повертає кількість записаних байтів. Відповідь закривається завжди —
        інакше з'єднання не повертається в пул і наступні завантаження
        починають падати одне за одним.
        """
        tmp = path.with_name(path.name + ".part")
        resp = self.client.request("GET", url, stream=True)
        try:
            if resp.status_code >= 400:
                raise IOError(f"HTTP {resp.status_code}")
            declared = _int(resp.headers.get("Content-Length"))
            if self.max_bytes and declared > self.max_bytes:
                raise TooLarge(f"файл {declared // 1048576} МБ перевищує ліміт")

            path.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                with open(long_path(tmp), "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        self.client.check_cancel()
                        if not chunk:
                            continue
                        fh.write(chunk)
                        written += len(chunk)
                        if self.max_bytes and written > self.max_bytes:
                            raise TooLarge("файл перевищив ліміт під час завантаження")
            except BaseException:
                _unlink(tmp)
                raise
            # Недовантажений файл гірший за відсутній: краще позначити збій
            # і перекачати, ніж лишити на диску обрізаний документ.
            if declared and written < declared:
                _unlink(tmp)
                raise IOError(f"отримано {written} з {declared} байтів")
        finally:
            resp.close()
        os.replace(long_path(tmp), long_path(path))
        return written

    def _bump(self, field: str) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + 1)


def _unlink(path: Path) -> None:
    try:
        os.unlink(long_path(path))
    except OSError:
        pass


def document_extension(doc: dict) -> str:
    """Розширення документа в нижньому регістрі, з крапкою: ``.pdf``.

    Береться з назви, а якщо її немає — визначається за MIME-типом,
    тобто рівно так само, як формується ім'я файлу на диску.
    """
    name = file_name_for(doc)
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot and len(ext) <= 8 else ""


def is_signature(doc: dict) -> bool:
    """Чи це відокремлений підпис КЕП, а не змістовний документ."""
    if (doc.get("format") or "").strip().lower() in SIGNATURE_FORMATS:
        return True
    return document_extension(doc) in SIGNATURE_EXTENSIONS


def file_name_for(doc: dict) -> str:
    """Ім'я файлу на диску для документа (без розведення збігів)."""
    title = (doc.get("title") or "").strip() or (doc.get("doc_id") or "file")
    name = safe_name(title, max_len=90)
    if "." not in name[-6:]:
        fmt = (doc.get("format") or "").lower()
        ext = _EXTRA_MIME.get(fmt)
        if ext is None:
            ext = mimetypes.guess_extension(fmt) or ""
        name += ext
    return name


def normalize_extensions(values) -> set[str]:
    """``['PDF', '.docx', ' xls ']`` → ``{'.pdf', '.docx', '.xls'}``."""
    out: set[str] = set()
    for value in values or []:
        item = str(value).strip().lower().lstrip("*").lstrip(".")
        if item:
            out.add(f".{item}")
    return out


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _is_within(candidate: str, root: Path) -> bool:
    try:
        Path(candidate).relative_to(root)
        return True
    except ValueError:
        return False


def tender_folder(root: Path, tender_id: str, title: str, date_created: str) -> Path:
    """Тека закупівлі: ``<корінь>/<рік-місяць>/<номер назва>``."""
    month = (date_created or "")[:7] or "без-дати"
    folder = safe_name(f"{tender_id} {title}".strip(), max_len=80)
    return root / month / folder
