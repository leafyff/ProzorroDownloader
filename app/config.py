"""Налаштування застосунку (зберігаються у %LOCALAPPDATA%/ProzorroDownloader/settings.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .paths import SETTINGS_FILE, default_output_dir

# --- значення за замовчуванням --------------------------------------------

#: Клас ДК021 за замовчуванням — 30 «Офісна та комп'ютерна техніка».
DEFAULT_CPV_PREFIXES = ["30"]

#: Скільки місяців історії брати за замовчуванням.
DEFAULT_PERIOD_MONTHS = 12

#: Типи документів, які качаємо за замовчуванням (порожньо = усі).
DOC_SCOPES = {
    "tender": "Документи закупівлі (тендерна документація)",
    "bid": "Документи пропозицій учасників",
    "award": "Документи кваліфікації/визначення переможця",
    "contract": "Документи договорів",
    "other": "Інші (скарги, скасування, моніторинг)",
}

STATUS_LABELS = {
    "active.enquiries": "Період уточнень",
    "active.tendering": "Подання пропозицій",
    "active.auction": "Аукціон",
    "active.qualification": "Кваліфікація",
    "active.awarded": "Визначено переможця",
    "active": "Активна",
    "complete": "Завершена",
    "cancelled": "Скасована",
    "unsuccessful": "Не відбулася",
    "draft": "Чернетка",
}

METHOD_LABELS = {
    "aboveThresholdUA": "Відкриті торги",
    "aboveThresholdEU": "Відкриті торги (EU)",
    "aboveThreshold": "Відкриті торги (нові)",
    "belowThreshold": "Допорогова",
    "reporting": "Звіт про договір",
    "negotiation": "Переговорна",
    "negotiation.quick": "Переговорна (скор.)",
    "competitiveDialogueUA": "Конкурентний діалог",
    "competitiveDialogueEU": "Конк. діалог (EU)",
    "esco": "ESCO",
    "closeFrameworkAgreementUA": "Рамкова угода",
    "priceQuotation": "Запит пропозицій",
    "simple.defense": "Оборонні спрощені",
}

#: Повні назви процедур — показуємо як підказку над скороченим підписом.
METHOD_HINTS = {
    "aboveThresholdUA": "Відкриті торги",
    "aboveThresholdEU": "Відкриті торги з публікацією англійською мовою",
    "aboveThreshold": "Відкриті торги за новою редакцією Особливостей",
    "belowThreshold": "Спрощена (допорогова) закупівля",
    "reporting": "Звіт про укладений договір без використання процедури",
    "negotiation": "Переговорна процедура",
    "negotiation.quick": "Переговорна процедура, скорочена",
    "competitiveDialogueUA": "Конкурентний діалог",
    "competitiveDialogueEU": "Конкурентний діалог з публікацією англійською мовою",
    "esco": "Закупівля енергосервісу (ESCO)",
    "closeFrameworkAgreementUA": "Закупівля за рамковою угодою",
    "priceQuotation": "Запит пропозицій постачальників (електронний каталог)",
    "simple.defense": "Спрощені закупівлі для потреб оборони",
}


def default_date_from() -> str:
    d = date.today() - timedelta(days=365)
    return d.isoformat()


def default_date_to() -> str:
    return date.today().isoformat()


@dataclass
class SearchPreset:
    """Збережений набір фільтрів пошуку."""
    name: str = "За замовчуванням"
    date_from: str = field(default_factory=default_date_from)
    date_to: str = field(default_factory=default_date_to)
    cpv_prefixes: list[str] = field(default_factory=lambda: list(DEFAULT_CPV_PREFIXES))
    cpv_codes: list[str] = field(default_factory=list)   # конкретні коди, якщо обрано точково
    text: str = ""
    tenderers: list[str] = field(default_factory=list)   # ЄДРПОУ учасників (конкурентів)
    buyers: list[str] = field(default_factory=list)      # ЄДРПОУ замовників
    statuses: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    value_min: float | None = None
    value_max: float | None = None
    doc_scopes: list[str] = field(default_factory=lambda: list(DOC_SCOPES.keys()))
    #: Не качати відокремлені підписи КЕП (.p7s тощо) — вони не містять змісту
    #: і в типовій закупівлі це близько третини файлів.
    skip_signatures: bool = True
    #: Обмежити типи файлів (порожньо — усі, крім відсіяних вище).
    only_extensions: list[str] = field(default_factory=list)
    #: Типово збираємо лише дані: замовник, постачальник, суми та коди ДК021
    #: лежать у картці закупівлі, і для аналітики файли не потрібні.
    download_files: bool = False
    #: Додатково тягнути картки товарів з е-каталогу Prozorro Market — це єдине
    #: джерело технічних характеристик, фото та цінових діапазонів.
    collect_market: bool = True
    #: Брати лише чинні картки: за клас їх удвічі менше, а прострочені описують
    #: пропозицію, якої вже немає на ринку.
    market_active_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SearchPreset":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class Settings:
    output_dir: str = field(default_factory=lambda: str(default_output_dir()))
    #: Наші ЄДРПОУ — щоб відрізняти «нас» від конкурентів в аналітиці.
    own_edrpou: list[str] = field(default_factory=list)
    #: Конкуренти, яких відстежуємо постійно.
    competitors: list[str] = field(default_factory=list)

    search_concurrency: int = 6
    detail_concurrency: int = 8
    #: Сервер документів Prozorro віддає близько 30–40 КБ/с на потік, тож
    #: паралельність тут прямо впливає на швидкість. Вище восьми віддача вже
    #: не росте, а обривів стає помітно більше.
    download_concurrency: int = 8
    index_concurrency: int = 4
    request_timeout: int = 60
    max_retries: int = 4
    rate_limit_rps: float = 12.0

    #: Не качати файли, більші за N МБ (0 = без обмежень).
    max_file_mb: int = 0
    #: Пропускати вже завантажені файли (за хешем/розміром).
    skip_existing: bool = True
    #: Качати всі версії документа, а не лише останню.
    download_all_versions: bool = False
    #: Спосіб розпізнавання UUID: auto / contracts / index.
    resolve_mode: str = "auto"
    #: Зберігати повний JSON кожного тендера поруч із файлами.
    save_tender_json: bool = True
    #: Зберігати повний індекс tenderID→uuid (інакше — лише знайдені).
    keep_full_index: bool = True

    theme: str = "dark"
    preset: SearchPreset = field(default_factory=SearchPreset)
    saved_presets: list[dict] = field(default_factory=list)

    # --- (де)серіалізація -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["preset"] = self.preset.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        d = dict(d or {})
        preset = SearchPreset.from_dict(d.pop("preset", {}) or {})
        known = {f for f in cls.__dataclass_fields__}
        obj = cls(**{k: v for k, v in d.items() if k in known and k != "preset"})
        obj.preset = preset
        return obj

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or SETTINGS_FILE
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return cls.from_dict(json.load(fh))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or SETTINGS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        tmp.replace(path)
