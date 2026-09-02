"""Сторінка «Пошук і завантаження»: фільтри, запуск, поступ."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QVBoxLayout, QWidget,
)

from ...config import (
    DOC_SCOPES, METHOD_HINTS, METHOD_LABELS, STATUS_LABELS, SearchPreset,
)
from ...core.classifiers import regions
from ...core.downloader import DOCUMENT_EXTENSIONS, TEXT_EXTENSIONS
from ..widgets.common import (
    Card, CheckGrid, DateRange, EdrpouList, MoneyEdit, StatTile, wrapped_label,
)
from ..widgets.cpv_picker import CpvPicker


class SearchPage(QWidget):
    """Складання фільтра та керування запуском."""

    start_download = Signal()
    start_count = Signal()
    stop_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        #: Яку з двох кнопок натиснули останньою — від цього залежить, чи качати файли.
        self._with_files = False
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 16)
        root.setSpacing(14)

        title = QLabel("Пошук і завантаження")
        title.setObjectName("PageTitle")
        root.addWidget(title)
        root.addWidget(wrapped_label(
            "Задайте фільтри й натисніть «Зібрати дані» — на виході буде одна таблиця. "
            "«Зібрати файли» додатково завантажить документи закупівель у теку.",
            "PageHint"))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left, right = self._build_left(), self._build_right()
        left.setMinimumWidth(525)
        right.setMinimumWidth(380)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([540, 660])
        root.addWidget(splitter, 1)

        root.addWidget(self._build_actions())

    # --- ліва колонка: фільтри -------------------------------------------

    def _build_left(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 10, 0)
        lay.setSpacing(12)

        period = Card("Період", "За датою оприлюднення закупівлі.")
        self.dates = DateRange()
        period.add(self.dates)
        lay.addWidget(period)

        parties = Card("Учасники ринку",
                       "ЄДРПОУ конкурентів — знайде всі закупівлі, де вони подавалися чи вигравали. "
                       "Можна вставити кілька кодів одразу.")
        parties.add(QLabel("Учасники (конкуренти)", objectName="Muted"))
        self.tenderers = EdrpouList("ЄДРПОУ учасника, напр. 12345678")
        parties.add(self.tenderers)
        parties.add(QLabel("Замовники", objectName="Muted"))
        self.buyers = EdrpouList("ЄДРПОУ замовника")
        parties.add(self.buyers)
        lay.addWidget(parties)

        text_card = Card("Текстовий запит", "Необов'язково. Шукає в назві, номері й реквізитах.")
        self.text = QLineEdit()
        self.text.setPlaceholderText("напр. моноблок, ноутбук, UA-2026-…")
        text_card.add(self.text)
        lay.addWidget(text_card)

        refine = Card("Уточнення")
        refine.add(QLabel("Статус закупівлі", objectName="Muted"))
        self.statuses = CheckGrid(STATUS_LABELS, columns=2)
        refine.add(self.statuses)
        refine.add(QLabel("Тип процедури", objectName="Muted"))
        self.methods = CheckGrid(METHOD_LABELS, columns=2, hints=METHOD_HINTS)
        refine.add(self.methods)

        refine.add(QLabel("Регіон замовника", objectName="Muted"))
        self.region = QComboBox()
        self.region.addItem("— будь-який —", "")
        for name in regions():
            self.region.addItem(name, name)
        self.region.setToolTip(
            "Регіон береться з адреси замовника. Частина замовників його не "
            "заповнює — такі закупівлі до вибірки за регіоном не потраплять.")
        refine.add(self.region)

        refine.add(QLabel("Очікувана вартість, грн", objectName="Muted"))
        amount_row = QHBoxLayout()
        amount_row.setSpacing(8)
        self.value_min = MoneyEdit("від")
        self.value_max = MoneyEdit("до")
        amount_row.addWidget(self.value_min, 1)
        amount_row.addWidget(self.value_max, 1)
        refine.add(amount_row)
        lay.addWidget(refine)

        files = Card("Що збирати")
        self.collect_market = QCheckBox("Додати картки товарів з е-каталогу Prozorro Market")
        self.collect_market.setToolTip(
            "Єдине джерело технічних характеристик, фото, штрихкодів і цінових "
            "діапазонів. Береться за тим самим класом ДК021, що й закупівлі.")
        files.add(self.collect_market)
        self.market_active_only = QCheckBox("     лише чинні картки товарів")
        self.market_active_only.setToolTip(
            "Прострочені картки описують пропозицію, якої вже немає на ринку. "
            "Разом із ними класу вдвічі більше.")
        files.add(self.market_active_only)
        files.add(wrapped_label(
            "Каталог дає бренд і модель, ~30 технічних параметрів на товар, "
            "гарантійний термін, фото та квартилі ціни. Перший збір класу триває "
            "довго, надалі оновлюються лише змінені картки."))
        self.collect_market.toggled.connect(self.market_active_only.setEnabled)

        files.add(wrapped_label(
            "Нижче — налаштування для кнопки «Зібрати файли». На збір даних вони "
            "не впливають."))

        self.files_box = QWidget()
        files_lay = QVBoxLayout(self.files_box)
        files_lay.setContentsMargins(0, 0, 0, 0)
        files_lay.setSpacing(10)
        files.add(self.files_box)

        self.scopes = CheckGrid(DOC_SCOPES, columns=1)
        files_lay.addWidget(self.scopes)

        self.skip_signatures = QCheckBox("Пропускати файли електронного підпису (.p7s)")
        self.skip_signatures.setToolTip(
            "Відокремлені підписи КЕП не містять змісту, але це близько третини "
            "файлів закупівлі та п'ята частина обсягу.")
        files_lay.addWidget(self.skip_signatures)

        ext_label = QLabel("Лише ці типи файлів (через кому, порожньо — усі)")
        ext_label.setObjectName("Muted")
        files_lay.addWidget(ext_label)
        self.extensions = QLineEdit()
        self.extensions.setPlaceholderText("напр. pdf, docx")
        files_lay.addWidget(self.extensions)

        ext_row = QHBoxLayout()
        ext_row.setSpacing(4)
        for title, value, hint in (
            ("PDF і Word", TEXT_EXTENSIONS,
             "Лише текстові документи. Архіви буде пропущено, а в них часто "
             "лежить уся пропозиція учасника."),
            ("Документи й архіви", DOCUMENT_EXTENSIONS,
             "Текст, таблиці та архіви (zip, rar, 7z) — рекомендовано"),
            ("Усі типи", [], "Без обмеження за типом файлу"),
        ):
            btn = QPushButton(title)
            btn.setObjectName("Link")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(hint)
            btn.clicked.connect(lambda _=False, v=value: self.extensions.setText(", ".join(v)))
            ext_row.addWidget(btn)
        ext_row.addStretch(1)
        files_lay.addLayout(ext_row)
        lay.addWidget(files)

        lay.addStretch(1)
        area.setWidget(holder)
        return area

    # --- права колонка: ДК021 --------------------------------------------

    def _build_right(self) -> QWidget:
        card = Card("Класифікатор ДК021:2015 (CPV)",
                    "Позначений вузол означає «цей код і все, що під ним». "
                    "Нічого не обрано — шукаємо без обмеження за класом.")
        self.cpv = CpvPicker()
        card.add(self.cpv)
        return card

    # --- нижня панель дій -------------------------------------------------

    def _build_actions(self) -> QWidget:
        card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)

        self.tile_found = StatTile("Знайдено закупівель")
        self.tile_loaded = StatTile("Завантажено карток")
        self.tile_files = StatTile("Файлів збережено")
        self.tile_size = StatTile("Обсяг")
        for tile in (self.tile_found, self.tile_loaded, self.tile_files, self.tile_size):
            row.addWidget(tile)
        card.add(row)

        self.progress = QProgressBar()
        self.progress.setFormat("Готово до роботи")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        card.add(self.progress)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.status = QLabel("")
        self.status.setObjectName("Muted")
        buttons.addWidget(self.status, 1)

        self.btn_count = QPushButton("Порахувати")
        self.btn_count.setToolTip("Швидкий пошук без збору: скільки закупівель у фільтрі")
        self.btn_count.clicked.connect(self.start_count.emit)
        buttons.addWidget(self.btn_count)

        # Режим визначає натиснута кнопка, окремого перемикача немає.
        self.btn_start = QPushButton("Зібрати дані")
        self.btn_start.setObjectName("Primary")
        self.btn_start.setToolTip(
            "Замовник, постачальник, суми, коди ДК021, учасники та технічні "
            "характеристики товарів. На виході одна таблиця Excel, на диск нічого "
            "не пишеться. Сотня закупівель — близько хвилини.")
        self.btn_start.clicked.connect(lambda: self._start(with_files=False))
        buttons.addWidget(self.btn_start)

        self.btn_files = QPushButton("Зібрати файли")
        self.btn_files.setToolTip(
            "Те саме плюс документи закупівель у теку на диску: вимоги, "
            "специфікації, договори. Десятки-сотні мегабайт на сотню закупівель "
            "і години часу — сервер документів віддає близько 30 КБ/с на потік.")
        self.btn_files.clicked.connect(lambda: self._start(with_files=True))
        buttons.addWidget(self.btn_files)

        self.btn_stop = QPushButton("Зупинити")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        buttons.addWidget(self.btn_stop)
        card.add(buttons)
        return card

    def _start(self, *, with_files: bool) -> None:
        self._with_files = with_files
        self.start_download.emit()

    # --- обмін з налаштуваннями ------------------------------------------

    def load_preset(self, preset: SearchPreset) -> None:
        self.dates.set_values(preset.date_from, preset.date_to)
        self.cpv.set_prefixes(list(preset.cpv_prefixes or []) + list(preset.cpv_codes or []))
        self.text.setText(preset.text or "")
        self.tenderers.set_values(preset.tenderers)
        self.buyers.set_values(preset.buyers)
        self.statuses.set_values(preset.statuses)
        self.methods.set_values(preset.methods)
        self.scopes.set_values(preset.doc_scopes or list(DOC_SCOPES))
        region = (preset.regions or [""])[0]
        index = self.region.findData(region)
        self.region.setCurrentIndex(max(0, index))
        self.value_min.set_value(preset.value_min)
        self.value_max.set_value(preset.value_max)
        self.skip_signatures.setChecked(preset.skip_signatures)
        self.extensions.setText(", ".join(preset.only_extensions or []))
        self._with_files = bool(preset.download_files)
        self.collect_market.setChecked(preset.collect_market)
        self.market_active_only.setChecked(preset.market_active_only)
        self.market_active_only.setEnabled(preset.collect_market)

    def to_preset(self) -> SearchPreset:
        self.dates.normalize()
        date_from, date_to = self.dates.values()
        region = self.region.currentData() or ""
        low, high = self.value_min.value(), self.value_max.value()
        if low is not None and high is not None and low > high:
            low, high = high, low
            self.value_min.set_value(low)
            self.value_max.set_value(high)
        return SearchPreset(
            date_from=date_from,
            date_to=date_to,
            cpv_prefixes=self.cpv.selected_prefixes(),
            cpv_codes=[],
            text=self.text.text().strip(),
            tenderers=self.tenderers.values(),
            buyers=self.buyers.values(),
            statuses=self.statuses.values(),
            methods=self.methods.values(),
            regions=[region] if region else [],
            value_min=low,
            value_max=high,
            doc_scopes=self.scopes.values(),
            skip_signatures=self.skip_signatures.isChecked(),
            only_extensions=[e.strip().lstrip(".").lower()
                             for e in self.extensions.text().replace(";", ",").split(",")
                             if e.strip()],
            download_files=self._with_files,
            collect_market=self.collect_market.isChecked(),
            market_active_only=self.market_active_only.isChecked(),
        )

    # --- стан під час роботи ---------------------------------------------

    def set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_files.setEnabled(not running)
        self.btn_count.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        if running:
            # Перший звіт про поступ прийде аж після першої сторінки видачі,
            # а до того вікно показувало б «Готово до роботи» — наче ніщо й
            # не запустилося. Тому вмикаємо «живу» смугу одразу.
            self.progress.setRange(0, 0)
            self.progress.setFormat("Запускаємо…")
        else:
            self.progress.setRange(0, 100)
            self.progress.setFormat("Готово до роботи")
            self.progress.setValue(0)

    def set_progress(self, stage: str, done: int, total: int) -> None:
        if total <= 0:
            self.progress.setRange(0, 0)
            self.progress.setFormat(stage)
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)
        percent = int(done / total * 100)
        # Пробіл у тисячах ставимо тільки числам. Назва етапу приходить із
        # ядра й складається на льоту — кома в ній перетворилася б на пробіл
        # разом з усім рядком, як це вже бувало в журналі.
        counters = (f"{done:,}".replace(",", " "), f"{total:,}".replace(",", " "))
        self.progress.setFormat(f"{stage} — {counters[0]} / {counters[1]}  ({percent}%)")

    def set_stats(self, stats: dict) -> None:
        self.tile_found.set(f"{stats.get('found', 0):,}".replace(",", " "))
        self.tile_loaded.set(f"{stats.get('tenders_loaded', 0):,}".replace(",", " "))
        self.tile_files.set(f"{stats.get('files_ok', 0):,}".replace(",", " "))
        size = stats.get("bytes", 0) or 0
        self.tile_size.set(f"{size / 1048576:.1f} МБ" if size < 1073741824
                           else f"{size / 1073741824:.2f} ГБ")
        self.status.setText(
            f"запитів: {stats.get('requests', 0)}  ·  повторів: {stats.get('retries', 0)}"
            f"  ·  помилок: {stats.get('errors', 0)}")


