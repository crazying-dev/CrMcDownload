"""界面主题：PCL 风格的浅色卡片、蓝色强调色与程序化图标。

不依赖任何图片资源：版本图标/项目图标都用 QPainter 画出来，
因此打包后也不会缺资源。
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap, QPolygonF

__all__ = [
    "ACCENT",
    "ACCENT_DARK",
    "ACCENT_SOFT",
    "BG",
    "BORDER",
    "CARD",
    "DANGER",
    "MUTED",
    "OK",
    "TEXT",
    "WARN",
    "app_qss",
    "block_pixmap",
    "human_count",
    "human_size",
    "human_date",
    "initial_pixmap",
    "loader_color",
]

BG = "#F1F4F9"
CARD = "#FFFFFF"
BORDER = "#E3E9F2"
TEXT = "#1F2530"
MUTED = "#7B8494"
ACCENT = "#2D6CDF"
ACCENT_DARK = "#1F55B8"
ACCENT_SOFT = "#E8F0FE"
OK = "#2E9E6B"
WARN = "#D18B22"
DANGER = "#D24B45"

#: 加载器/内容类型的标识色
LOADER_COLORS = {
    "Fabric": "#D9A441",
    "Forge": "#5A6B8C",
    "NeoForge": "#C96A3E",
    "Quilt": "#9B6BD6",
    "原版": "#4C8C6A",
    "未知": "#8A93A3",
}

PALETTE = [
    "#2D6CDF",
    "#2E9E6B",
    "#C96A3E",
    "#7B5BD6",
    "#C4477C",
    "#3B93B8",
    "#8C7A3F",
    "#4C6B8A",
]


def _seed_value(seed: str) -> int:
    value = 0
    for char in seed or "?":
        value = (value * 131 + ord(char)) & 0xFFFFFFFF
    return value


def loader_color(loader: str) -> str:
    return LOADER_COLORS.get(loader or "", PALETTE[_seed_value(loader) % len(PALETTE)])


def block_pixmap(seed: str, size: int = 64, badge: str = "") -> QPixmap:
    """画一个等轴测方块图标（草地方块风格），颜色由 ``seed`` 决定。"""
    ratio = 2
    pix = QPixmap(size * ratio, size * ratio)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ratio, ratio)

    base = QColor(PALETTE[_seed_value(seed) % len(PALETTE)])
    top = QColor(base).lighter(118)
    left = QColor(base).darker(112)
    right = QColor(base).darker(128)

    inset = size * 0.1
    width = size - inset * 2
    height = width * 0.56
    cx = size / 2
    top_y = size * 0.2
    mid_y = top_y + height
    bottom_y = mid_y + height * 0.92

    top_poly = QPolygonF(
        [
            QPointF(cx, top_y),
            QPointF(cx + width / 2, top_y + height / 2),
            QPointF(cx, top_y + height),
            QPointF(cx - width / 2, top_y + height / 2),
        ]
    )
    left_poly = QPolygonF(
        [
            QPointF(cx - width / 2, top_y + height / 2),
            QPointF(cx, top_y + height),
            QPointF(cx, bottom_y),
            QPointF(cx - width / 2, mid_y),
        ]
    )
    right_poly = QPolygonF(
        [
            QPointF(cx + width / 2, top_y + height / 2),
            QPointF(cx, top_y + height),
            QPointF(cx, bottom_y),
            QPointF(cx + width / 2, mid_y),
        ]
    )

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(top)
    painter.drawPolygon(top_poly)
    painter.setBrush(left)
    painter.drawPolygon(left_poly)
    painter.setBrush(right)
    painter.drawPolygon(right_poly)

    # 顶部高光
    gradient = QLinearGradient(cx - width / 2, top_y, cx + width / 2, top_y + height)
    gradient.setColorAt(0, QColor(255, 255, 255, 60))
    gradient.setColorAt(1, QColor(255, 255, 255, 0))
    painter.setBrush(gradient)
    painter.drawPolygon(top_poly)

    if badge:
        painter.setBrush(QColor("#FFFFFF"))
        painter.setPen(Qt.PenStyle.NoPen)
        radius = size * 0.17
        painter.drawEllipse(QRectF(size - radius * 2 - size * 0.02, size - radius * 2 - size * 0.02, radius * 2, radius * 2))
        painter.setPen(QColor(loader_color(badge)))
        font = QFont("Microsoft YaHei UI", int(size * 0.16))
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            QRectF(size - radius * 2 - size * 0.02, size - radius * 2 - size * 0.02, radius * 2, radius * 2),
            Qt.AlignmentFlag.AlignCenter,
            badge[:1].upper(),
        )
    painter.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def initial_pixmap(text: str, size: int = 44, seed: str = "") -> QPixmap:
    """圆角方块 + 首字母，用于模组/整合包图标占位。"""
    ratio = 2
    pix = QPixmap(size * ratio, size * ratio)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(ratio, ratio)
    base = QColor(PALETTE[_seed_value(seed or text) % len(PALETTE)])
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0, base.lighter(112))
    gradient.setColorAt(1, base.darker(118))
    painter.setBrush(gradient)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    painter.setPen(QColor("#FFFFFF"))
    font = QFont("Microsoft YaHei UI", int(size * 0.38))
    font.setBold(True)
    painter.setFont(font)
    letter = (text or "?").strip()[:1].upper() or "?"
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, letter)
    painter.end()
    pix.setDevicePixelRatio(ratio)
    return pix


def human_size(value: int | float) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.0f} GiB"


def human_count(value: int | float) -> str:
    number = float(value or 0)
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f} 亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f} 万"
    return str(int(number))


def human_date(stamp: float | str) -> str:
    if isinstance(stamp, str):
        return stamp[:10].replace("T", " ") or "-"
    if not stamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def app_qss() -> str:
    return f"""
QWidget {{
    background: transparent;
    color: {TEXT};
    font-family: "Microsoft YaHei UI";
    font-size: 10pt;
}}
QMainWindow, QDialog {{ background: {BG}; }}
QFrame#topbar {{
    background: {CARD};
    border-bottom: 1px solid {BORDER};
}}
QFrame#sidebar {{
    background: {CARD};
    border-right: 1px solid {BORDER};
}}
QFrame#card {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QFrame#softCard {{
    background: #F7F9FD;
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#tile {{
    background: #F7F9FD;
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#tile:hover {{
    background: {ACCENT_SOFT};
    border-color: #C9D8F5;
}}
QLabel#brand {{ font-size: 14pt; font-weight: 700; color: {TEXT}; }}
QLabel#brandSub {{ font-size: 8pt; color: {MUTED}; }}
QLabel#title {{ font-size: 19pt; font-weight: 700; }}
QLabel#h2 {{ font-size: 13pt; font-weight: 700; }}
QLabel#h3 {{ font-size: 11pt; font-weight: 600; }}
QLabel#muted {{ color: {MUTED}; font-size: 9pt; }}
QLabel#mutedSmall {{ color: {MUTED}; font-size: 8pt; }}
QLabel#heroName {{ font-size: 20pt; font-weight: 700; }}
QLabel#status {{ color: {MUTED}; font-size: 9pt; }}
QLabel#error {{ color: {DANGER}; font-size: 9pt; }}
QLabel#link {{ color: {ACCENT}; font-size: 9pt; }}
QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 14px;
}}
QPushButton:hover {{ background: #F2F6FE; border-color: #C9D8F5; }}
QPushButton:pressed {{ background: #E6EEFC; }}
QPushButton:disabled {{ color: #AEB6C4; background: #F6F8FB; border-color: {BORDER}; }}
QPushButton#nav {{
    background: transparent;
    border: none;
    border-radius: 9px;
    text-align: left;
    padding: 10px 14px;
    color: #4B5565;
    font-size: 10.5pt;
}}
QPushButton#nav:hover {{ background: #F1F5FD; color: {TEXT}; }}
QPushButton#topnav {{
    background: transparent;
    border: none;
    border-radius: 9px;
    text-align: center;
    padding: 8px 20px;
    color: #4B5565;
    font-size: 10.5pt;
}}
QPushButton#topnav:hover {{ background: #F1F5FD; color: {TEXT}; }}
QPushButton#topnav:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    font-weight: 700;
}}
QPushButton#nav:checked {{
    background: {ACCENT_SOFT};
    color: {ACCENT};
    font-weight: 700;
}}
QPushButton#accent {{
    background: {ACCENT};
    color: #FFFFFF;
    border: none;
    border-radius: 9px;
    font-weight: 700;
    padding: 9px 18px;
}}
QPushButton#accent:hover {{ background: #3D7BEC; }}
QPushButton#accent:pressed {{ background: {ACCENT_DARK}; }}
QPushButton#accent:disabled {{ background: #B9CDF0; color: #F4F7FD; }}
QPushButton#accentBig {{
    background: {ACCENT};
    color: #FFFFFF;
    border: none;
    border-radius: 11px;
    font-size: 14pt;
    font-weight: 700;
}}
QPushButton#accentBig:hover {{ background: #3D7BEC; }}
QPushButton#accentBig:pressed {{ background: {ACCENT_DARK}; }}
QPushButton#accentBig:disabled {{ background: #B9CDF0; color: #F4F7FD; }}
QPushButton#danger {{ color: {DANGER}; border-color: #F0C9C7; background: #FFF6F5; }}
QPushButton#danger:hover {{ background: #FDECEA; }}
QPushButton#segment {{
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 6px 16px;
    color: {MUTED};
}}
QPushButton#segment:hover {{ color: {TEXT}; background: #EEF3FB; }}
QPushButton#segment:checked {{
    background: {CARD};
    color: {ACCENT};
    font-weight: 700;
}}
QPushButton#tile {{
    background: #F7F9FD;
    border: 1px solid {BORDER};
    border-radius: 10px;
    text-align: left;
    padding: 10px 12px;
    color: {TEXT};
}}
QPushButton#tile:hover {{ background: {ACCENT_SOFT}; border-color: #C9D8F5; }}
QLineEdit, QSpinBox, QComboBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px; border-radius: 5px;
    border: 1px solid #C7D0DE; background: {CARD};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
}}
QTreeWidget, QListWidget {{
    background: {CARD};
    border: none;
    outline: none;
}}
QTreeWidget::item {{ padding: 7px 4px; border-radius: 7px; }}
QTreeWidget::item:hover {{ background: #F4F7FD; }}
QTreeWidget::item:selected {{ background: {ACCENT_SOFT}; color: {TEXT}; }}
QListWidget::item {{ border-radius: 7px; }}
QListWidget::item:hover {{ background: #F4F7FD; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; }}
QHeaderView::section {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 4px;
    font-size: 8.5pt;
}}
QProgressBar {{
    background: #E8EDF6;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    font-size: 8pt;
    color: transparent;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::groove:horizontal {{ height: 4px; background: #E3E9F2; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
    background: {CARD}; border: 2px solid {ACCENT};
}}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C9D2E1; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #AEBACD; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #C9D2E1; border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QPlainTextEdit {{ background: #0F1520; color: #D6E2F5; border-radius: 10px; padding: 8px; }}
QMenu {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px;
}}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT_SOFT}; }}
QToolTip {{
    background: #1F2530; color: #FFFFFF; border: none; border-radius: 6px; padding: 5px 8px;
}}
"""
