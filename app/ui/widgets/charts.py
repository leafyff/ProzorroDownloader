"""Графіки, намальовані вручну.

У проєкті свідомо стоїть ``PySide6-Essentials`` — повний PySide6 тягне понад
400 МБ Addons заради одного модуля QtCharts. Тому діаграми малюються прямо на
``QPainter``: стовпчики, смуги, кільце, лінії та точкова хмара. Цього набору
вистачає на весь звіт, а важить він нуль.

Кожен віджет отримує :class:`app.core.report.ChartData` — рушій аналітики
каже, *що* показати, а не *як*, — і сам вирішує компонування, підписи осей
і підказки під курсором.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ...core.report import ChartData, compact, money
from ..theme import palette

#: Кольори рядів. Порядок підібраний так, щоб сусідні ряди різнилися і за
#: тоном, і за яскравістю — графік лишається читабельним і в чорно-білому друці.
SERIES_COLORS = [
    "#3d7eff", "#3ecf8e", "#f0b429", "#a78bfa", "#22d3ee",
    "#fb923c", "#f472b6", "#84cc16", "#e879f9", "#94a3b8",
]
#: Колір для виділених значень — наші ТОВ у рейтингах.
OWN_COLOR = "#3ecf8e"
#: Колір викидів у точковій хмарі.
OUTLIER_COLOR = "#ef5f5f"


def _fmt(value: float, chart: ChartData) -> str:
    if chart.unit == "%":
        return f"{value:.1f}".replace(".", ",") + "%"
    if chart.money_axis:
        return compact(value)
    return money(value)


def _nice_step(span: float, ticks: int = 4, whole: bool = False) -> float:
    """Крок сітки з «людського» ряду 1-2-5-10, щоб підписи були круглими.

    ``whole`` потрібен там, де значення — це штуки: крок 0,25 дав би підписи
    «0 0 1 1 1», бо дробові поділки округляються до тих самих цілих.
    """
    if span <= 0:
        return 1.0
    raw = span / max(ticks, 1)
    power = 10 ** math.floor(math.log10(raw))
    step = power * 10
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= power * factor:
            step = power * factor
            break
    return max(1.0, round(step)) if whole else step


def _is_whole(series) -> bool:
    """Чи всі значення рядів цілі — тоді й вісь має бути цілою."""
    return all(float(v).is_integer() for s in series for v in s.values)


class ChartBase(QWidget):
    """Спільна основа: палітра, підказки, службове малювання."""

    #: Типова висота графіка цього типу.
    HEIGHT = 260

    def __init__(self, data: ChartData, theme: str = "dark", parent=None):
        super().__init__(parent)
        self.data = data
        self.set_theme(theme)
        self.setMinimumHeight(self.HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        self.setMouseTracking(True)
        self._hits: list[tuple[QRectF, str]] = []
        self._hot = -1

    def set_theme(self, theme: str) -> None:
        self.pal = palette(theme)
        self.update()

    # --- службове ---------------------------------------------------------

    def color(self, index: int) -> QColor:
        return QColor(SERIES_COLORS[index % len(SERIES_COLORS)])

    def _text_pen(self, muted: bool = False) -> QPen:
        return QPen(QColor(self.pal["muted"] if muted else self.pal["text"]))

    def _small(self, painter: QPainter, size: int = 10, bold: bool = False) -> QFontMetrics:
        font = QFont(painter.font())
        font.setPointSizeF(size)
        font.setBold(bold)
        painter.setFont(font)
        return QFontMetrics(font)

    def _empty(self, painter: QPainter) -> None:
        painter.setPen(self._text_pen(muted=True))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Немає даних")

    def _grid(self, painter: QPainter, plot: QRectF, top: float, step: float,
              horizontal: bool = True) -> None:
        pen = QPen(QColor(self.pal["border"]))
        pen.setWidth(1)
        painter.setPen(pen)
        self._small(painter, 9)
        value = 0.0
        while value <= top + step / 2:
            fraction = value / top if top else 0
            if horizontal:
                y = plot.bottom() - fraction * plot.height()
                painter.setPen(QPen(QColor(self.pal["border"])))
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                painter.setPen(self._text_pen(muted=True))
                text = _fmt(value, self.data)
                painter.drawText(QRectF(0, y - 9, plot.left() - 6, 18),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                                 text)
            else:
                x = plot.left() + fraction * plot.width()
                painter.setPen(QPen(QColor(self.pal["border"])))
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                painter.setPen(self._text_pen(muted=True))
                text = _fmt(value, self.data)
                painter.drawText(QRectF(x - 45, plot.bottom() + 2, 90, 14),
                                 int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                                 text)
            value += step

    def _legend(self, painter: QPainter, rect: QRectF) -> float:
        """Малює легенду рядів угорі. Повертає використану висоту."""
        series = [s for s in self.data.series if s.name]
        if len(series) < 2:
            return 0.0
        metrics = self._small(painter, 9)
        x = rect.left()
        y = rect.top() + 6
        for i, item in enumerate(series):
            painter.setBrush(self.color(i))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(x, y - 4, 10, 10), 2, 2)
            painter.setPen(self._text_pen(muted=True))
            painter.drawText(QPointF(x + 15, y + 5), item.name)
            x += 15 + metrics.horizontalAdvance(item.name) + 18
        return 20.0

    # --- підказки ---------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:
        position = event.position()
        for index, (rect, text) in enumerate(self._hits):
            if rect.contains(position):
                if index != self._hot:
                    self._hot = index
                    self.update()
                QToolTip.showText(event.globalPosition().toPoint(), text, self)
                return
        if self._hot != -1:
            self._hot = -1
            self.update()
        QToolTip.hideText()

    def leaveEvent(self, event) -> None:
        self._hot = -1
        QToolTip.hideText()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self._hits = []
        try:
            self.draw(painter)
        finally:
            painter.end()

    def draw(self, painter: QPainter) -> None:      # перевизначається
        self._empty(painter)


#: Кут нахилу довгих підписів під віссю і максимальна їхня довжина.
TILT = 40
TILT_WIDTH = 118


class BarChart(ChartBase):
    """Вертикальні стовпчики; кілька рядів стоять поруч у групі."""

    HEIGHT = 300

    def draw(self, painter: QPainter) -> None:
        chart = self.data
        series = [s for s in chart.series if s.values]
        if not series:
            return self._empty(painter)
        labels = series[0].labels
        if not labels:
            return self._empty(painter)

        metrics = self._small(painter, 9)
        legend_height = self._legend(painter, QRectF(self.rect()))
        # Довгі підписи кладемо під кутом — інакше вони зливаються.
        longest = max((metrics.horizontalAdvance(str(l)) for l in labels), default=0)
        slot = max((self.width() - 80) / max(len(labels), 1), 1)
        rotated = longest > slot - 6
        bottom = int(TILT_WIDTH * math.sin(math.radians(TILT))) + 18 if rotated else 26
        plot = QRectF(64, 10 + legend_height, max(self.width() - 76, 40),
                      max(self.height() - bottom - 12 - legend_height, 40))

        top = max((max(s.values) for s in series if s.values), default=0) or 1
        step = _nice_step(top, whole=_is_whole(series))
        top = math.ceil(top / step) * step
        self._grid(painter, plot, top, step)

        groups = len(labels)
        group_width = plot.width() / max(groups, 1)
        bar_gap = min(6.0, group_width * 0.12)
        bar_width = max((group_width - bar_gap * 2) / len(series), 2.0)

        for gi, label in enumerate(labels):
            base_x = plot.left() + gi * group_width + bar_gap
            for si, item in enumerate(series):
                value = item.values[gi] if gi < len(item.values) else 0
                height = (value / top) * plot.height() if top else 0
                rect = QRectF(base_x + si * bar_width, plot.bottom() - height,
                              max(bar_width - 1, 1.5), max(height, 0.5))
                color = QColor(OWN_COLOR) if gi in item.accent else self.color(si)
                if len(self._hits) == self._hot:
                    color = color.lighter(125)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(rect, 3, 3)
                name = f"{item.name}: " if item.name and len(series) > 1 else ""
                self._hits.append((rect.adjusted(-2, -4, 2, 4),
                                   f"{label}\n{name}{_fmt(value, chart)} {chart.unit}".strip()))

            painter.setPen(self._text_pen(muted=True))
            text = str(label)
            if rotated:
                # Зсув рахуємо по вже вкороченому тексту: інакше довгий підпис
                # їде вліво за межі віджета й обрізається з початку.
                shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, TILT_WIDTH)
                painter.save()
                painter.translate(base_x + group_width / 2 - bar_gap, plot.bottom() + 8)
                painter.rotate(-TILT)
                painter.drawText(QPointF(-metrics.horizontalAdvance(shown) - 2, 6), shown)
                painter.restore()
            else:
                painter.drawText(
                    QRectF(plot.left() + gi * group_width, plot.bottom() + 4, group_width, 18),
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                    metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(group_width)))


class HBarChart(ChartBase):
    """Горизонтальні смуги — для рейтингів, де підписи довгі."""

    #: Смуга, вища за це, виглядає не діаграмою, а плямою: коли рядів мало,
    #: висота графіка стискається, а самі смуги лишаються нормальними.
    MAX_ROW = 44

    def __init__(self, data: ChartData, theme: str = "dark", parent=None):
        super().__init__(data, theme, parent)
        rows = len(data.series[0].labels) if data.series else 0
        self.setMinimumHeight(self._needed(rows))

    def _needed(self, rows: int) -> int:
        return max(120, 28 + rows * min(self.MAX_ROW, 26))

    def draw(self, painter: QPainter) -> None:
        chart = self.data
        series = [s for s in chart.series if s.values]
        if not series or not series[0].labels:
            return self._empty(painter)
        labels = series[0].labels
        metrics = self._small(painter, 9)
        legend_height = self._legend(painter, QRectF(self.rect()))

        label_width = min(max((metrics.horizontalAdvance(str(l)) for l in labels), default=60) + 10,
                          self.width() * 0.42)
        plot = QRectF(label_width + 8, 6 + legend_height,
                      max(self.width() - label_width - 88, 40),
                      max(self.height() - 24 - legend_height, 30))
        top = max((max(s.values) for s in series if s.values), default=0) or 1

        rows = len(labels)
        row_height = min(plot.height() / max(rows, 1), float(self.MAX_ROW))
        # Коли рядів мало, а місця багато, групу смуг центруємо, а не
        # розтягуємо на всю висоту.
        offset = max((plot.height() - row_height * rows) / 2, 0.0)
        bar_height = max((row_height - 6) / len(series), 3.0)

        for ri, label in enumerate(labels):
            y0 = plot.top() + offset + ri * row_height + 3
            painter.setPen(self._text_pen(muted=True))
            painter.drawText(QRectF(0, y0, label_width, row_height - 6),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(str(label), Qt.TextElideMode.ElideRight,
                                                int(label_width)))
            for si, item in enumerate(series):
                value = item.values[ri] if ri < len(item.values) else 0
                width = (value / top) * plot.width() if top else 0
                rect = QRectF(plot.left(), y0 + si * bar_height, max(width, 1.0),
                              max(bar_height - 1, 2.0))
                color = QColor(OWN_COLOR) if ri in item.accent else self.color(si)
                if len(self._hits) == self._hot:
                    color = color.lighter(125)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                painter.drawRoundedRect(rect, 3, 3)
                painter.setPen(self._text_pen(muted=True))
                painter.drawText(QRectF(rect.right() + 6, rect.top() - 2, 80, rect.height() + 4),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                                 _fmt(value, chart))
                name = f"{item.name}: " if item.name and len(series) > 1 else ""
                self._hits.append((rect.adjusted(0, -2, 60, 2),
                                   f"{label}\n{name}{_fmt(value, chart)} {chart.unit}".strip()))

    def sizeHint(self):
        hint = super().sizeHint()
        rows = len(self.data.series[0].labels) if self.data.series else 0
        hint.setHeight(self._needed(rows))
        return hint

    def minimumSizeHint(self):
        return self.sizeHint()


class PieChart(ChartBase):
    """Кільцева діаграма з легендою праворуч."""

    HEIGHT = 300

    def draw(self, painter: QPainter) -> None:
        chart = self.data
        if not chart.series or not chart.series[0].values:
            return self._empty(painter)
        item = chart.series[0]
        values = [max(v, 0) for v in item.values]
        total = sum(values)
        if total <= 0:
            return self._empty(painter)

        size = min(self.height() - 20, self.width() * 0.45)
        circle = QRectF(14, (self.height() - size) / 2, size, size)
        inner = circle.adjusted(size * 0.28, size * 0.28, -size * 0.28, -size * 0.28)

        start = 90 * 16
        for i, value in enumerate(values):
            span = -int(round(value / total * 360 * 16))
            path = QPainterPath()
            path.moveTo(circle.center())
            path.arcTo(circle, start / 16, span / 16)
            path.closeSubpath()
            hole = QPainterPath()
            hole.addEllipse(inner)
            path = path.subtracted(hole)
            color = self.color(i)
            if len(self._hits) == self._hot:
                color = color.lighter(120)
            painter.setPen(QPen(QColor(self.pal["surface"]), 1))
            painter.setBrush(color)
            painter.drawPath(path)
            self._hits.append((path.boundingRect(),
                               f"{item.labels[i] if i < len(item.labels) else ''}\n"
                               f"{_fmt(value, chart)} {chart.unit} — "
                               f"{value / total * 100:.1f}%".replace(".", ",")))
            start += span

        painter.setPen(self._text_pen())
        self._small(painter, 11, bold=True)
        painter.drawText(inner, Qt.AlignmentFlag.AlignCenter,
                         _fmt(total, chart) + ("\n" + chart.unit if chart.unit else ""))

        metrics = self._small(painter, 9)
        x = circle.right() + 18
        available = self.width() - x - 8
        line = 20.0
        y = (self.height() - min(len(values), 12) * line) / 2 + 4
        for i, value in enumerate(values[:12]):
            painter.setBrush(self.color(i))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(QRectF(x, y - 8, 10, 10), 2, 2)
            label = str(item.labels[i]) if i < len(item.labels) else ""
            painter.setPen(self._text_pen())
            share_text = f"  {value / total * 100:.1f}%".replace(".", ",")
            width = available - metrics.horizontalAdvance(share_text) - 20
            painter.drawText(QPointF(x + 16, y + 1),
                             metrics.elidedText(label, Qt.TextElideMode.ElideRight,
                                                max(int(width), 40)))
            painter.setPen(self._text_pen(muted=True))
            painter.drawText(QRectF(x + 16, y - 9, available - 16, 16),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             share_text.strip())
            y += line


class LineChart(ChartBase):
    """Лінії з маркерами — для динаміки по місяцях."""

    HEIGHT = 280

    def draw(self, painter: QPainter) -> None:
        chart = self.data
        series = [s for s in chart.series if s.values]
        if not series or not series[0].labels:
            return self._empty(painter)
        labels = series[0].labels
        metrics = self._small(painter, 9)
        legend_height = self._legend(painter, QRectF(self.rect()))
        plot = QRectF(64, 10 + legend_height, max(self.width() - 76, 40),
                      max(self.height() - 42 - legend_height, 40))

        top = max((max(s.values) for s in series if s.values), default=0) or 1
        step = _nice_step(top, whole=_is_whole(series))
        top = math.ceil(top / step) * step
        self._grid(painter, plot, top, step)

        points = max(len(labels) - 1, 1)
        dx = plot.width() / points

        for si, item in enumerate(series):
            color = self.color(si)
            path = QPainterPath()
            for i, value in enumerate(item.values):
                x = plot.left() + i * dx
                y = plot.bottom() - (value / top) * plot.height()
                if i == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(color)
            pen.setWidthF(2.2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(path)

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            for i, value in enumerate(item.values):
                x = plot.left() + i * dx
                y = plot.bottom() - (value / top) * plot.height()
                radius = 4.5 if len(self._hits) == self._hot else 3.2
                painter.drawEllipse(QPointF(x, y), radius, radius)
                name = f"{item.name}: " if item.name else ""
                self._hits.append((QRectF(x - 9, y - 9, 18, 18),
                                   f"{labels[i] if i < len(labels) else ''}\n"
                                   f"{name}{_fmt(value, chart)} {chart.unit}".strip()))

        painter.setPen(self._text_pen(muted=True))
        stride = max(1, int(len(labels) * 56 / max(plot.width(), 1)))
        for i, label in enumerate(labels):
            if i % stride:
                continue
            x = plot.left() + i * dx
            # Крайні підписи притискаємо до країв віджета, інакше останній
            # місяць виїжджає за межу й обрізається на півслові.
            left = min(max(x - 40, 0.0), self.width() - 80.0)
            painter.drawText(QRectF(left, plot.bottom() + 6, 80, 16),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                             metrics.elidedText(str(label), Qt.TextElideMode.ElideRight, 78))


class ScatterChart(ChartBase):
    """Точкова хмара: обидві осі логарифмічні, викиди позначені окремо."""

    HEIGHT = 300

    def draw(self, painter: QPainter) -> None:
        chart = self.data
        if not chart.series or not chart.series[0].points:
            return self._empty(painter)
        item = chart.series[0]
        xs = [p[0] for p in item.points]
        ys = [p[1] for p in item.points]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        x_span = (x_max - x_min) or 1.0
        y_span = (y_max - y_min) or 1.0

        plot = QRectF(50, 12, max(self.width() - 62, 40), max(self.height() - 44, 40))
        pen = QPen(QColor(self.pal["border"]))
        painter.setPen(pen)
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(self._text_pen(muted=True))
        self._small(painter, 9)
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            value = 10 ** (y_min + y_span * i / 4)
            painter.drawText(QRectF(0, y - 8, plot.left() - 6, 16),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                             compact(value))
        for i in range(5):
            x = plot.left() + plot.width() * i / 4
            value = 10 ** (x_min + x_span * i / 4)
            left = min(max(x - 40, 0.0), self.width() - 80.0)
            painter.drawText(QRectF(left, plot.bottom() + 4, 80, 16),
                             int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                             compact(value))

        normal = QColor(self.pal["accent"])
        normal.setAlpha(120)
        painter.setPen(Qt.PenStyle.NoPen)
        for i, (px, py) in enumerate(item.points):
            x = plot.left() + (px - x_min) / x_span * plot.width()
            y = plot.bottom() - (py - y_min) / y_span * plot.height()
            if i in item.accent:
                painter.setBrush(QColor(OUTLIER_COLOR))
                painter.drawEllipse(QPointF(x, y), 3.6, 3.6)
            else:
                painter.setBrush(normal)
                painter.drawEllipse(QPointF(x, y), 2.4, 2.4)

        painter.setPen(self._text_pen(muted=True))
        painter.drawText(QRectF(plot.left(), plot.top() - 2, plot.width(), 14),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop),
                         f"точок: {len(item.points)}, з них викидів: {len(item.accent)}")


#: ``kind`` із :class:`ChartData` → віджет.
KINDS = {
    "bar": BarChart,
    "hist": BarChart,
    "hbar": HBarChart,
    "pie": PieChart,
    "line": LineChart,
    "area": LineChart,
    "scatter": ScatterChart,
}


def build(data: ChartData, theme: str = "dark", parent=None) -> ChartBase:
    """Створює віджет під тип графіка; невідомий тип падає у стовпчики."""
    return KINDS.get(data.kind, BarChart)(data, theme, parent)
