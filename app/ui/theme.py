"""Оформлення інтерфейсу: палітри та таблиця стилів."""
from __future__ import annotations

DARK = {
    "bg": "#15171c",
    "surface": "#1c1f26",
    "surface2": "#232731",
    "surface3": "#2b303b",
    "border": "#343a47",
    "text": "#e7e9ef",
    "muted": "#98a1b2",
    "accent": "#3d7eff",
    "accent_hover": "#5a92ff",
    "accent_soft": "#1e2c4d",
    "ok": "#3ecf8e",
    "warn": "#f0b429",
    "err": "#ef5f5f",
    "sel": "#26314a",
}

LIGHT = {
    "bg": "#f3f5f9",
    "surface": "#ffffff",
    "surface2": "#eef1f6",
    "surface3": "#e3e8f0",
    "border": "#d3dae5",
    "text": "#1b2027",
    "muted": "#5f6b7d",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_soft": "#dde8ff",
    "ok": "#12905c",
    "warn": "#a86a00",
    "err": "#c62f2f",
    "sel": "#d9e6ff",
}


def palette(name: str) -> dict:
    return LIGHT if name == "light" else DARK


QSS = """
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
    color: {text};
}}
QWidget#Root, QMainWindow {{ background: {bg}; }}

/* --- бічна навігація --- */
QFrame#Sidebar {{
    background: {surface};
    border-right: 1px solid {border};
}}
QLabel#Brand {{
    font-size: 16px; font-weight: 700; color: {text};
    padding: 18px 16px 4px 18px;
}}
QLabel#BrandSub {{
    font-size: 11px; color: {muted}; padding: 0 18px 14px 18px;
}}
QPushButton#NavButton {{
    text-align: left; padding: 10px 16px; border: none; border-radius: 8px;
    background: transparent; color: {muted}; font-size: 13.5px; margin: 2px 10px;
}}
QPushButton#NavButton:hover {{ background: {surface2}; color: {text}; }}
QPushButton#NavButton:checked {{ background: {accent_soft}; color: {accent}; font-weight: 600; }}

/* --- картки й заголовки --- */
QFrame#Card {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 12px;
}}
QLabel#PageTitle {{ font-size: 20px; font-weight: 700; padding-bottom: 2px; }}
QLabel#PageHint  {{ color: {muted}; font-size: 12px; }}
QLabel#SectionTitle {{ font-size: 13px; font-weight: 600; color: {text}; }}
QLabel#Muted {{ color: {muted}; }}
QLabel#StatValue {{ font-size: 19px; font-weight: 700; }}
QLabel#StatLabel {{ font-size: 11px; color: {muted}; }}

/* --- елементи вводу --- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {{
    background: {surface2}; border: 1px solid {border}; border-radius: 8px;
    padding: 7px 10px; selection-background-color: {accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QDateEdit:focus, QComboBox:focus {{ border: 1px solid {accent}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {surface2}; border: 1px solid {border};
    selection-background-color: {sel}; outline: none;
}}
QDateEdit::down-arrow, QComboBox::down-arrow {{ width: 0; height: 0; }}

/* --- кнопки --- */
QPushButton {{
    background: {surface3}; border: 1px solid {border}; border-radius: 8px;
    padding: 8px 16px; font-weight: 500;
}}
QPushButton:hover {{ background: {surface2}; border-color: {accent}; }}
QPushButton:disabled {{ color: {muted}; background: {surface2}; border-color: {border}; }}
QPushButton#Primary {{
    background: {accent}; border: 1px solid {accent}; color: #ffffff; font-weight: 600;
    padding: 10px 22px;
}}
QPushButton#Primary:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#Primary:disabled {{ background: {surface3}; border-color: {border}; color: {muted}; }}
QPushButton#Danger {{ border-color: {err}; color: {err}; }}
QPushButton#Danger:hover {{ background: {err}; color: #ffffff; }}
QPushButton#Danger:disabled {{ border-color: {border}; color: {muted}; background: {surface2}; }}
QPushButton#Link:disabled {{ color: {muted}; }}
QPushButton#Link {{
    background: transparent; border: none; color: {accent}; padding: 4px 6px;
    text-decoration: underline;
}}

/* --- прапорці --- */
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator, QTreeWidget::indicator {{
    width: 16px; height: 16px; border: 1px solid {border};
    border-radius: 4px; background: {surface2};
}}
QCheckBox::indicator:checked, QTreeWidget::indicator:checked {{
    background: {accent}; border-color: {accent}; image: none;
}}
QTreeWidget::indicator:indeterminate {{ background: {muted}; border-color: {muted}; }}

/* --- таблиці й дерева --- */
QTableView, QTreeWidget, QTreeView, QListWidget {{
    background: {surface}; border: 1px solid {border}; border-radius: 10px;
    gridline-color: {border}; alternate-background-color: {surface2};
    selection-background-color: {sel}; selection-color: {text}; outline: none;
}}
QTableView::item, QTreeWidget::item, QListWidget::item {{ padding: 5px 6px; }}
QHeaderView::section {{
    background: {surface2}; border: none; border-bottom: 1px solid {border};
    border-right: 1px solid {border}; padding: 8px 8px; font-weight: 600; color: {muted};
}}
QTableCornerButton::section {{ background: {surface2}; border: none; }}

/* --- поступ --- */
QProgressBar {{
    background: {surface2}; border: 1px solid {border}; border-radius: 8px;
    height: 18px; text-align: center; color: {text};
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 7px; }}

/* --- смуги прокручування --- */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {surface3}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {muted}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {surface3}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* --- інше --- */
QSplitter::handle {{ background: {border}; }}
QToolTip {{
    background: {surface3}; color: {text}; border: 1px solid {border};
    padding: 6px 8px; border-radius: 6px;
}}
QTabWidget::pane {{ border: 1px solid {border}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{
    background: transparent; padding: 8px 16px; color: {muted};
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; font-weight: 600; }}
QStatusBar {{ background: {surface}; border-top: 1px solid {border}; color: {muted}; }}
QGroupBox {{
    border: 1px solid {border}; border-radius: 10px; margin-top: 14px; padding: 12px 10px 8px 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {muted}; font-weight: 600;
}}
"""


def stylesheet(theme: str = "dark") -> str:
    return QSS.format(**palette(theme))
