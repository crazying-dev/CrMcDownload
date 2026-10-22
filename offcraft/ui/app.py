"""CrMCDownload 主窗口（PyQt6）。

PCL 风格：顶部导航（启动 / 下载 / 插件 / 设置），启动页左侧版本列表、右侧版本详情，
底部左侧为「启动游戏」。窗口宽高比 3:2，左右栏宽度比 2:3。
下载源按 F12 打开独立窗口修改，默认 https://mc.yjlt.top。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from offcraft import APP_NAME, __version__
from offcraft.config import Config, config_dir, save_config
from offcraft.instances import Instance, discover_instances
from offcraft.launch.auth import make_profile
from offcraft.launch.command import LaunchError, LaunchOptions, build_command
from offcraft.launch.java import detect_java, list_java
from offcraft.launch.process import GameProcess
from offcraft.meta.manifest import VersionManifest, VersionSummary
from offcraft.meta.version import VersionError, load_version
from offcraft.mirror import Mirror
from offcraft.net.downloader import Downloader, ProgressTracker

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

LAUNCH_CATEGORIES = ["原版游戏", "Mod", "整合包", "光影", "Java"]
DOWNLOAD_CATEGORIES = ["正式版", "快照", "远古版", "已安装"]
PLUGIN_CATEGORIES = ["市场", "官方", "社区", "其他", "已下载"]

QSS = """
QWidget {
    background: #eef1f5;
    color: #23272f;
    font-family: "Microsoft YaHei UI";
    font-size: 10pt;
}
QFrame#sidebar, QFrame#card, QGroupBox {
    background: #ffffff;
    border-radius: 8px;
}
QGroupBox {
    border: 1px solid #e6ebf1;
    margin-top: 16px;
    padding-top: 6px;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: #6b7280; font-size: 9pt;
}
QLabel#title { font-size: 17pt; font-weight: 700; }
QLabel#brand { font-size: 13pt; font-weight: 700; color: #3b6fd4; }
QLabel#muted { color: #6b7280; font-size: 9pt; }
QLabel#status { background: #ffffff; color: #6b7280; padding: 4px 10px; }
QPushButton {
    background: #ffffff; border: 1px solid #dde3ea; border-radius: 6px; padding: 7px 12px;
}
QPushButton:hover { background: #e8f0ff; border-color: #b8cdf5; }
QPushButton:checked { background: #dbe7ff; border-color: #3b6fd4; color: #234d9e; }
QPushButton#accent { background: #3b6fd4; color: #ffffff; border: none; font-weight: 700; }
QPushButton#accent:hover { background: #4f81e0; }
QPushButton#accent:disabled { background: #a8bde8; }
QPushButton#flat { background: transparent; border: none; text-align: left; padding: 5px 9px; }
QPushButton#flat:hover { background: #e8f0ff; }
QPushButton#flat:checked { background: #dbe7ff; color: #234d9e; font-weight: 600; }
QLineEdit, QSpinBox {
    background: #ffffff; border: 1px solid #dde3ea; border-radius: 5px; padding: 5px 7px;
}
QLineEdit:focus, QSpinBox:focus { border-color: #3b6fd4; }
QListWidget, QTreeWidget {
    background: #ffffff; border: 1px solid #e6ebf1; border-radius: 6px;
}
QListWidget::item { padding: 7px 8px; border-radius: 4px; }
QListWidget::item:selected { background: #dbe7ff; color: #23272f; }
QTreeWidget::item { padding: 4px; }
QTreeWidget::item:selected { background: #dbe7ff; color: #23272f; }
QHeaderView::section { background: #f7f9fc; color: #6b7280; border: none; padding: 6px; }
QProgressBar {
    background: #ffffff; border: 1px solid #dde3ea; border-radius: 6px;
    height: 14px; text-align: center; font-size: 8pt;
}
QProgressBar::chunk { background: #3b6fd4; border-radius: 5px; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #c9d3e0; border-radius: 5px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""


class SourceDialog(QDialog):
    """F12：修改下载源（默认 https://mc.yjlt.top）。"""

    def __init__(self, parent: QWidget, config: Config) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("下载源设置（F12）")
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        self.base = QLineEdit(config.download_base or "https://mc.yjlt.top")
        self.token = QLineEdit(config.download_token)
        self.proxy = QLineEdit(config.proxy)
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setValue(int(config.max_workers))
        form.addRow("下载源", self.base)
        form.addRow("访问令牌", self.token)
        form.addRow("代理", self.proxy)
        form.addRow("并发下载数", self.workers)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def apply(self) -> None:
        self.config.download_base = self.base.text().strip().rstrip("/") or "https://mc.yjlt.top"
        self.config.download_token = self.token.text().strip()
        self.config.proxy = self.proxy.text().strip()
        self.config.max_workers = int(self.workers.value())


class MainWindow(QMainWindow):
    def __init__(self, config: Config, data_dir: str | Path | None = None) -> None:
        super().__init__()
        self.config = config
        self.data_dir = data_dir
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.manifest: VersionManifest | None = None
        self.instances: list[Instance] = []
        self.process: GameProcess | None = None
        self.cancel_event: threading.Event | None = None
        self._filtered: list[Instance] = []
        self._detail: Instance | None = None
        self._launch_category = "原版游戏"
        self._download_category = "正式版"
        self._plugin_category = "已下载"
        self.remote_summaries: list[VersionSummary] = []
        self.log_records: deque[tuple[str, str, str]] = deque(maxlen=2000)
        self.log_text: QPlainTextEdit | None = None

        self.setWindowTitle(APP_NAME)
        screen = QApplication.primaryScreen()
        avail_w = screen.availableGeometry().width() if screen else 1200
        avail_h = screen.availableGeometry().height() if screen else 800
        height = min(733, max(600, avail_h - 100))
        width = int(height * 1.5)
        if width > avail_w - 60:
            width = avail_w - 60
            height = int(width / 1.5)
        self.setFixedSize(width, height)
        self._left_width = int(width * 0.4)
        self.setStyleSheet(QSS)

        self._build()
        QShortcut(QKeySequence("F12"), self, activated=self.open_source_dialog)
        self.refresh_instances()
        self._load_cached_manifest()
        if self.config.download_base:
            self.refresh_remote()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(120)

    # --------------------------------------------------------------- 构建
    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 4)
        outer.setSpacing(6)

        header = QHBoxLayout()
        brand = QLabel("CrMCDownload")
        brand.setObjectName("brand")
        header.addWidget(brand)
        self.nav_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in (
            ("launch", "启动"),
            ("download", "下载"),
            ("plugins", "插件"),
            ("settings", "设置"),
        ):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("flat")
            button.clicked.connect(lambda _c, name=key: self.show_page(name))
            group.addButton(button)
            header.addWidget(button)
            self.nav_buttons[key] = button
        header.addStretch(1)
        log_button = QPushButton("日志")
        log_button.setObjectName("flat")
        log_button.clicked.connect(self.open_log_window)
        header.addWidget(log_button)
        outer.addLayout(header)

        self.status = QLabel("就绪")
        self.status.setObjectName("status")
        self.stack = QStackedWidget()
        self.pages = {
            "launch": self._build_launch_page(),
            "download": self._build_download_page(),
            "plugins": self._build_plugins_page(),
            "settings": self._build_settings_page(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        outer.addWidget(self.stack, 1)
        outer.addWidget(self.status)
        self.show_page("launch")

    def _muted(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        return label

    # --------------------------------------------------------------- 启动页
    def _build_launch_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        left = QFrame()
        left.setObjectName("sidebar")
        left.setFixedWidth(self._left_width)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(4)
        left_layout.addWidget(self._muted("游戏"))
        self.launch_cat_buttons: dict[str, QPushButton] = {}
        self._launch_group = QButtonGroup(self)
        self._launch_group.setExclusive(True)
        for category in LAUNCH_CATEGORIES:
            button = QPushButton(category)
            button.setCheckable(True)
            button.setObjectName("flat")
            button.clicked.connect(lambda _c, name=category: self._select_launch_category(name))
            self._launch_group.addButton(button)
            left_layout.addWidget(button)
            self.launch_cat_buttons[category] = button

        left_layout.addSpacing(4)
        left_layout.addWidget(self._muted("版本"))
        self.version_search = QLineEdit()
        self.version_search.setPlaceholderText("搜索版本")
        self.version_search.textChanged.connect(lambda _t: self._render_launch_list())
        left_layout.addWidget(self.version_search)
        self.version_list = QListWidget()
        self.version_list.currentRowChanged.connect(self._on_instance_select)
        self.version_list.itemDoubleClicked.connect(lambda _i: self._launch_or_stop())
        left_layout.addWidget(self.version_list, 1)
        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setObjectName("accent")
        self.launch_button.setFixedHeight(52)
        self.launch_button.clicked.connect(self._launch_or_stop)
        left_layout.addWidget(self.launch_button)

        right = QFrame()
        right.setObjectName("card")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setSpacing(8)

        title_row = QHBoxLayout()
        self.detail_title = QLabel("未选择版本")
        self.detail_title.setObjectName("title")
        title_row.addWidget(self.detail_title)
        self.detail_loader = QLabel("")
        self.detail_loader.setObjectName("muted")
        title_row.addWidget(self.detail_loader)
        title_row.addStretch(1)
        open_dir = QPushButton("打开目录")
        open_dir.setObjectName("flat")
        open_dir.clicked.connect(self.open_game_dir)
        title_row.addWidget(open_dir)
        right_layout.addLayout(title_row)

        info = QFrame()
        info.setObjectName("card")
        grid = QGridLayout(info)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        self.detail_labels: dict[str, QLabel] = {}
        for row, (title, key) in enumerate((("Java 路径", "java"), ("版本路径", "path"))):
            grid.addWidget(self._muted(title), row, 0, alignment=Qt.AlignmentFlag.AlignTop)
            value = QLabel("-")
            grid.addWidget(value, row, 1)
            self.detail_labels[key] = value
        grid.setColumnStretch(1, 1)
        right_layout.addWidget(info)

        account = QHBoxLayout()
        account.addWidget(self._muted("玩家名"))
        self.username_edit = QLineEdit(self.config.username)
        self.username_edit.setFixedWidth(110)
        account.addWidget(self.username_edit)
        account.addSpacing(10)
        account.addWidget(self._muted("内存 MB"))
        self.memory_spin = QSpinBox()
        self.memory_spin.setRange(1024, 32768)
        self.memory_spin.setSingleStep(512)
        self.memory_spin.setValue(int(self.config.memory_mb))
        self.memory_spin.setFixedWidth(90)
        account.addWidget(self.memory_spin)
        account.addStretch(1)
        account.addWidget(self._muted(f"v{__version__}"))
        right_layout.addLayout(account)

        lists = QVBoxLayout()
        mods_box = QGroupBox("Mod")
        mods_layout = QVBoxLayout(mods_box)
        self.mods_tree = QTreeWidget()
        self.mods_tree.setHeaderLabels(["Mod 文件"])
        self.mods_tree.setRootIsDecorated(False)
        mods_layout.addWidget(self.mods_tree)
        saves_box = QGroupBox("存档")
        saves_layout = QVBoxLayout(saves_box)
        self.saves_tree = QTreeWidget()
        self.saves_tree.setHeaderLabels(["存档", "修改时间"])
        self.saves_tree.setRootIsDecorated(False)
        saves_layout.addWidget(self.saves_tree)
        lists.addWidget(mods_box, 1)
        lists.addWidget(saves_box, 1)
        right_layout.addLayout(lists, 1)

        layout.addWidget(left, 2)
        layout.addWidget(right, 3)
        self._select_launch_category("原版游戏")
        return page

    def _select_launch_category(self, category: str) -> None:
        self._launch_category = category
        for name, button in self.launch_cat_buttons.items():
            button.setChecked(name == category)
        self._render_launch_list()

    def _render_launch_list(self) -> None:
        if not hasattr(self, "version_list"):
            return
        self.version_list.blockSignals(True)
        self.version_list.clear()
        self._filtered = []

        if self._launch_category == "Java":
            try:
                candidates = list_java(self._game_dir())
            except Exception as exc:  # noqa: BLE001
                self.log(f"检测 Java 失败：{exc}", "ERROR")
                candidates = []
            for java in candidates:
                self.version_list.addItem(f"{java.label}  ·  {java.source}")
            self.version_list.blockSignals(False)
            if candidates:
                self.version_list.setCurrentRow(0)
                self._show_java_details(candidates[0])
            return

        if self._launch_category == "光影":
            self.version_list.addItem("光影管理将在后续版本提供")
            self.version_list.blockSignals(False)
            return

        query = self.version_search.text().strip().lower()
        for item in self.instances:
            if self._launch_category == "原版游戏" and item.loader != "原版":
                continue
            if self._launch_category == "Mod" and item.loader == "原版":
                continue
            if query and query not in item.id.lower() and query not in item.mc_version.lower():
                continue
            self._filtered.append(item)
            self.version_list.addItem(item.id)
        self.version_list.blockSignals(False)
        self.status.setText(f"{len(self._filtered)} 个实例")
        if self._filtered:
            self.version_list.setCurrentRow(0)
            self._show_instance_details(self._filtered[0])
        else:
            self._detail = None
            self.detail_title.setText("未选择版本")
            self.detail_loader.setText("")
            for label in self.detail_labels.values():
                label.setText("-")
            self.mods_tree.clear()
            self.saves_tree.clear()

    def _on_instance_select(self, row: int) -> None:
        if self._launch_category == "Java" or row < 0 or row >= len(self._filtered):
            return
        self._show_instance_details(self._filtered[row])

    @staticmethod
    def _elide(text: str, limit: int = 28) -> str:
        return text if len(text) <= limit else "…" + text[-(limit - 1):]

    def _show_instance_details(self, instance: Instance) -> None:
        self._detail = instance
        self.detail_title.setText(instance.id)
        self.detail_loader.setText(instance.loader)
        version_dir = self._game_dir() / "versions" / instance.id
        self.detail_labels["path"].setText(self._elide(str(version_dir)))
        java_text = "未找到"
        try:
            java = detect_java(self._game_dir(), instance.java_major)
            java_text = self._elide(str(java.path))
        except Exception:  # noqa: BLE001
            pass
        self.detail_labels["java"].setText(java_text)

        self.mods_tree.clear()
        for folder in (self._game_dir() / "mods", version_dir / "mods"):
            if folder.is_dir():
                for entry in sorted(folder.glob("*.jar")):
                    self.mods_tree.addTopLevelItem(QTreeWidgetItem([entry.name]))
        self.saves_tree.clear()
        saves_dir = self._game_dir() / "saves"
        if saves_dir.is_dir():
            for entry in sorted(saves_dir.iterdir()):
                if entry.is_dir():
                    try:
                        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.stat().st_mtime))
                    except OSError:
                        stamp = "-"
                    self.saves_tree.addTopLevelItem(QTreeWidgetItem([entry.name, stamp]))

    def _show_java_details(self, java) -> None:  # noqa: ANN001
        self._detail = None
        self.detail_title.setText("Java")
        self.detail_loader.setText(java.source)
        self.detail_labels["java"].setText(self._elide(str(java.path)))
        self.detail_labels["path"].setText(self._elide(str(java.path.parent.parent)))
        self.mods_tree.clear()
        self.saves_tree.clear()

    def _launch_or_stop(self) -> None:
        if self.process is not None and self.process.running:
            self.stop_game()
        else:
            self.launch_selected()

    # --------------------------------------------------------------- 下载页
    def _build_download_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        left = QFrame()
        left.setObjectName("sidebar")
        left.setFixedWidth(self._left_width)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(self._muted("版本类型"))
        self.download_cat_buttons: dict[str, QPushButton] = {}
        self._download_group = QButtonGroup(self)
        self._download_group.setExclusive(True)
        for category in DOWNLOAD_CATEGORIES:
            button = QPushButton(category)
            button.setCheckable(True)
            button.setObjectName("flat")
            button.clicked.connect(lambda _c, name=category: self._select_download_category(name))
            self._download_group.addButton(button)
            left_layout.addWidget(button)
            self.download_cat_buttons[category] = button
        left_layout.addStretch(1)
        hint = self._muted("F12 修改下载源")
        hint.setWordWrap(True)
        left_layout.addWidget(hint)

        right = QFrame()
        right.setObjectName("card")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setSpacing(8)
        search_row = QHBoxLayout()
        self.remote_search = QLineEdit()
        self.remote_search.setPlaceholderText("搜索版本")
        self.remote_search.returnPressed.connect(self._render_remote)
        search_row.addWidget(self.remote_search, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_remote)
        search_row.addWidget(refresh)
        right_layout.addLayout(search_row)

        self.remote_tree = QTreeWidget()
        self.remote_tree.setHeaderLabels(["版本", "类型", "发布时间"])
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.itemDoubleClicked.connect(lambda _i, _c: self.install_selected())
        right_layout.addWidget(self.remote_tree, 1)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        right_layout.addWidget(self.progress)
        actions = QHBoxLayout()
        self.install_button = QPushButton("安装所选版本")
        self.install_button.setObjectName("accent")
        self.install_button.clicked.connect(self.install_selected)
        actions.addWidget(self.install_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_task)
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        right_layout.addLayout(actions)

        layout.addWidget(left, 2)
        layout.addWidget(right, 3)
        self._select_download_category("正式版")
        return page

    def _select_download_category(self, category: str) -> None:
        self._download_category = category
        for name, button in self.download_cat_buttons.items():
            button.setChecked(name == category)
        if category == "已安装":
            self._render_downloaded()
        else:
            self._render_remote()

    def _render_downloaded(self) -> None:
        self.remote_tree.clear()
        self.remote_summaries = []
        for item in self.instances:
            QTreeWidgetItem(self.remote_tree, [item.id, item.loader, format_size(item.size_bytes)])

    def _render_remote(self) -> None:
        if self.manifest is None:
            return
        kinds = {
            "正式版": {"release"},
            "快照": {"snapshot"},
            "远古版": {"old_beta", "old_alpha"},
            "已安装": None,
        }.get(self._download_category)
        items = self.manifest.filter(kinds, self.remote_search.text())
        self.remote_tree.clear()
        self.remote_summaries = []
        for item in items[:500]:
            QTreeWidgetItem(self.remote_tree, [item.id, item.label, item.release_time[:10]])
            self.remote_summaries.append(item)
        self.status.setText(f"{len(items)} 个版本")

    # --------------------------------------------------------------- 插件页
    def _build_plugins_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        left = QFrame()
        left.setObjectName("sidebar")
        left.setFixedWidth(self._left_width)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.addWidget(self._muted("插件"))
        self.plugin_cat_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for category in PLUGIN_CATEGORIES:
            button = QPushButton(category)
            button.setCheckable(True)
            button.setObjectName("flat")
            button.clicked.connect(lambda _c, name=category: self._select_plugin_category(name))
            group.addButton(button)
            left_layout.addWidget(button)
            self.plugin_cat_buttons[category] = button
        left_layout.addStretch(1)

        right = QFrame()
        right.setObjectName("card")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 10, 12, 10)
        self.plugin_title = QLabel("已下载插件")
        self.plugin_title.setObjectName("title")
        right_layout.addWidget(self.plugin_title)
        self.plugin_list = QListWidget()
        right_layout.addWidget(self.plugin_list, 1)
        self.plugin_hint = self._muted("")
        self.plugin_hint.setWordWrap(True)
        right_layout.addWidget(self.plugin_hint)

        layout.addWidget(left, 2)
        layout.addWidget(right, 3)
        self._select_plugin_category("已下载")
        return page

    def _select_plugin_category(self, category: str) -> None:
        self._plugin_category = category
        for name, button in self.plugin_cat_buttons.items():
            button.setChecked(name == category)
        self.plugin_list.clear()
        self.plugin_title.setText(f"{category}插件")
        if category == "已下载":
            plugin_dir = config_dir(self.data_dir) / "plugins"
            names = sorted(p.name for p in plugin_dir.glob("*.py")) if plugin_dir.is_dir() else []
            for name in names:
                self.plugin_list.addItem(name)
            self.plugin_hint.setText(
                "将 .py 插件放入数据目录的 plugins/ 下即可加载。"
                if names
                else "还没有已安装插件。把 .py 文件放入数据目录的 plugins/ 目录。"
            )
        else:
            self.plugin_hint.setText(f"{category}插件市场仍在规划中，当前仅支持本地插件。")

    # --------------------------------------------------------------- 设置页
    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(10)
        self.settings_edits: dict[str, QLineEdit] = {}
        fields = [
            ("游戏目录", "game_dir", self.config.game_dir or str(self._game_dir())),
            ("Java 路径（可选）", "java_path", self.config.java_overrides.get("__global__", "")),
            ("额外 JVM 参数", "extra_jvm", " ".join(self.config.extra_jvm)),
        ]
        for label, key, value in fields:
            edit = QLineEdit(value)
            self.settings_edits[key] = edit
            form.addRow(self._muted(label), edit)
        self.skip_assets_check = QCheckBox("安装时跳过资源文件（更快）")
        self.skip_assets_check.setChecked(bool(self.config.skip_assets))
        form.addRow(self.skip_assets_check)
        save = QPushButton("保存设置")
        save.setObjectName("accent")
        save.clicked.connect(self.save_settings)
        form.addRow(save)
        note = self._muted("下载源、访问令牌、代理与并发数请按 F12 修改。")
        note.setWordWrap(True)
        form.addRow(note)
        layout.addWidget(card, 1)
        layout.addStretch(1)
        return page

    def save_settings(self) -> None:
        edits = self.settings_edits
        self.config.game_dir = edits["game_dir"].text().strip()
        java_path = edits["java_path"].text().strip()
        if java_path:
            self.config.java_overrides["__global__"] = java_path
        else:
            self.config.java_overrides.pop("__global__", None)
        self.config.extra_jvm = [part for part in edits["extra_jvm"].text().split(" ") if part]
        self.config.skip_assets = self.skip_assets_check.isChecked()
        save_config(self.config, self.data_dir)
        self.refresh_instances()
        self.status.setText("设置已保存")
        self.log("设置已保存", "SYSTEM")

    # --------------------------------------------------------------- 事件
    def show_page(self, name: str) -> None:
        if name not in self.pages:
            return
        self.stack.setCurrentWidget(self.pages[name])
        for key, button in self.nav_buttons.items():
            button.setChecked(key == name)
        if name == "download" and self.manifest is None and self.config.download_base:
            self.refresh_remote()
        if name == "launch":
            self.refresh_instances()

    def open_source_dialog(self) -> None:
        dialog = SourceDialog(self, self.config)
        if dialog.exec():
            dialog.apply()
            save_config(self.config, self.data_dir)
            self.log(f"下载源已更新：{self.config.download_base}", "SYSTEM")
            self.refresh_remote()

    def log(self, text: str, level: str = "INFO") -> None:
        self.events.put(("log", (level, text)))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    level, text = payload  # type: ignore[misc]
                    self._append_log(level, text)
                elif kind == "progress":
                    self._show_progress(payload)  # type: ignore[arg-type]
                elif kind == "remote":
                    self.manifest = payload  # type: ignore[assignment]
                    self._render_remote()
                elif kind == "instances":
                    self.instances = payload  # type: ignore[assignment]
                    self._render_launch_list()
                elif kind == "task_started":
                    self.install_button.setEnabled(False)
                    self.cancel_button.setEnabled(True)
                elif kind == "task_done":
                    self.install_button.setEnabled(True)
                    self.cancel_button.setEnabled(False)
                    self.progress.setRange(0, 100)
                    self.refresh_instances()
                elif kind == "status":
                    self.status.setText(str(payload))
                elif kind == "launch_started":
                    self.launch_button.setText("结束游戏")
                elif kind == "launch_done":
                    self.launch_button.setText("启动游戏")
                    self.process = None
        except queue.Empty:
            pass

    def _append_log(self, level: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_records.append((stamp, level, text))
        if self.log_text is not None:
            try:
                self.log_text.appendPlainText(f"[{stamp}] {text}")
            except RuntimeError:
                self.log_text = None

    def open_log_window(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("运行日志")
        dialog.resize(680, 420)
        layout = QVBoxLayout(dialog)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("font-family: Consolas; font-size: 9pt;")
        for stamp, _level, text in self.log_records:
            self.log_text.appendPlainText(f"[{stamp}] {text}")
        layout.addWidget(self.log_text)
        dialog.exec()
        self.log_text = None

    # --------------------------------------------------------------- 数据
    def _game_dir(self) -> Path:
        return Path(self.config.game_dir) if self.config.game_dir else default_game_dir()

    def refresh_instances(self) -> None:
        threading.Thread(
            target=lambda: self.events.put(("instances", discover_instances(self._game_dir()))),
            daemon=True,
        ).start()

    def refresh_remote(self) -> None:
        base = self.config.download_base or "https://mc.yjlt.top"
        self.config.download_base = base
        self.status.setText(f"正在获取版本清单（{base}）…")
        threading.Thread(target=self._fetch_remote, daemon=True).start()

    def _fetch_remote(self) -> None:
        try:
            mirror = Mirror(self.config.download_base)
            downloader = Downloader(
                proxy=self.config.proxy or None,
                token=self.config.download_token or None,
                workers=2,
                timeout=30,
            )
            data = downloader.fetch_json(mirror.rewrite(MANIFEST_URL))
            manifest = VersionManifest.from_json(data)
            try:
                path = config_dir(self.data_dir) / "cache" / "version_manifest.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            self.events.put(("remote", manifest))
            self.events.put(("status", f"共 {len(manifest.versions)} 个版本"))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("log", ("ERROR", f"获取版本清单失败：{exc}")))
            self.events.put(("status", "版本清单获取失败（F12 可修改下载源）"))

    def _load_cached_manifest(self) -> None:
        try:
            data = json.loads(
                (config_dir(self.data_dir) / "cache" / "version_manifest.json").read_text(encoding="utf-8")
            )
            self.manifest = VersionManifest.from_json(data)
        except (OSError, ValueError, json.JSONDecodeError):
            return
        self._render_remote()
        self.log(f"已加载缓存的版本清单：{len(self.manifest.versions)} 个版本")

    # --------------------------------------------------------------- 安装
    def install_selected(self) -> None:
        node = self.remote_tree.currentItem()
        row = self.remote_tree.indexOfTopLevelItem(node) if node is not None else -1
        if row < 0 or row >= len(self.remote_summaries):
            QMessageBox.information(self, APP_NAME, "请先选择一个版本")
            return
        summary = self.remote_summaries[row]
        if self.manifest is None:
            return
        self.cancel_event = threading.Event()
        self.events.put(("task_started", None))
        self.events.put(("status", f"正在安装 {summary.id}…"))
        threading.Thread(target=self._install_worker, args=(summary, self.manifest), daemon=True).start()

    def _install_worker(self, summary: VersionSummary, manifest: VersionManifest) -> None:
        from offcraft.install.installer import install, prepare_install
        from offcraft.install.plan import InstallOptions

        try:
            mirror = Mirror(self.config.download_base)
            downloader = Downloader(
                proxy=self.config.proxy or None,
                token=self.config.download_token or None,
                workers=self.config.max_workers,
                timeout=30,
                progress_callback=lambda tracker: self.events.put(("progress", tracker.snapshot())),
                progress_interval=0.25,
            )
            options = InstallOptions(include_assets=not self.config.skip_assets)
            plan = prepare_install(self._game_dir(), summary.id, manifest, mirror, downloader, options=options)
            self.events.put(("log", ("INFO", f"{summary.id}：{plan.summary()}")))
            for warning in plan.warnings:
                self.events.put(("log", ("WARN", warning)))
            result = install(plan, downloader, tracker=ProgressTracker(), cancel=self.cancel_event)
            if result.ok:
                self.events.put(
                    ("log", ("INFO", f"{summary.id} 安装完成：{len(result.report.succeeded)} 个文件，{result.duration:.1f}s"))
                )
                self.config.last_version = summary.id
                save_config(self.config, self.data_dir)
            else:
                for task, error in result.report.failed[:5]:
                    self.events.put(("log", ("ERROR", f"{task.name}: {error}")))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("log", ("ERROR", f"安装失败：{exc}")))
        finally:
            self.events.put(("task_done", None))

    def cancel_task(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.log("正在取消…", "WARN")

    def _show_progress(self, snap: dict) -> None:
        percent = snap.get("percent")
        if percent is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(percent))
        self.status.setText(
            f"{percent or 0:.1f}%  {snap['done_bytes'] / 1048576:.1f}/"
            f"{snap['total_bytes'] / 1048576:.1f} MiB  {snap['current'][:34]}"
        )

    # --------------------------------------------------------------- 启动
    def launch_selected(self) -> None:
        if self._launch_category == "Java":
            QMessageBox.information(self, APP_NAME, "当前页是 Java 列表，请切换回实例分类")
            return
        instance = self._detail
        if instance is None:
            QMessageBox.information(self, APP_NAME, "请先选择一个实例")
            return
        if self.process is not None and self.process.running:
            QMessageBox.information(self, APP_NAME, "游戏已在运行")
            return
        self.config.username = self.username_edit.text().strip() or "Player"
        self.config.memory_mb = int(self.memory_spin.value())
        save_config(self.config, self.data_dir)
        threading.Thread(target=self._launch_worker, args=(instance,), daemon=True).start()

    def _launch_worker(self, instance: Instance) -> None:
        try:
            version = load_version(self._game_dir(), instance.id)
            profile = make_profile(self.config.username)
            options = LaunchOptions(
                memory_mb=self.config.memory_mb,
                width=self.config.width,
                height=self.config.height,
                fullscreen=self.config.fullscreen,
                java_path=self.config.java_overrides.get(instance.id)
                or self.config.java_overrides.get("__global__"),
                extra_jvm=list(self.config.extra_jvm),
            )
            plan = build_command(version, self._game_dir(), profile, options=options)
            for warning in plan.warnings:
                self.events.put(("log", ("WARN", warning)))
            self.events.put(("log", ("INFO", f"启动 {instance.id}（{plan.java.display}）")))
            self.events.put(("launch_started", None))

            def on_line(line: str) -> None:
                self.events.put(("log", ("INFO", line)))

            def on_exit(code: int) -> None:
                self.events.put(("log", ("INFO" if code == 0 else "ERROR", f"游戏进程退出，代码 {code}")))
                self.events.put(("launch_done", None))

            proc = GameProcess(plan, on_line=on_line, on_exit=on_exit)
            self.process = proc
            proc.start()
            self.events.put(("status", "游戏运行中"))
        except (LaunchError, VersionError, ValueError) as exc:
            self.events.put(("log", ("ERROR", f"无法启动：{exc}")))
            self.events.put(("launch_done", None))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("log", ("ERROR", f"启动异常：{exc}")))
            self.events.put(("launch_done", None))

    def stop_game(self) -> None:
        if self.process is not None:
            self.log("正在结束游戏…", "WARN")
            self.process.stop()

    # --------------------------------------------------------------- 其他
    def open_game_dir(self) -> None:
        game_dir = self._game_dir()
        game_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(game_dir))  # type: ignore[attr-defined]

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        save_config(self.config, self.data_dir)
        if self.process is not None and self.process.running:
            answer = QMessageBox.question(self, APP_NAME, "游戏仍在运行，确定退出启动器？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.process.stop()
        event.accept()


def format_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def default_game_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"


def run_app(config: Config, data_dir: str | Path | None = None) -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    os.environ.setdefault("QT_SCALE_FACTOR", "1")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    window = MainWindow(config, data_dir)
    window.show()
    app.exec()
