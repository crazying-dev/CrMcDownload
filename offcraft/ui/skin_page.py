"""皮肤页：选图 → 预览（2D 展开）→ 上传到镜像 /skin → 状态反馈。

入口挂在启动页账号行「自定义皮肤」与设置页卡片上（由 MainWindow 包成对话框）。
未配置下载源时禁用上传并给出说明；上传成功后本地缓存并刷新启动页头像。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from offcraft.skins.avatar import head_avatar
from offcraft.skins.upload import (
    SkinError,
    load_local_skin,
    read_skin,
    save_local_skin,
    upload_skin,
)
from offcraft.ui.theme import MUTED, initial_pixmap
from offcraft.ui.widgets import Badge, Card

__all__ = ["SkinPage"]


class SkinPage(QWidget):
    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        self._data: bytes | None = None
        self._build()
        self._reload_state()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("自定义皮肤")
        title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        header.addWidget(title)
        header.addStretch(1)
        self.status_badge = Badge("未选择", "gray")
        header.addWidget(self.status_badge)
        root.addLayout(header)

        hint = QLabel("只接受 64×64 或 64×32 的 PNG。皮肤上传到下载源后，装了 CustomSkinLoader 的实例会显示。")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED};")
        root.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(16)

        self.preview = QLabel()
        self.preview.setFixedSize(128, 128)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: #F1F3F7; border-radius: 10px;")
        self.preview.setPixmap(initial_pixmap(self.window.config.username, 96, "skin"))
        preview_card = Card()
        preview_card.add(self.preview)
        preview_card.add(self._label("正面 / 背面（2D 展开）"))
        body.addWidget(preview_card)

        controls = QVBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(self._label("模型"))
        self.model = QComboBox()
        self.model.addItems(["自动判定", "经典 (classic)", "纤细 (slim)"])
        controls.addWidget(self.model)

        self.choose = QPushButton("选择皮肤图片…")
        self.choose.clicked.connect(self._choose_file)
        controls.addWidget(self.choose)

        self.upload = QPushButton("上传皮肤")
        self.upload.setObjectName("primary")
        self.upload.setMinimumHeight(34)
        self.upload.clicked.connect(self._upload)
        controls.addWidget(self.upload)

        self.info = QLabel(" ")
        self.info.setWordWrap(True)
        self.info.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        controls.addWidget(self.info)
        controls.addStretch(1)
        body.addLayout(controls, 1)

        root.addLayout(body, 1)

    def _label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        return label

    # ------------------------------------------------------------------ 状态
    def _reload_state(self) -> None:
        base = getattr(self.window.config, "download_base", "") or ""
        enabled = bool(base)
        self.upload.setEnabled(enabled and self._data is not None)
        self.choose.setEnabled(True)
        if not enabled:
            self.info.setText("尚未配置下载源，无法上传皮肤（按 F12 打开下载源设置）。")
        elif self._data is None:
            self.info.setText("请先选择一张皮肤图片。")

    # ------------------------------------------------------------------ 交互
    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择皮肤图片", "", "PNG 图片 (*.png)")
        if not path:
            return
        try:
            data, info = read_skin(
                path,
                self.window.config.username,
                model=self._model_value(),
            )
        except SkinError as exc:
            self.status_badge.set("无效", "red")
            self.info.setText(str(exc))
            self._data = None
            self.upload.setEnabled(False)
            return
        self._data = data
        self._info = info
        self.status_badge.set(f"{info.model}", "blue")
        self.info.setText(f"{info.width}×{info.height} · {info.size} 字节 · SHA1 {info.sha1[:12]}")
        self._render_preview(data)
        base = getattr(self.window.config, "download_base", "") or ""
        self.upload.setEnabled(bool(base))

    def _model_value(self) -> str:
        return ["auto", "classic", "slim"][self.model.currentIndex()]

    def _render_preview(self, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        scaled = pixmap.scaled(
            128, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation
        )
        self.preview.setPixmap(scaled)

    def _upload(self) -> None:
        if self._data is None:
            return
        self.upload.setEnabled(False)
        self.status_badge.set("上传中…", "amber")
        self.window.run_task(
            "上传皮肤",
            self._upload_worker,
            on_done=self._upload_finished,
        )

    def _upload_worker(self, cancel: Any) -> str:
        data, info = self._data, self._info
        downloader = self.window.make_downloader(progress=False)
        mirror = self.window.source(downloader)
        result = upload_skin(data, info, downloader=downloader, mirror=mirror)
        save_local_skin(info, data)
        avatar = head_avatar(data, size=128)
        self.window.post(lambda a=avatar: self.window.apply_avatar(a))
        return f"已上传 {info.name}（{info.model}）· {result.get('status', 'ok')}"

    def _upload_finished(self) -> None:
        self.status_badge.set("已上传", "green")
        self.upload.setEnabled(True)

    # ------------------------------------------------------------------ 头像
    @staticmethod
    def current_avatar(name: str, size: int = 128) -> bytes | None:
        """取本地缓存皮肤的头像字节（启动页头像用）；无皮肤返回 ``None``。"""
        try:
            data = load_local_skin(name, model="classic") or load_local_skin(name, model="slim")
        except SkinError:
            return None
        if not data:
            return None
        avatar = head_avatar(data, size=size)
        return avatar or None
