"""Вибір кодів ДК021:2015 (CPV) деревом із пошуком."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ...core.classifiers import (
    ancestors_of, children_of, dk021, expand_prefixes, label_for, parent_of,
    roots, significant_prefix,
)
from .common import wrapped_label

ROLE_KEY = Qt.ItemDataRole.UserRole
ROLE_FILLED = Qt.ItemDataRole.UserRole + 1

#: Готові набори для типових ринків.
QUICK_SETS = [
    ("ІТ та офісна техніка", ["30"]),
    ("Комп'ютери, ноутбуки", ["30213"]),
    ("Монітори, периферія", ["30231"]),
    ("Оргтехніка", ["30232"]),
    ("Програмне забезпечення", ["48"]),
    ("Телекомунікації", ["32"]),
]


class CpvPicker(QWidget):
    """Дерево ДК021 з тристановими прапорцями.

    Джерело істини — множина обраних префіксів :attr:`_selected`; дерево лише
    її відображає. Позначений вузол означає «цей код і все, що під ним».
    """

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected: set[str] = set()
        self._guard = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Пошук за кодом або назвою — «моноблок», «ноутбук», 30213…")
        self.search.textChanged.connect(self._rebuild)
        top.addWidget(self.search, 1)
        clear = QPushButton("Очистити вибір")
        clear.clicked.connect(self.clear_selection)
        top.addWidget(clear)
        layout.addLayout(top)

        quick_label = QLabel("Швидкий вибір:")
        quick_label.setObjectName("Muted")
        layout.addWidget(quick_label)
        quick = QGridLayout()
        quick.setSpacing(2)
        for i, (title, prefixes) in enumerate(QUICK_SETS):
            btn = QPushButton(title)
            btn.setObjectName("Link")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, p=prefixes: self.add_prefixes(p))
            quick.addWidget(btn, i // 3, i % 3)
        layout.addLayout(quick)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tree.itemExpanded.connect(self._on_expand)
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree, 1)

        self.summary = wrapped_label("")
        layout.addWidget(self.summary)

        self._rebuild()

    # --- стан вибору ------------------------------------------------------

    def _state_of(self, key: str) -> Qt.CheckState:
        if key in self._selected or any(a in self._selected for a in ancestors_of(key)):
            return Qt.CheckState.Checked
        if any(s.startswith(key) for s in self._selected):
            return Qt.CheckState.PartiallyChecked
        return Qt.CheckState.Unchecked

    def _select(self, key: str) -> None:
        if self._state_of(key) == Qt.CheckState.Checked:
            return
        self._selected = {s for s in self._selected if not s.startswith(key)}
        self._selected.add(key)
        self._collapse_full_parents(parent_of(key))

    def _collapse_full_parents(self, key: str) -> None:
        """Якщо обрано всіх дітей вузла — замінює їх самим вузлом."""
        while key:
            kids = children_of(key)
            if kids and all(k in self._selected for k in kids):
                self._selected -= set(kids)
                self._selected.add(key)
                key = parent_of(key)
            else:
                return

    def _deselect(self, key: str) -> None:
        self._selected = {s for s in self._selected if not s.startswith(key)}
        # Якщо позначений якийсь предок — розкладаємо його на гілки, крім нашої.
        for ancestor in ancestors_of(key):
            if ancestor in self._selected:
                self._selected.discard(ancestor)
                path = [key] + ancestors_of(key)
                path = path[:path.index(ancestor) + 1]
                for depth in range(len(path) - 1, 0, -1):
                    node, keep = path[depth], path[depth - 1]
                    for kid in children_of(node):
                        if kid != keep:
                            self._selected.add(kid)
                break

    # --- побудова дерева --------------------------------------------------

    def _rebuild(self) -> None:
        text = self.search.text().strip().lower()
        self._guard = True
        self.tree.clear()
        if text:
            matches = [(code, name) for code, name in dk021().items()
                       if text in code.lower() or text in name.lower()][:500]
            for code, name in matches:
                key = significant_prefix(code)
                item = QTreeWidgetItem(self.tree, [f"{code} — {name}"])
                self._init_item(item, key, leaf=True)
            self._guard = False
            self.summary.setText(
                f"Знайдено {len(matches)} код(ів) за запитом «{text}». "
                f"Позначте потрібні — вибір збережеться після очищення пошуку.")
            return
        for key in roots():
            item = QTreeWidgetItem(self.tree, [label_for(key)])
            self._init_item(item, key)
        self._guard = False
        self._update_summary()

    def _init_item(self, item: QTreeWidgetItem, key: str, *, leaf: bool = False) -> None:
        item.setData(0, ROLE_KEY, key)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, self._state_of(key))
        kids = [] if leaf else children_of(key)
        item.setData(0, ROLE_FILLED, not kids)
        if kids:
            QTreeWidgetItem(item, ["…"])          # заглушка для лінивого розкриття

    def _on_expand(self, item: QTreeWidgetItem) -> None:
        if item.data(0, ROLE_FILLED):
            return
        self._guard = True
        item.takeChildren()
        for key in children_of(item.data(0, ROLE_KEY)):
            child = QTreeWidgetItem(item, [label_for(key)])
            self._init_item(child, key)
        item.setData(0, ROLE_FILLED, True)
        self._guard = False

    def _refresh_states(self) -> None:
        """Оновлює прапорці всіх уже показаних вузлів."""
        self._guard = True

        def walk(item: QTreeWidgetItem) -> None:
            key = item.data(0, ROLE_KEY)
            if key:
                item.setCheckState(0, self._state_of(key))
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))
        self._guard = False

    # --- події ------------------------------------------------------------

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._guard or column != 0:
            return
        key = item.data(0, ROLE_KEY)
        if not key:
            return
        if item.checkState(0) == Qt.CheckState.Checked:
            self._select(key)
        else:
            self._deselect(key)
        self._refresh_states()
        self._update_summary()
        self.changed.emit()

    # --- зовнішній інтерфейс ---------------------------------------------

    def selected_prefixes(self) -> list[str]:
        return sorted(self._selected)

    def set_prefixes(self, prefixes: list[str]) -> None:
        self._selected = {significant_prefix(p) for p in (prefixes or []) if str(p).strip()}
        self._selected.discard("")
        self._refresh_states()
        self._update_summary()
        self.changed.emit()

    def add_prefixes(self, prefixes: list[str]) -> None:
        for prefix in prefixes:
            key = significant_prefix(prefix)
            if key:
                self._select(key)
        self._refresh_states()
        self._update_summary()
        self.changed.emit()

    def clear_selection(self) -> None:
        self._selected.clear()
        self.search.clear()
        self._rebuild()
        self.changed.emit()

    def _update_summary(self) -> None:
        if not self._selected:
            self.summary.setText("Нічого не обрано — пошук піде без фільтра за ДК021.")
            return
        codes = expand_prefixes(self.selected_prefixes())
        names = ", ".join(label_for(p).split(" — ")[0] for p in sorted(self._selected)[:6])
        more = f" та ще {len(self._selected) - 6}" if len(self._selected) > 6 else ""
        self.summary.setText(
            f"Обрано гілок: {len(self._selected)} ({names}{more}) → {len(codes)} конкретних кодів. "
            f"Портал шукає за точним кодом, тож це {len(codes)} запит(ів) до пошуку.")
