"""可复用的小部件：卡片、徽标、内容行。"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from offcraft.content.modrinth import ProjectHit
from offcraft.ui.theme import ACCENT, MUTED, TEXT, human_count, initial_pixmap

__all__ = ["Badge", "Card", "ComboPolisher", "ContentRow", "ElidedLabel", "Tile", "polish_combo"]


def polish_combo(combo: QComboBox) -> None:
    """去掉下拉列表默认的黑色外框（Qt 弹出视图自带 FrameShape + 系统窗口边）。"""
    view = combo.view()
    if view is None:
        return
    try:
        view.setFrameShape(QFrame.Shape.NoFrame)
        window = view.window()
        if window is not None and window is not view:
            # 弹出容器本身是横跨全屏的无名 QFrame，给了样式才会跟主题走
            window.setObjectName("comboPopup")
            window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            window.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint, True)
        combo.setMaxVisibleItems(14)
    except (AttributeError, RuntimeError):
        pass


class ComboPolisher(QObject):
    """全局事件过滤器：每个下拉框在首次绘制前都会被去掉黑边。"""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 命名
        if isinstance(obj, QComboBox) and event.type() in (
            QEvent.Type.Polish,
            QEvent.Type.Show,
        ):
            polish_combo(obj)
        return False


class Card(QFrame):
    """白底圆角卡片，带一个默认的竖直布局。"""

    def __init__(self, parent: QWidget | None = None, *, margins: tuple[int, int, int, int] = (18, 16, 18, 16)) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(10)

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self.body.addWidget(widget, stretch)

    def add_layout(self, layout, stretch: int = 0) -> None:
        self.body.addLayout(layout, stretch)


class Badge(QLabel):
    """圆角小标签。"""

    TONES = {
        "blue": ("#E9F1FE", ACCENT),
        "green": ("#E7F6EE", "#2E9E6B"),
        "amber": ("#FDF3E3", "#B47616"),
        "red": ("#FDECEA", "#C2453F"),
        "gray": ("#F1F3F7", "#6C7688"),
        "purple": ("#F2ECFD", "#7B5BD6"),
    }

    def __init__(self, text: str = "", tone: str = "gray", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        background, color = self.TONES.get(tone, self.TONES["gray"])
        self.setStyleSheet(
            f"background: {background}; color: {color}; border-radius: 8px;"
            "padding: 2px 8px; font-size: 8.5pt;"
        )

    def set(self, text: str, tone: str | None = None) -> None:
        self.setText(text)
        if tone:
            self.set_tone(tone)


class ElidedLabel(QLabel):
    """单行省略标签（保留完整文本作为 tooltip）。"""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)
        self.setToolTip(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 命名
        self._full = text
        metrics = self.fontMetrics()
        available = max(self.width(), 80)
        self.setToolTip(text)
        super().setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, available))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        metrics = self.fontMetrics()
        self.setToolTip(self._full)
        super().setText(metrics.elidedText(self._full, Qt.TextElideMode.ElideRight, max(self.width(), 60)))


class ContentRow(QWidget):
    """内容搜索结果行：图标、标题、简介、下载量。"""

    def __init__(self, hit: ProjectHit, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hit = hit
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 10, 7)
        layout.setSpacing(12)

        self.icon = QLabel()
        self.icon.setFixedSize(46, 46)
        self.icon.setPixmap(initial_pixmap(hit.title, 46, hit.slug or hit.id))
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon)

        text = QVBoxLayout()
        text.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        self.title = QLabel(hit.title)
        self.title.setStyleSheet(f"font-size: 10.5pt; font-weight: 700; color: {TEXT};")
        title_row.addWidget(self.title)
        self.kind = Badge(
            {"mod": "模组", "modpack": "整合包", "resourcepack": "资源包", "shader": "光影"}.get(
                hit.project_type, hit.project_type
            ),
            "blue",
        )
        title_row.addWidget(self.kind)
        title_row.addStretch(1)
        text.addLayout(title_row)
        self.desc = ElidedLabel(hit.description)
        self.desc.setStyleSheet(f"color: {MUTED}; font-size: 8.8pt;")
        text.addWidget(self.desc)
        layout.addLayout(text, 1)

        stats = QVBoxLayout()
        stats.setSpacing(2)
        self.downloads = QLabel(f"↓ {human_count(hit.downloads)}")
        self.downloads.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.downloads.setStyleSheet(f"color: {MUTED}; font-size: 8.5pt;")
        stats.addWidget(self.downloads)
        self.author = QLabel(hit.author or "")
        self.author.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.author.setStyleSheet(f"color: {MUTED}; font-size: 8.5pt;")
        stats.addWidget(self.author)
        layout.addLayout(stats)

    def set_icon(self, pixmap) -> None:
        self.icon.setPixmap(pixmap)


class Tile(QFrame):
    """可点击的快捷入口卡片（标题 + 说明 + 回调）。"""

    clicked = pyqtSignal()

    def __init__(
        self,
        title: str,
        hint: str = "",
        callback: Callable[[], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("tile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(56)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(
            f"font-size: 10.5pt; font-weight: 700; color: {TEXT}; background: transparent;"
        )
        self.hint_label = QLabel(hint)
        self.hint_label.setStyleSheet(f"font-size: 8.5pt; color: {MUTED}; background: transparent;")
        layout.addWidget(self.title_label)
        layout.addWidget(self.hint_label)
        if callback is not None:
            self.clicked.connect(callback)

    def set_hint(self, text: str) -> None:
        self.hint_label.setText(text)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        inside = self.rect().contains(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and inside:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

