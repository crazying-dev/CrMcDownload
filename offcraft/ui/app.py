"""CrMCDownload 主窗口（PyQt6）。

界面参考 PCL2：左侧导航 + 卡片式内容区，右侧是可操作的主体。

- 「启动游戏」：账户、当前实例概览、内存滑杆、启动按钮、版本列表与快捷入口。
- 「下载中心」：游戏版本 + Modrinth 的模组 / 整合包 / 资源包 / 光影。
- 「设置」：游戏目录、版本隔离、Java、内存、下载源。

所有网络请求都在后台线程执行，结果通过 ``self.events`` 回到界面线程，
因此搜索、安装、下载过程中窗口不会卡死。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from offcraft import APP_NAME, __version__
from offcraft.config import Config, config_dir, save_config
from offcraft.content.install import download_file, install_project_version, safe_filename
from offcraft.content.modpack import unique_instance_name
from offcraft.content.modrinth import ModrinthClient, ProjectHit, ProjectVersion
from offcraft.instances import (
    Instance,
    discover_instances,
    instance_game_dir,
    read_pack_metadata,
)
from offcraft.launch.auth import make_profile, validate_name
from offcraft.launch.command import LaunchOptions, build_command
from offcraft.launch.java import list_java, probe_java
from offcraft.launch.process import GameProcess
from offcraft.meta.manifest import VersionManifest, VersionSummary
from offcraft.meta.version import load_version
from offcraft.mirror import Mirror
from offcraft.net.downloader import Downloader, ProgressTracker
from offcraft.ui.content_page import ContentPage
from offcraft.ui.theme import (
    DANGER,
    OK,
    app_qss,
    block_pixmap,
    human_date,
    human_size,
    initial_pixmap,
)
from offcraft.ui.widgets import Badge, Card, Tile

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

#: 下载中心的内容页签
CONTENT_TABS = [
    ("mod", "模组"),
    ("modpack", "整合包"),
    ("resourcepack", "资源包"),
    ("shader", "光影"),
]

#: 游戏版本分类
SEGMENTS = [
    ("release", "正式版"),
    ("snapshot", "快照"),
    ("old", "远古版"),
    ("installed", "已安装"),
]

SEGMENT_KINDS = {
    "release": {"release"},
    "snapshot": {"snapshot"},
    "old": {"old_beta", "old_alpha"},
}

LOADER_TONES = {
    "Fabric": "amber",
    "Forge": "gray",
    "NeoForge": "red",
    "Quilt": "purple",
    "原版": "green",
}


def default_game_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"


def format_size(value: int) -> str:
    return human_size(value)


def _reveal(folder: Path | str) -> None:
    path = Path(folder)
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError:
        pass


class SourceDialog(QDialog):
    """F12：修改下载源（默认 https://mc.yjlt.top）。"""

    def __init__(self, parent: QWidget, config: Config) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("下载源设置（F12）")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        hint = QLabel("客户端只与下载源通信：游戏文件、Fabric、Modrinth 都会经由该地址代理。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.base = QLineEdit(config.download_base or "https://mc.yjlt.top")
        self.token = QLineEdit(config.download_token)
        self.proxy = QLineEdit(config.proxy)
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setValue(int(config.max_workers))
        for label, widget in (
            ("下载源", self.base),
            ("访问令牌", self.token),
            ("代理", self.proxy),
            ("并发下载数", self.workers),
        ):
            row = QHBoxLayout()
            row.setSpacing(10)
            name = QLabel(label)
            name.setObjectName("muted")
            name.setFixedWidth(76)
            row.addWidget(name)
            row.addWidget(widget, 1)
            layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("accent")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def apply(self) -> None:
        self.config.download_base = self.base.text().strip().rstrip("/") or "https://mc.yjlt.top"
        self.config.download_token = self.token.text().strip()
        self.config.proxy = self.proxy.text().strip()
        self.config.max_workers = int(self.workers.value())


class AccountDialog(QDialog):
    """离线账户：只影响游戏内显示的用户名。"""

    def __init__(self, parent: QWidget, name: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("离线账户")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        hint = QLabel("离线账户无需登录，用户名会作为游戏内昵称（3-16 位字母 / 数字 / 下划线）。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.edit = QLineEdit(name)
        self.edit.setMaxLength(16)
        self.edit.selectAll()
        layout.addWidget(self.edit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("accent")
        ok.clicked.connect(self.accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def value(self) -> str:
        return self.edit.text().strip()


class LogDialog(QDialog):
    """运行日志：可按级别过滤并复制。"""

    LEVELS = [("全部", ""), ("信息", "INFO"), ("警告", "WARN"), ("错误", "ERROR")]

    def __init__(self, parent: QWidget, records: list[tuple[str, str, str]]) -> None:
        super().__init__(parent)
        self.records = records
        self.setWindowTitle("运行日志")
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.filter = QComboBox()
        for label, value in self.LEVELS:
            self.filter.addItem(label, value)
        self.filter.currentIndexChanged.connect(self._render)
        row.addWidget(self.filter)
        self.count = QLabel("")
        self.count.setObjectName("muted")
        row.addWidget(self.count)
        row.addStretch(1)
        copy = QPushButton("复制全部")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        layout.addLayout(row)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_row.addWidget(close)
        layout.addLayout(close_row)
        self._render()

    def _visible(self) -> list[tuple[str, str, str]]:
        level = self.filter.currentData()
        return [item for item in self.records if not level or item[1] == level]

    def _render(self) -> None:
        items = self._visible()
        self.text.setPlainText("\n".join(f"[{stamp}] [{level}] {text}" for stamp, level, text in items))
        self.count.setText(f"{len(items)} 条")
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())


class InstanceDialog(QDialog):
    """单个实例的设置：目录、Java、删除。"""

    def __init__(self, window: "MainWindow", instance: Instance) -> None:
        super().__init__(window)
        self.window = window
        self.instance = instance
        self.setWindowTitle(f"版本设置 · {instance.id}")
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        icon = QLabel()
        icon.setFixedSize(52, 52)
        icon.setPixmap(block_pixmap(instance.id, 52, instance.loader))
        header.addWidget(icon)
        names = QVBoxLayout()
        names.setSpacing(1)
        title = QLabel(instance.id)
        title.setObjectName("h2")
        names.addWidget(title)
        subtitle = QLabel(
            f"{instance.loader} · MC {instance.mc_version} · {human_size(instance.size_bytes)}"
        )
        subtitle.setObjectName("muted")
        names.addWidget(subtitle)
        header.addLayout(names, 1)
        layout.addLayout(header)

        pack = read_pack_metadata(window.game_dir(), instance.id)
        if pack:
            layout.addWidget(self._hint(f"整合包：{pack.get('name', '')} {pack.get('version', '')}".strip()))
        if instance.broken:
            broken = QLabel(instance.broken)
            broken.setObjectName("error")
            broken.setWordWrap(True)
            layout.addWidget(broken)

        layout.addWidget(self._row("游戏目录", str(window.game_dir_for(instance))))
        layout.addWidget(self._row("版本目录", str(window.game_dir() / "versions" / instance.id)))

        java_row = QHBoxLayout()
        java_row.setSpacing(10)
        java_label = QLabel("Java")
        java_label.setObjectName("muted")
        java_label.setFixedWidth(76)
        java_row.addWidget(java_label)
        self.java_combo = QComboBox()
        self.java_combo.addItem("跟随全局设置", "")
        for candidate in list_java(window.game_dir()):
            self.java_combo.addItem(f"Java {candidate.major} · {candidate.source}", str(candidate.path))
        index = self.java_combo.findData(window.config.java_overrides.get(instance.id, ""))
        if index >= 0:
            self.java_combo.setCurrentIndex(index)
        java_row.addWidget(self.java_combo, 1)
        layout.addLayout(java_row)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        open_game = QPushButton("打开游戏目录")
        open_game.clicked.connect(lambda: _reveal(window.game_dir_for(instance)))
        buttons.addWidget(open_game)
        open_version = QPushButton("打开版本目录")
        open_version.clicked.connect(lambda: _reveal(window.game_dir() / "versions" / instance.id))
        buttons.addWidget(open_version)
        buttons.addStretch(1)
        remove = QPushButton("删除实例")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove)
        buttons.addWidget(remove)
        done = QPushButton("完成")
        done.setObjectName("accent")
        done.clicked.connect(self._done)
        buttons.addWidget(done)
        layout.addLayout(buttons)

    @staticmethod
    def _hint(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _row(name: str, value: str) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        label = QLabel(name)
        label.setObjectName("muted")
        label.setFixedWidth(76)
        layout.addWidget(label)
        edit = QLineEdit(value)
        edit.setReadOnly(True)
        edit.setCursorPosition(0)
        layout.addWidget(edit, 1)
        return widget

    def _done(self) -> None:
        path = self.java_combo.currentData()
        if path:
            self.window.config.java_overrides[self.instance.id] = str(path)
        else:
            self.window.config.java_overrides.pop(self.instance.id, None)
        save_config(self.window.config, self.window.data_dir)
        self.accept()

    def _remove(self) -> None:
        answer = QMessageBox.question(
            self, APP_NAME, f"确定删除实例 {self.instance.id}？此操作不可恢复。"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(self.window.game_dir() / "versions" / self.instance.id, ignore_errors=True)
        self.window.notice(f"已删除实例 {self.instance.id}", "WARN")
        self.window.refresh_instances()
        self.accept()


class MainWindow(QMainWindow):
    """主窗口。"""

    def __init__(self, config: Config, data_dir: str | Path | None = None) -> None:
        super().__init__()
        self.config = config
        self.data_dir = data_dir
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.manifest: VersionManifest | None = None
        self.instances: list[Instance] = []
        self.selected: Instance | None = None
        self.process: GameProcess | None = None
        self.cancel_event: threading.Event | None = None
        self.log_records: deque[tuple[str, str, str]] = deque(maxlen=3000)
        self._segment = "release"
        self._current_tab = "game"
        self._remote_loading = False
        self._icons: dict[str, QPixmap] = {}
        self._icon_waiters: dict[str, list[tuple[Callable[[QPixmap], None], int]]] = {}
        self._icon_loading: set[str] = set()
        self._content_client: ModrinthClient | None = None
        self._content_key: tuple[str, str, str] | None = None
        self._version_rows: list[VersionSummary] = []

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(block_pixmap("crmcdownload", 64)))
        self.setStyleSheet(app_qss())
        self.setMinimumSize(1020, 660)
        width = int((config.window or {}).get("w", 1180) or 1180)
        height = int((config.window or {}).get("h", 760) or 760)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, available.width() - 40)
            height = min(height, available.height() - 60)
        self.resize(max(width, 1020), max(height, 660))

        self._build()
        QShortcut(QKeySequence("F12"), self, activated=self.open_source_dialog)
        self._refresh_source_badge()
        self._load_cached_manifest()
        self.refresh_instances()
        self.refresh_remote()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain)
        self._timer.start(80)

    # ------------------------------------------------------------- 布局
    def _label(self, text: str, name: str = "muted") -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        return label

    @staticmethod
    def _hline() -> QFrame:
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #E9EEF6; border: none;")
        return line

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar())

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(18, 16, 18, 8)
        right_layout.setSpacing(10)
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "home": self._build_home_page(),
            "download": self._build_download_page(),
            "settings": self._build_settings_page(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        right_layout.addWidget(self.stack, 1)
        right_layout.addWidget(self._build_task_panel())
        right_layout.addWidget(self._build_status_bar())
        root.addWidget(right, 1)
        self.show_page("home")

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("sidebar")
        bar.setFixedWidth(212)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(14, 18, 12, 14)
        layout.setSpacing(5)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo = QLabel()
        logo.setFixedSize(34, 34)
        logo.setPixmap(block_pixmap("crmcdownload", 34))
        brand_row.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("brand")
        names.addWidget(title)
        names.addWidget(self._label(f"v{__version__} · 自建镜像"), )
        brand_row.addLayout(names, 1)
        layout.addLayout(brand_row)
        layout.addSpacing(16)

        self.nav_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in (("home", "启动游戏"), ("download", "下载中心"), ("settings", "设置")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("nav")
            button.clicked.connect(lambda _c, name=key: self.show_page(name))
            group.addButton(button)
            layout.addWidget(button)
            self.nav_buttons[key] = button
        layout.addSpacing(14)

        layout.addWidget(self._label("内容下载"))
        for key, text in CONTENT_TABS:
            button = QPushButton(text)
            button.setObjectName("nav")
            button.clicked.connect(lambda _c, name=key: self.goto_content(name))
            layout.addWidget(button)

        layout.addStretch(1)

        self.account_button = QPushButton(self.config.username)
        self.account_button.setObjectName("nav")
        self.account_button.setIcon(QIcon(initial_pixmap(self.config.username, 26, "account")))
        self.account_button.setIconSize(QSize(24, 24))
        self.account_button.setToolTip("切换离线账户")
        self.account_button.clicked.connect(self._edit_account)
        layout.addWidget(self.account_button)

        log_button = QPushButton("运行日志")
        log_button.setObjectName("nav")
        log_button.clicked.connect(self.open_log_dialog)
        layout.addWidget(log_button)

        source_button = QPushButton("下载源")
        source_button.setObjectName("nav")
        source_button.setToolTip("F12 也可以打开")
        source_button.clicked.connect(self.open_source_dialog)
        layout.addWidget(source_button)
        return bar

    def _build_home_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        hero = Card(margins=(22, 20, 22, 18))
        hero.setFixedWidth(348)

        account_row = QHBoxLayout()
        account_row.setSpacing(10)
        self.avatar = QLabel()
        self.avatar.setFixedSize(40, 40)
        self.avatar.setPixmap(initial_pixmap(self.config.username, 40, "account"))
        account_row.addWidget(self.avatar)
        account_text = QVBoxLayout()
        account_text.setSpacing(0)
        self.account_name = QLabel(self.config.username)
        self.account_name.setStyleSheet("font-weight: 700;")
        account_text.addWidget(self.account_name)
        account_text.addWidget(self._label("离线账户"))
        account_row.addLayout(account_text, 1)
        switch = QPushButton("切换")
        switch.setObjectName("segment")
        switch.clicked.connect(self._edit_account)
        account_row.addWidget(switch)
        hero.add_layout(account_row)
        hero.add(self._hline())

        self.hero_icon = QLabel()
        self.hero_icon.setFixedSize(84, 84)
        self.hero_icon.setPixmap(block_pixmap("minecraft", 84))
        icon_row = QHBoxLayout()
        icon_row.addStretch(1)
        icon_row.addWidget(self.hero_icon)
        icon_row.addStretch(1)
        hero.add_layout(icon_row)

        self.hero_name = QLabel("未选择版本")
        self.hero_name.setObjectName("heroName")
        self.hero_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero.add(self.hero_name)

        badges = QHBoxLayout()
        badges.setSpacing(6)
        badges.addStretch(1)
        self.hero_loader = Badge("原版", "gray")
        badges.addWidget(self.hero_loader)
        self.hero_mc = Badge("MC -", "blue")
        badges.addWidget(self.hero_mc)
        badges.addStretch(1)
        hero.add_layout(badges)

        self.hero_meta = QLabel("从「下载中心」安装一个游戏版本开始")
        self.hero_meta.setObjectName("muted")
        self.hero_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hero_meta.setWordWrap(True)
        hero.add(self.hero_meta)
        hero.body.addStretch(1)
        hero.add(self._hline())

        memory_row = QHBoxLayout()
        memory_row.setSpacing(8)
        memory_row.addWidget(self._label("内存"))
        self.home_memory = QSlider(Qt.Orientation.Horizontal)
        self.home_memory.setRange(1024, 32768)
        self.home_memory.setSingleStep(512)
        self.home_memory.setPageStep(1024)
        self.home_memory.setValue(int(self.config.memory_mb))
        self.home_memory.valueChanged.connect(self._on_memory_changed)
        memory_row.addWidget(self.home_memory, 1)
        self.memory_value = QLabel(f"{self.config.memory_mb} MB")
        self.memory_value.setMinimumWidth(76)
        self.memory_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.memory_value.setStyleSheet("font-weight: 700;")
        memory_row.addWidget(self.memory_value)
        hero.add_layout(memory_row)

        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setObjectName("accentBig")
        self.launch_button.setFixedHeight(56)
        self.launch_button.clicked.connect(self._launch_or_stop)
        hero.add(self.launch_button)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        instance_button = QPushButton("版本设置")
        instance_button.clicked.connect(self._open_instance_dialog)
        actions.addWidget(instance_button, 1)
        open_button = QPushButton("打开目录")
        open_button.clicked.connect(self._open_game_dir)
        actions.addWidget(open_button, 1)
        hero.add_layout(actions)

        layout.addWidget(hero)

        right = Card(margins=(16, 14, 16, 14))
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self._label("版本列表", "h2"))
        header.addStretch(1)
        self.instance_count = self._label("")
        header.addWidget(self.instance_count)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_instances)
        header.addWidget(refresh)
        right.add_layout(header)

        self.version_tree = QTreeWidget()
        self.version_tree.setHeaderLabels(["版本", "加载器", "大小", "最近使用"])
        self.version_tree.setRootIsDecorated(False)
        self.version_tree.setUniformRowHeights(True)
        self.version_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.version_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.version_tree.customContextMenuRequested.connect(self._version_menu)
        self.version_tree.itemSelectionChanged.connect(self._on_version_selected)
        self.version_tree.itemDoubleClicked.connect(self._on_version_activated)
        head = self.version_tree.header()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        right.add(self.version_tree, 1)

        tiles = QGridLayout()
        tiles.setSpacing(10)
        entries = [
            ("下载游戏", "正式版 / 快照 / 远古版", lambda: self.goto_tab("game")),
            ("模组", "从 Modrinth 安装", lambda: self.goto_content("mod")),
            ("整合包", "一键安装完整实例", lambda: self.goto_content("modpack")),
            ("资源包", "改变贴图与音效", lambda: self.goto_content("resourcepack")),
            ("光影", "Iris / OptiFine 光影", lambda: self.goto_content("shader")),
            ("数据目录", "打开启动器数据目录", self._open_data_dir),
        ]
        for index, (title, hint, callback) in enumerate(entries):
            tiles.addWidget(Tile(title, hint, callback), index // 3, index % 3)
        right.add_layout(tiles)
        layout.addWidget(right, 1)
        return page

    def _build_download_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        header = QHBoxLayout()
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(self._label("下载中心", "h2"))
        self.download_sub = self._label("游戏版本、模组、整合包、资源包与光影，全部经由自建镜像加速")
        head.addWidget(self.download_sub)
        header.addLayout(head, 1)
        self.source_badge = Badge("镜像", "green")
        header.addWidget(self.source_badge)
        layout.addLayout(header)

        tabs = QFrame()
        tabs.setObjectName("softCard")
        tabs_layout = QHBoxLayout(tabs)
        tabs_layout.setContentsMargins(6, 6, 6, 6)
        tabs_layout.setSpacing(4)
        self.tab_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in [("game", "游戏版本")] + CONTENT_TABS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("segment")
            button.setMinimumHeight(30)
            button.clicked.connect(lambda _c, name=key: self.switch_tab(name))
            group.addButton(button)
            tabs_layout.addWidget(button)
            self.tab_buttons[key] = button
        tabs_layout.addStretch(1)
        layout.addWidget(tabs)

        self.dl_stack = QStackedWidget()
        self.dl_stack.addWidget(self._build_game_tab())
        self.content_pages: dict[str, ContentPage] = {}
        for key, _text in CONTENT_TABS:
            content = ContentPage(self, key)
            self.content_pages[key] = content
            self.dl_stack.addWidget(content)
        layout.addWidget(self.dl_stack, 1)

        self.tab_buttons["game"].setChecked(True)
        return page

    def _build_game_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        banner = QFrame()
        banner.setObjectName("softCard")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(16, 12, 16, 12)
        banner_layout.setSpacing(12)
        self.banner_icon = QLabel()
        self.banner_icon.setFixedSize(44, 44)
        self.banner_icon.setPixmap(block_pixmap("latest", 44))
        banner_layout.addWidget(self.banner_icon)
        banner_text = QVBoxLayout()
        banner_text.setSpacing(1)
        self.banner_title = QLabel("最新正式版")
        self.banner_title.setObjectName("h3")
        banner_text.addWidget(self.banner_title)
        self.banner_hint = self._label("正在获取版本清单……")
        banner_text.addWidget(self.banner_hint)
        banner_layout.addLayout(banner_text, 1)
        self.banner_button = QPushButton("安装最新版")
        self.banner_button.setObjectName("accent")
        self.banner_button.setEnabled(False)
        self.banner_button.clicked.connect(self._install_latest)
        banner_layout.addWidget(self.banner_button)
        layout.addWidget(banner)

        filters = Card(margins=(12, 10, 12, 10))
        row = QHBoxLayout()
        row.setSpacing(10)
        segment_wrap = QFrame()
        segment_wrap.setObjectName("softCard")
        segment_layout = QHBoxLayout(segment_wrap)
        segment_layout.setContentsMargins(4, 3, 4, 3)
        segment_layout.setSpacing(2)
        self.segment_buttons: dict[str, QPushButton] = {}
        segment_group = QButtonGroup(self)
        segment_group.setExclusive(True)
        for key, text in SEGMENTS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("segment")
            button.clicked.connect(lambda _c, name=key: self._on_segment(name))
            segment_group.addButton(button)
            segment_layout.addWidget(button)
            self.segment_buttons[key] = button
        self.segment_buttons["release"].setChecked(True)
        row.addWidget(segment_wrap)
        row.addStretch(1)
        self.game_search = QLineEdit()
        self.game_search.setPlaceholderText("搜索版本号")
        self.game_search.setMaximumWidth(220)
        self.game_search.textChanged.connect(lambda _text: self._render_versions())
        row.addWidget(self.game_search)
        filters.add_layout(row)
        layout.addWidget(filters)

        body = Card(margins=(12, 10, 12, 10))
        self.game_tree = QTreeWidget()
        self.game_tree.setHeaderLabels(["版本", "类型", "发布时间", "状态"])
        self.game_tree.setRootIsDecorated(False)
        self.game_tree.setUniformRowHeights(True)
        self.game_tree.itemDoubleClicked.connect(lambda _i, _c: self._on_game_action())
        head = self.game_tree.header()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        body.add(self.game_tree, 1)
        footer = QHBoxLayout()
        self.game_count = self._label("")
        footer.addWidget(self.game_count)
        footer.addStretch(1)
        self.game_install_button = QPushButton("安装所选版本")
        self.game_install_button.setObjectName("accent")
        self.game_install_button.clicked.connect(self._on_game_action)
        footer.addWidget(self.game_install_button)
        body.add_layout(footer)
        layout.addWidget(body, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(12)

        game = Card()
        game.add(self._label("游戏", "h2"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.settings_game_dir = QLineEdit(self.config.game_dir or str(self.game_dir()))
        row.addWidget(self.settings_game_dir, 1)
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse_game_dir)
        row.addWidget(browse)
        game.add_layout(row)
        game.add(self._label("游戏目录：版本、模组、存档与配置都保存在这里"))
        self.settings_isolate = QCheckBox("版本隔离（每个版本独立 mods / 存档 / 配置，推荐开启）")
        self.settings_isolate.setChecked(bool(self.config.isolation))
        game.add(self.settings_isolate)
        self.settings_skip_assets = QCheckBox("安装时跳过资源文件（assets，体积更小但部分音效缺失）")
        self.settings_skip_assets.setChecked(bool(self.config.skip_assets))
        game.add(self.settings_skip_assets)
        layout.addWidget(game)

        java = Card()
        java.add(self._label("Java 与内存", "h2"))
        java_row = QHBoxLayout()
        java_row.setSpacing(8)
        self.settings_java = QComboBox()
        java_row.addWidget(self.settings_java, 1)
        detect = QPushButton("重新检测")
        detect.clicked.connect(self._refresh_java_combo)
        java_row.addWidget(detect)
        add_java = QPushButton("添加 Java")
        add_java.clicked.connect(self._choose_java)
        java_row.addWidget(add_java)
        java.add_layout(java_row)
        self.settings_java.currentIndexChanged.connect(self._on_java_changed)

        memory_row = QHBoxLayout()
        memory_row.setSpacing(10)
        memory_row.addWidget(self._label("内存"))
        self.settings_memory = QSlider(Qt.Orientation.Horizontal)
        self.settings_memory.setRange(1024, 32768)
        self.settings_memory.setSingleStep(512)
        self.settings_memory.setPageStep(1024)
        self.settings_memory.setValue(int(self.config.memory_mb))
        memory_row.addWidget(self.settings_memory, 1)
        self.settings_memory_value = QLabel(f"{self.config.memory_mb} MB")
        self.settings_memory_value.setMinimumWidth(76)
        self.settings_memory_value.setStyleSheet("font-weight: 700;")
        memory_row.addWidget(self.settings_memory_value)
        java.add_layout(memory_row)
        self.settings_memory.valueChanged.connect(
            lambda value: self.settings_memory_value.setText(f"{value} MB")
        )

        self.settings_jvm = QLineEdit(" ".join(self.config.extra_jvm))
        self.settings_jvm.setPlaceholderText("额外的 JVM 参数，例如 -XX:+UseG1GC")
        java.add(self.settings_jvm)
        layout.addWidget(java)

        source = Card()
        source.add(self._label("下载源", "h2"))
        source.add(self._label("客户端只与下载源通信，游戏文件 / Fabric / Modrinth 都经它代理"))
        self.settings_base = QLineEdit(self.config.download_base)
        source.add(self.settings_base)
        pair = QHBoxLayout()
        pair.setSpacing(8)
        self.settings_token = QLineEdit(self.config.download_token)
        self.settings_token.setPlaceholderText("访问令牌（可选）")
        pair.addWidget(self.settings_token, 1)
        self.settings_proxy = QLineEdit(self.config.proxy)
        self.settings_proxy.setPlaceholderText("代理（可选，如 http://127.0.0.1:7890）")
        pair.addWidget(self.settings_proxy, 1)
        source.add_layout(pair)
        workers_row = QHBoxLayout()
        workers_row.setSpacing(8)
        workers_row.addWidget(self._label("并发下载数"))
        self.settings_workers = QSpinBox()
        self.settings_workers.setRange(1, 32)
        self.settings_workers.setValue(int(self.config.max_workers))
        workers_row.addWidget(self.settings_workers)
        workers_row.addStretch(1)
        test = QPushButton("测试连接")
        test.clicked.connect(self._test_source)
        workers_row.addWidget(test)
        source.add_layout(workers_row)
        self.settings_source_state = self._label("")
        source.add(self.settings_source_state)
        layout.addWidget(source)

        about = Card()
        about.add(self._label("关于", "h2"))
        about.add(self._label(f"{APP_NAME} v{__version__} · 自建镜像启动器，仅依赖 Python 标准库 + PyQt6"))
        about.add(self._label(f"数据目录：{config_dir(self.data_dir)}"))
        about_row = QHBoxLayout()
        about_row.setSpacing(8)
        open_data = QPushButton("打开数据目录")
        open_data.clicked.connect(self._open_data_dir)
        about_row.addWidget(open_data)
        open_log = QPushButton("运行日志")
        open_log.clicked.connect(self.open_log_dialog)
        about_row.addWidget(open_log)
        source_dialog = QPushButton("下载源设置（F12）")
        source_dialog.clicked.connect(self.open_source_dialog)
        about_row.addWidget(source_dialog)
        about_row.addStretch(1)
        about.add_layout(about_row)
        layout.addWidget(about)

        layout.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        save = QPushButton("保存设置")
        save.setObjectName("accent")
        save.clicked.connect(self.save_settings)
        actions.addWidget(save)
        outer.addLayout(actions)
        return page

    def _build_task_panel(self) -> QWidget:
        self.task_panel = Card(margins=(14, 10, 14, 10))
        row = QHBoxLayout()
        row.setSpacing(10)
        self.task_title = QLabel("")
        self.task_title.setObjectName("h3")
        row.addWidget(self.task_title)
        self.task_phase = self._label("")
        row.addWidget(self.task_phase, 1)
        self.task_detail = self._label("")
        row.addWidget(self.task_detail)
        self.task_percent = QLabel("0%")
        self.task_percent.setStyleSheet("font-weight: 700;")
        row.addWidget(self.task_percent)
        self.task_cancel = QPushButton("取消")
        self.task_cancel.clicked.connect(self.cancel_task)
        row.addWidget(self.task_cancel)
        self.task_panel.add_layout(row)
        self.task_bar = QProgressBar()
        self.task_bar.setRange(0, 100)
        self.task_bar.setValue(0)
        self.task_panel.add(self.task_bar)
        self.task_panel.setVisible(False)
        return self.task_panel

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(6, 0, 4, 0)
        layout.setSpacing(8)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {OK}; font-size: 11pt;")
        layout.addWidget(self.status_dot)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("status")
        layout.addWidget(self.status_label, 1)
        self.stats_label = self._label("")
        layout.addWidget(self.stats_label)
        return bar

    # ------------------------------------------------------------- 导航
    def show_page(self, name: str) -> None:
        if name not in self.pages:
            return
        self.stack.setCurrentWidget(self.pages[name])
        for key, button in self.nav_buttons.items():
            button.setChecked(key == name)
        if name == "home":
            self.refresh_instances()
        elif name == "download":
            self.switch_tab(self._current_tab)
            self._refresh_source_badge()
        elif name == "settings":
            self._load_settings_values()

    def goto_content(self, kind: str) -> None:
        self.show_page("download")
        self.switch_tab(kind)

    def goto_tab(self, key: str) -> None:
        self.show_page("download")
        self.switch_tab(key)

    def switch_tab(self, key: str) -> None:
        order = ["game"] + [name for name, _text in CONTENT_TABS]
        if key not in order:
            key = "game"
        self._current_tab = key
        self.dl_stack.setCurrentIndex(order.index(key))
        for name, button in self.tab_buttons.items():
            button.setChecked(name == key)
        if key == "game":
            self._render_versions()
            return
        page = self.content_pages.get(key)
        if page is None:
            return
        if page.hits or page.offset:
            page.sync_filters()
        else:
            page.refresh()

    def _refresh_source_badge(self) -> None:
        base = self.config.download_base or ""
        if base:
            host = base.split("//", 1)[-1].split("/")[0]
            self.source_badge.set(f"镜像 · {host}", "green")
            self.download_sub.setToolTip(f"游戏与内容均通过 {base} 下载")
        else:
            self.source_badge.set("官方源", "gray")
            self.download_sub.setToolTip("未配置下载源，将直连官方地址")

    # ------------------------------------------------------------- 实例
    def game_dir(self) -> Path:
        return Path(self.config.game_dir) if self.config.game_dir else default_game_dir()

    def game_dir_for(self, instance: Instance | None) -> Path:
        mc_dir = self.game_dir()
        if instance is None:
            return mc_dir
        isolated = bool(self.config.isolation) or read_pack_metadata(mc_dir, instance.id) is not None
        return instance_game_dir(mc_dir, instance.id, isolated)

    def selected_instance(self) -> Instance | None:
        return self.selected

    def refresh_instances(self) -> None:
        def worker() -> None:
            try:
                found = discover_instances(self.game_dir())
            except Exception as exc:  # noqa: BLE001
                self.post_log("ERROR", f"读取实例失败：{exc}")
                return
            self.events.put(("instances", found))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_instances(self, instances: list[Instance]) -> None:
        previous = self.selected.id if self.selected is not None else (self.config.last_version or "")
        self.instances = list(instances)
        self.version_tree.blockSignals(True)
        self.version_tree.clear()
        for item in self.instances:
            node = QTreeWidgetItem(
                [item.id, item.loader, human_size(item.size_bytes), human_date(item.mtime)]
            )
            node.setData(0, Qt.ItemDataRole.UserRole, item)
            node.setIcon(0, QIcon(block_pixmap(item.id, 26, item.loader)))
            if item.broken:
                node.setToolTip(0, item.broken)
            self.version_tree.addTopLevelItem(node)
        self.version_tree.blockSignals(False)

        target = None
        for index in range(self.version_tree.topLevelItemCount()):
            node = self.version_tree.topLevelItem(index)
            if node is not None and node.text(0) == previous:
                target = node
                break
        if target is None and self.version_tree.topLevelItemCount():
            target = self.version_tree.topLevelItem(0)
        if target is not None:
            self.version_tree.setCurrentItem(target)
        self.selected = target.data(0, Qt.ItemDataRole.UserRole) if target is not None else None
        self._update_hero()

        total = sum(item.size_bytes for item in self.instances)
        self.instance_count.setText(f"{len(self.instances)} 个实例")
        self.stats_label.setText(f"{len(self.instances)} 个实例 · {human_size(total)}")
        self._render_versions()

    def _on_version_selected(self) -> None:
        node = self.version_tree.currentItem()
        self.selected = node.data(0, Qt.ItemDataRole.UserRole) if node is not None else None
        self._update_hero()

    def _on_version_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        instance = item.data(0, Qt.ItemDataRole.UserRole)
        if instance is not None:
            self.launch_game(instance)

    def _update_hero(self) -> None:
        instance = self.selected
        if instance is None:
            self.hero_name.setText("未选择版本")
            self.hero_icon.setPixmap(block_pixmap("minecraft", 84))
            self.hero_loader.set("原版", "gray")
            self.hero_mc.set("MC -", "gray")
            self.hero_meta.setText("从「下载中心」安装一个游戏版本开始")
            self.launch_button.setEnabled(False)
            return
        self.hero_name.setText(instance.id)
        self.hero_icon.setPixmap(block_pixmap(instance.id, 84, instance.loader))
        self.hero_loader.set(instance.loader, LOADER_TONES.get(instance.loader, "gray"))
        self.hero_mc.set(f"MC {instance.mc_version}", "blue")
        parts = [human_size(instance.size_bytes), f"最近使用 {human_date(instance.mtime)}"]
        pack = read_pack_metadata(self.game_dir(), instance.id)
        if pack and pack.get("name"):
            parts.insert(0, f"整合包 {pack.get('name')}")
        if instance.broken:
            parts.append("版本 JSON 损坏")
        self.hero_meta.setText("  ·  ".join(parts))
        self.launch_button.setEnabled(True)

    def _on_memory_changed(self, value: int) -> None:
        self.config.memory_mb = int(value)
        self.memory_value.setText(f"{value} MB")
        if hasattr(self, "settings_memory"):
            self.settings_memory.blockSignals(True)
            self.settings_memory.setValue(int(value))
            self.settings_memory.blockSignals(False)
            self.settings_memory_value.setText(f"{value} MB")

    def _version_menu(self, pos) -> None:
        node = self.version_tree.itemAt(pos)
        if node is None:
            return
        self.version_tree.setCurrentItem(node)
        instance = node.data(0, Qt.ItemDataRole.UserRole)
        if instance is None:
            return
        menu = QMenu(self)
        launch = menu.addAction("启动游戏")
        menu.addSeparator()
        settings = menu.addAction("版本设置")
        menu.addSeparator()
        open_game = menu.addAction("打开游戏目录")
        open_version = menu.addAction("打开版本目录")
        menu.addSeparator()
        remove = menu.addAction("删除实例")
        chosen = menu.exec(self.version_tree.viewport().mapToGlobal(pos))
        if chosen is launch:
            self.launch_game(instance)
        elif chosen is settings:
            self._open_instance_dialog()
        elif chosen is open_game:
            _reveal(self.game_dir_for(instance))
        elif chosen is open_version:
            _reveal(self.game_dir() / "versions" / instance.id)
        elif chosen is remove:
            self._delete_instance(instance)

    def _delete_instance(self, instance: Instance) -> None:
        answer = QMessageBox.question(
            self, APP_NAME, f"确定删除实例 {instance.id}？此操作不可恢复。"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(self.game_dir() / "versions" / instance.id, ignore_errors=True)
        self.notice(f"已删除实例 {instance.id}", "WARN")
        self.refresh_instances()

    def _edit_account(self) -> None:
        dialog = AccountDialog(self, self.config.username)
        if not dialog.exec():
            return
        name = dialog.value()
        try:
            validate_name(name)
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        self.config.username = name
        save_config(self.config, self.data_dir)
        self.account_name.setText(name)
        self.avatar.setPixmap(initial_pixmap(name, 40, "account"))
        self.account_button.setText(name)
        self.account_button.setIcon(QIcon(initial_pixmap(name, 26, "account")))
        self.notice(f"已切换离线账户：{name}")

    # ------------------------------------------------------------- 启动
    def _launch_or_stop(self) -> None:
        if self.process is not None and self.process.running:
            self.stop_game()
            return
        if self.selected is None:
            self.notice("请先选择一个版本", "WARN")
            return
        self.launch_game(self.selected)

    def launch_game(self, instance: Instance) -> None:
        if self.process is not None and self.process.running:
            self.notice("游戏已经在运行了", "WARN")
            return
        save_config(self.config, self.data_dir)
        game_dir = self.game_dir()
        target_dir = self.game_dir_for(instance)
        options = LaunchOptions(
            memory_mb=self.config.memory_mb,
            width=self.config.width,
            height=self.config.height,
            fullscreen=self.config.fullscreen,
            java_path=self.config.java_overrides.get(instance.id)
            or self.config.java_overrides.get("__global__"),
            extra_jvm=list(self.config.extra_jvm),
            game_dir=str(target_dir),
        )

        def worker(cancel: threading.Event) -> str:
            version = load_version(game_dir, instance.id)
            profile = make_profile(self.config.username)
            plan = build_command(version, game_dir, profile, options=options)
            for warning in plan.warnings:
                self.post_log("WARN", warning)
            self.post_log("INFO", f"启动 {instance.id}（{plan.java.display}）")

            def on_line(line: str) -> None:
                self.post_log("INFO", line)

            def on_exit(code: int) -> None:
                level = "INFO" if code == 0 else "ERROR"
                self.post_log(level, f"游戏进程退出，代码 {code}")
                self.events.put(("launch_done", None))

            proc = GameProcess(plan, on_line=on_line, on_exit=on_exit)
            self.process = proc
            proc.start()
            self.events.put(("launch_started", None))
            return "游戏运行中"

        self.run_task(f"启动 {instance.id}", worker)

    def stop_game(self) -> None:
        if self.process is not None:
            self.post_log("WARN", "正在结束游戏……")
            self.process.stop()

    # ------------------------------------------------------------- 版本下载
    def _on_segment(self, key: str) -> None:
        self._segment = key
        self._render_versions()

    def _render_versions(self) -> None:
        if not hasattr(self, "game_tree"):
            return
        self.game_tree.clear()
        self._version_rows = []

        if self._segment == "installed":
            self.game_tree.setHeaderLabels(["版本", "加载器", "最近使用", "大小"])
            for item in self.instances:
                node = QTreeWidgetItem(
                    [item.id, item.loader, human_date(item.mtime), human_size(item.size_bytes)]
                )
                node.setData(0, Qt.ItemDataRole.UserRole, ("instance", item))
                node.setIcon(0, QIcon(block_pixmap(item.id, 26, item.loader)))
                self.game_tree.addTopLevelItem(node)
            self.game_count.setText(f"{len(self.instances)} 个实例")
            self.game_install_button.setText("启动所选版本")
            self.game_install_button.setEnabled(bool(self.instances))
            return

        self.game_install_button.setText("安装所选版本")
        self.game_tree.setHeaderLabels(["版本", "类型", "发布时间", "状态"])
        manifest = self.manifest
        if manifest is None:
            self.game_count.setText("版本清单加载中……")
            self.game_install_button.setEnabled(False)
            return
        kinds = SEGMENT_KINDS.get(self._segment, {"release"})
        items = manifest.filter(kinds, self.game_search.text())
        installed = {item.id for item in self.instances}
        for summary in items[:400]:
            node = QTreeWidgetItem(
                [
                    summary.id,
                    summary.label,
                    summary.release_time[:10],
                    "已安装" if summary.id in installed else "可安装",
                ]
            )
            node.setData(0, Qt.ItemDataRole.UserRole, ("summary", summary))
            node.setIcon(0, QIcon(block_pixmap(summary.id, 26)))
            self.game_tree.addTopLevelItem(node)
            self._version_rows.append(summary)
        self.game_count.setText(f"{len(items)} 个版本，显示前 {len(self._version_rows)} 个")
        self.game_install_button.setEnabled(bool(self._version_rows))
        if self._version_rows:
            self.game_tree.setCurrentItem(self.game_tree.topLevelItem(0))

    def _on_game_action(self) -> None:
        node = self.game_tree.currentItem()
        if node is None:
            self.notice("请先选择一个版本", "WARN")
            return
        payload = node.data(0, Qt.ItemDataRole.UserRole)
        if not payload:
            return
        kind, value = payload
        if kind == "instance":
            self.launch_game(value)
        else:
            self.install_game(value)

    def _install_latest(self) -> None:
        if self.manifest is None or not self.manifest.latest_release:
            self.notice("版本清单尚未就绪", "WARN")
            return
        summary = self.manifest.find(self.manifest.latest_release)
        if summary is not None:
            self.install_game(summary)

    def install_game(self, summary: VersionSummary) -> None:
        manifest = self.manifest
        if manifest is None:
            self.notice("版本清单尚未就绪，请稍后再试", "WARN")
            return

        def worker(cancel: threading.Event) -> str:
            from offcraft.install.installer import install, prepare_install
            from offcraft.install.plan import InstallOptions

            downloader = self.make_downloader()
            mirror = Mirror(self.config.download_base)
            options = InstallOptions(include_assets=not self.config.skip_assets)
            plan = prepare_install(
                self.game_dir(), summary.id, manifest, mirror, downloader, options=options
            )
            self.post_log("INFO", f"{summary.id}：{plan.summary()}")
            for warning in plan.warnings:
                self.post_log("WARN", warning)
            result = install(plan, downloader, tracker=ProgressTracker(), cancel=cancel)
            if not result.ok:
                first = result.report.failed[0] if result.report.failed else (None, "未知错误")
                raise RuntimeError(f"{len(result.report.failed)} 个文件失败：{first[1]}")
            self.config.last_version = summary.id
            save_config(self.config, self.data_dir)
            return f"{summary.id} 安装完成，共 {len(result.report.succeeded)} 个文件"

        self.run_task(f"安装 {summary.id}", worker)

    # ------------------------------------------------------------- 内容下载
    def content_client(self) -> ModrinthClient:
        key = (self.config.download_base or "", self.config.proxy, self.config.download_token)
        if self._content_client is None or self._content_key != key:
            self._content_client = ModrinthClient(
                self.config.download_base or "", downloader=self.make_downloader(workers=4)
            )
            self._content_key = key
        return self._content_client

    def install_content(
        self,
        hit: ProjectHit,
        version: ProjectVersion,
        dest: Path,
        instance: Instance,
        kind: str,
    ) -> None:
        game_version = instance.mc_version
        loader = instance.loader.lower() if instance.loader in ("Fabric", "Forge", "NeoForge", "Quilt") else ""

        def worker(cancel: threading.Event) -> str:
            client = self.content_client()
            result = install_project_version(
                client,
                version,
                dest,
                game_version=game_version,
                loader=loader,
                cancel=cancel,
            )
            for warning in result.warnings:
                self.post_log("WARN", warning)
            if not result.files:
                raise RuntimeError("没有下载到任何文件")
            names = "、".join(path.name for path in result.files[:3])
            more = f" 等 {len(result.files)} 个文件" if len(result.files) > 3 else ""
            return f"已安装到 {instance.id}/{dest.name}：{names}{more}"

        self.run_task(f"安装 {hit.title}", worker)

    def install_modpack(self, hit: ProjectHit, version: ProjectVersion, name: str) -> None:
        file = version.primary_file
        if file is None:
            self.notice("该版本没有可下载的文件", "WARN")
            return

        def worker(cancel: threading.Event) -> str:
            from offcraft.content.modpack import install_modpack as install_mrpack

            client = self.content_client()
            cache = config_dir(self.data_dir) / "cache" / "modpacks"
            cache.mkdir(parents=True, exist_ok=True)
            archive = download_file(
                client.downloader,
                client.resolve_url(file.url),
                cache / safe_filename(file.filename),
                sha1=file.sha1,
                sha512=file.sha512,
                size=file.size or None,
                cancel=cancel,
            )
            self.set_phase("校验整合包")
            target_name = unique_instance_name(self.game_dir(), name or hit.title)
            downloader = self.make_downloader()
            mirror = Mirror(self.config.download_base or "https://mc.yjlt.top")
            manifest = self.ensure_manifest(cancel)
            result = install_mrpack(
                self.game_dir(),
                archive,
                mirror=mirror,
                downloader=downloader,
                manifest=manifest,
                instance_name=target_name,
                isolated=True,
                cancel=cancel,
                phase=self.set_phase,
            )
            for warning in result.warnings:
                self.post_log("WARN", warning)
            self.config.last_version = result.instance_id
            save_config(self.config, self.data_dir)
            return f"整合包 {result.instance_id} 安装完成（{result.installed}/{result.total} 个文件）"

        self.run_task(f"安装整合包 {hit.title}", worker)

    # ------------------------------------------------------------- 版本清单
    def ensure_manifest(self, cancel: threading.Event | None = None) -> VersionManifest:
        if self.manifest is not None:
            return self.manifest
        base = self.config.download_base or "https://mc.yjlt.top"
        downloader = self.make_downloader(progress=False, workers=2)
        data = downloader.fetch_json(Mirror(base).rewrite(MANIFEST_URL))
        manifest = VersionManifest.from_json(data)
        self.manifest = manifest
        try:
            path = config_dir(self.data_dir) / "cache" / "version_manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        self.events.put(("manifest", manifest))
        return manifest

    def refresh_remote(self) -> None:
        if self._remote_loading:
            return
        self._remote_loading = True
        self.status("正在获取版本清单……")

        def worker() -> None:
            try:
                self.ensure_manifest()
            except Exception as exc:  # noqa: BLE001
                self.post_log("ERROR", f"版本清单获取失败：{exc}")
                self.events.put(("status", "版本清单获取失败，可在「设置」中修改下载源"))
            finally:
                self._remote_loading = False

        threading.Thread(target=worker, daemon=True).start()

    def _load_cached_manifest(self) -> bool:
        path = config_dir(self.data_dir) / "cache" / "version_manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifest = VersionManifest.from_json(data)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        self._apply_manifest(manifest)
        self._append_log("INFO", f"已加载缓存的版本清单（{len(manifest.versions)} 个版本）")
        return True

    def _apply_manifest(self, manifest: VersionManifest) -> None:
        self.manifest = manifest
        if manifest.latest_release:
            summary = manifest.find(manifest.latest_release)
            self.banner_title.setText(f"最新正式版 {manifest.latest_release}")
            hint = f"发布于 {summary.release_time[:10]}" if summary else ""
            if manifest.latest_snapshot:
                hint = f"{hint}  ·  最新快照 {manifest.latest_snapshot}".strip()
            self.banner_hint.setText(hint)
            self.banner_icon.setPixmap(block_pixmap(manifest.latest_release, 44))
            self.banner_button.setEnabled(True)
        self._render_versions()
        self.status(f"版本清单已就绪：{len(manifest.versions)} 个版本")

    # ------------------------------------------------------------- 网络工具
    def make_downloader(self, *, progress: bool = True, workers: int | None = None) -> Downloader:
        return Downloader(
            proxy=self.config.proxy or None,
            token=self.config.download_token or None,
            workers=workers or self.config.max_workers,
            timeout=30,
            progress_callback=(
                (lambda tracker: self.events.put(("progress", tracker.snapshot())))
                if progress
                else None
            ),
            progress_interval=0.25,
        )

    def request_icon(
        self, url: str, callback: Callable[[QPixmap], None], size: int = 46
    ) -> None:
        if not url:
            return
        cached = self._icons.get(url)
        if cached is not None:
            callback(self._scale_icon(cached, size))
            return
        if url in self._icon_loading:
            self._icon_waiters.setdefault(url, []).append((callback, size))
            return
        self._icon_loading.add(url)
        self._icon_waiters[url] = [(callback, size)]

        def worker() -> None:
            data = b""
            try:
                data = self.make_downloader(progress=False, workers=1).fetch_bytes(
                    url, max_size=4 * 1024 * 1024
                )
            except Exception:  # noqa: BLE001 - 图标失败不影响主流程
                data = b""
            self.events.put(("icon", (url, data)))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _scale_icon(pixmap: QPixmap, size: int) -> QPixmap:
        if pixmap.width() <= size and pixmap.height() <= size:
            return pixmap
        return pixmap.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )

    def _apply_icon(self, payload: tuple[str, bytes]) -> None:
        url, data = payload
        self._icon_loading.discard(url)
        waiters = self._icon_waiters.pop(url, [])
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            return
        self._icons[url] = pixmap
        for callback, size in waiters:
            try:
                callback(self._scale_icon(pixmap, size))
            except RuntimeError:
                continue

    # ------------------------------------------------------------- 任务
    def run_task(
        self,
        title: str,
        worker: Callable[[threading.Event], str | None],
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.notice("已有任务在进行，请等待完成或取消", "WARN")
            return
        cancel = threading.Event()
        self.cancel_event = cancel
        self.task_panel.setVisible(True)
        self.task_title.setText(title)
        self.task_phase.setText("准备中……")
        self.task_detail.setText("")
        self.task_percent.setText("0%")
        self.task_bar.setRange(0, 0)
        self.task_cancel.setEnabled(True)
        self.status(title)
        self._append_log("INFO", title)

        def runner() -> None:
            message = ""
            error: str | None = None
            try:
                message = worker(cancel) or ""
            except Exception as exc:  # noqa: BLE001 - 统一回传到界面
                error = str(exc) or exc.__class__.__name__
            self.events.put(("task_done", (message, error, on_done)))

        threading.Thread(target=runner, daemon=True).start()

    def cancel_task(self) -> None:
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.cancel_event.set()
            self.task_cancel.setEnabled(False)
            self.task_phase.setText("正在取消……")
            self.post_log("WARN", "正在取消当前任务……")

    def _finish_task(self, message: str, error: str | None) -> None:
        self.task_panel.setVisible(False)
        self.task_bar.setRange(0, 100)
        self.task_bar.setValue(0)
        self.task_percent.setText("0%")
        self.task_detail.setText("")
        self.task_cancel.setEnabled(True)
        self.cancel_event = None
        if error:
            self._append_log("ERROR", error)
            self.status_dot.setStyleSheet(f"color: {DANGER}; font-size: 11pt;")
            self.status_label.setText(error if len(error) <= 140 else error[:137] + "…")
        else:
            if message:
                self._append_log("INFO", message)
            self.status_dot.setStyleSheet(f"color: {OK}; font-size: 11pt;")
            self.status_label.setText(message or "完成")
        self.refresh_instances()

    def _show_progress(self, snapshot: dict[str, Any]) -> None:
        percent = snapshot.get("percent")
        if percent is None:
            self.task_bar.setRange(0, 0)
            self.task_percent.setText(
                human_size(snapshot.get("done_bytes") or 0)
            )
        else:
            self.task_bar.setRange(0, 100)
            self.task_bar.setValue(int(percent))
            self.task_percent.setText(f"{percent:.0f}%")
        speed = snapshot.get("speed") or 0
        current = snapshot.get("current") or "下载中"
        self.task_detail.setText(
            f"{current}  ·  {human_size(snapshot.get('done_bytes') or 0)}"
            f" / {human_size(snapshot.get('total_bytes') or 0)}"
            f"  ·  {human_size(speed)}/s"
        )

    # ------------------------------------------------------------- 事件
    def post(self, callback: Callable[[], None]) -> None:
        self.events.put(("call", callback))

    def log(self, text: str, level: str = "INFO") -> None:
        self.post_log(level, text)

    def post_log(self, level: str, text: str) -> None:
        self.events.put(("log", (level, text)))

    def status(self, text: str) -> None:
        self.events.put(("status", text))

    def notice(self, text: str, level: str = "INFO") -> None:
        self.post_log(level, text)
        self.status(text if len(text) <= 140 else text[:137] + "…")

    def set_phase(self, text: str) -> None:
        self.events.put(("task_phase", text))

    def _append_log(self, level: str, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_records.append((stamp, level, text))

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "call":
                    payload()
                elif kind == "log":
                    level, text = payload
                    self._append_log(level, text)
                elif kind == "status":
                    self.status_label.setText(str(payload))
                elif kind == "task_phase":
                    self.task_phase.setText(str(payload))
                elif kind == "progress":
                    self._show_progress(payload)
                elif kind == "instances":
                    self._apply_instances(payload)
                elif kind == "manifest":
                    self._apply_manifest(payload)
                elif kind == "icon":
                    self._apply_icon(payload)
                elif kind == "launch_started":
                    self.launch_button.setText("结束游戏")
                elif kind == "launch_done":
                    self.process = None
                    self.launch_button.setText("启动游戏")
                    self.status_dot.setStyleSheet(f"color: {OK}; font-size: 11pt;")
                    self.status_label.setText("游戏已退出")
                elif kind == "task_done":
                    message, error, callback = payload
                    self._finish_task(message, error)
                    if callback is not None and error is None:
                        callback()
            except Exception as exc:  # noqa: BLE001 - 单条事件失败不应拖死界面
                self._append_log("ERROR", f"界面事件处理失败：{exc}")

    # ------------------------------------------------------------- 目录 / 日志
    def _open_game_dir(self) -> None:
        _reveal(self.game_dir())

    def _open_version_dir(self) -> None:
        if self.selected is None:
            return
        _reveal(self.game_dir() / "versions" / self.selected.id)

    def _open_data_dir(self) -> None:
        _reveal(config_dir(self.data_dir))

    def _open_instance_dialog(self) -> None:
        if self.selected is None:
            self.notice("请先选择一个版本", "WARN")
            return
        dialog = InstanceDialog(self, self.selected)
        dialog.exec()

    def open_log_dialog(self) -> None:
        LogDialog(self, list(self.log_records)).exec()

    def open_source_dialog(self) -> None:
        dialog = SourceDialog(self, self.config)
        if not dialog.exec():
            return
        dialog.apply()
        save_config(self.config, self.data_dir)
        self._refresh_source_badge()
        self.manifest = None
        self._content_client = None
        self.post_log("INFO", f"下载源已更新：{self.config.download_base}")
        self.refresh_remote()

    # ------------------------------------------------------------- 设置
    def _load_settings_values(self) -> None:
        if not hasattr(self, "settings_game_dir"):
            return
        self.settings_game_dir.setText(self.config.game_dir or str(self.game_dir()))
        self.settings_isolate.setChecked(bool(self.config.isolation))
        self.settings_skip_assets.setChecked(bool(self.config.skip_assets))
        self.settings_memory.blockSignals(True)
        self.settings_memory.setValue(int(self.config.memory_mb))
        self.settings_memory.blockSignals(False)
        self.settings_memory_value.setText(f"{self.config.memory_mb} MB")
        self.settings_jvm.setText(" ".join(self.config.extra_jvm))
        self.settings_base.setText(self.config.download_base)
        self.settings_token.setText(self.config.download_token)
        self.settings_proxy.setText(self.config.proxy)
        self.settings_workers.setValue(int(self.config.max_workers))
        self._refresh_java_combo()

    def save_settings(self) -> None:
        self.config.game_dir = self.settings_game_dir.text().strip()
        self.config.isolation = self.settings_isolate.isChecked()
        self.config.skip_assets = self.settings_skip_assets.isChecked()
        self.config.memory_mb = int(self.settings_memory.value())
        self.config.extra_jvm = [part for part in self.settings_jvm.text().split(" ") if part]
        self.config.download_base = (
            self.settings_base.text().strip().rstrip("/") or "https://mc.yjlt.top"
        )
        self.config.download_token = self.settings_token.text().strip()
        self.config.proxy = self.settings_proxy.text().strip()
        self.config.max_workers = int(self.settings_workers.value())
        save_config(self.config, self.data_dir)

        self.home_memory.blockSignals(True)
        self.home_memory.setValue(int(self.config.memory_mb))
        self.home_memory.blockSignals(False)
        self.memory_value.setText(f"{self.config.memory_mb} MB")

        self.manifest = None
        self._content_client = None
        self._refresh_source_badge()
        self._load_cached_manifest()
        self.refresh_remote()
        self.refresh_instances()
        self.notice("设置已保存")

    def _browse_game_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择 .minecraft 目录", str(self.game_dir()))
        if folder:
            self.settings_game_dir.setText(folder)

    def _refresh_java_combo(self) -> None:
        if not hasattr(self, "settings_java"):
            return
        candidates = list_java(self.game_dir())
        self.settings_java.blockSignals(True)
        self.settings_java.clear()
        self.settings_java.addItem("自动选择（按版本要求）", "")
        for candidate in candidates:
            self.settings_java.addItem(
                f"Java {candidate.major} · {candidate.source}", str(candidate.path)
            )
        override = self.config.java_overrides.get("__global__", "")
        index = self.settings_java.findData(override)
        self.settings_java.setCurrentIndex(index if index >= 0 else 0)
        self.settings_java.blockSignals(False)
        if not candidates and hasattr(self, "settings_source_state"):
            self.settings_source_state.setText("未检测到 Java，可点「添加 Java」手动指定")

    def _on_java_changed(self, index: int) -> None:
        if index < 0:
            return
        path = self.settings_java.itemData(index)
        if path:
            self.config.java_overrides["__global__"] = str(path)
        else:
            self.config.java_overrides.pop("__global__", None)

    def _choose_java(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择 java.exe", "", "Java (java.exe);;所有文件 (*.*)"
        )
        if not path:
            return
        major = probe_java(path)
        if major is None:
            QMessageBox.warning(self, APP_NAME, "无法识别该 Java，请确认是有效的 java.exe")
            return
        self.config.java_overrides["__global__"] = path
        save_config(self.config, self.data_dir)
        self._refresh_java_combo()
        index = self.settings_java.findData(path)
        if index >= 0:
            self.settings_java.setCurrentIndex(index)
        self.notice(f"已添加 Java {major}：{path}")

    def _test_source(self) -> None:
        base = self.settings_base.text().strip().rstrip("/") or "https://mc.yjlt.top"
        self.settings_source_state.setText("正在测试连接……")

        def worker() -> None:
            try:
                downloader = self.make_downloader(progress=False, workers=1)
                data = downloader.fetch_json(f"{base}/health")
            except Exception as exc:  # noqa: BLE001
                message = f"连接失败：{exc}"
            else:
                version = data.get("version") if isinstance(data, dict) else ""
                message = "连接正常" + (f" · 服务端 {version}" if version else "")
            self.post(lambda: self.settings_source_state.setText(message))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------- 收尾
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.config.window = {"w": self.width(), "h": self.height()}
        save_config(self.config, self.data_dir)
        if self.process is not None and self.process.running:
            answer = QMessageBox.question(self, APP_NAME, "游戏仍在运行，确定退出启动器？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.process.stop()
        self._timer.stop()
        event.accept()


def run_app(config: Config, data_dir: str | Path | None = None) -> None:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    os.environ.setdefault("QT_SCALE_FACTOR", "1")
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(app_qss())
    window = MainWindow(config, data_dir)
    window.show()
    app.exec()
