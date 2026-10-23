"""CrMCDownload 主窗口（PyQt6）。

界面参考 PCL2：左侧导航 + 卡片式内容区，右侧是可操作的主体。

- 「启动游戏」：账户、当前实例概览、内存滑杆、启动按钮、版本列表与快捷入口。
- 「下载中心」：游戏版本 + Modrinth 的模组 / 整合包 / 资源包 / 光影 + Java 运行时。
- 「插件」：插件市场 / 官方 / 社区 / 其他 / 已下载（仅框架，接口未接入）。
- 「设置」：游戏目录、版本隔离、Java、内存；下载源只读，按 F12 才可修改。

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

from PyQt6.QtCore import QPoint, QSize, Qt, QTimer
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
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
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
from offcraft.content.java import (
    JavaRuntime,
    install_java_runtime,
    list_java_runtimes,
)
from offcraft.content.loader import (
    LOADERS,
    LoaderBuild,
    install_loader,
    list_loader_versions,
    supports_mods,
)
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
from offcraft.ui.widgets import Badge, Card, ElidedLabel, Tile

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

#: 下载中心的内容页签
CONTENT_TABS = [
    ("mod", "模组"),
    ("modpack", "整合包"),
    ("resourcepack", "资源包"),
    ("shader", "光影"),
]

#: 顶部导航（顺序：启动 → 下载 → 设置 → 插件）
NAV_ITEMS = [
    ("home", "启动"),
    ("download", "下载"),
    ("settings", "设置"),
    ("plugins", "插件"),
]

#: 草图单位长度：1k = 16px（窗口 75k × 50k → 1200 × 800）
UNIT_PX = 16


def u(k: float) -> int:
    """把草图里的单位长度（k）换算成像素。"""
    return int(round(k * UNIT_PX))


#: 窗口长宽比 3:2（按草图单位长度换算）
WINDOW_RATIO = (3, 2)
WINDOW_DEFAULT = (u(75), u(50))
WINDOW_MIN = (u(67.5), u(45))

#: 顶部菜单栏 5k
TOPBAR_H = u(5)
#: 内容区左右分栏：左 20k / 右 55k
LEFT_W = u(20)
RIGHT_W = u(55)
#: 单侧内边距
SIDE_PAD = u(1.5)
#: 启动页头像边长（比草图小一些，避免占去左栏一半高度）
AVATAR_PX = 128

#: 下载中心全部页签（游戏版本 + 内容 + Java）
DOWNLOAD_TABS = [("game", "游戏版本")] + CONTENT_TABS + [("java", "Java")]

#: 插件页分区（仅框架）
PLUGIN_TABS = [
    ("market", "市场"),
    ("official", "官方"),
    ("community", "社区"),
    ("other", "其他"),
    ("downloaded", "已下载"),
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


class LoaderPickDialog(QDialog):
    """选择加载器与版本（安装游戏或给已有版本补装加载器时使用）。"""

    def __init__(
        self,
        window: "MainWindow",
        mc_version: str,
        *,
        allow_vanilla: bool = True,
        loader: str = "vanilla",
        title: str = "选择加载器",
    ) -> None:
        super().__init__(window)
        self.window = window
        self.mc_version = mc_version
        self._builds: list[LoaderBuild] = []
        self._token = 0
        self.setWindowTitle(title)
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        heading = QLabel(f"Minecraft {mc_version}")
        heading.setObjectName("h2")
        layout.addWidget(heading)
        layout.addWidget(QLabel("加载器决定能不能装模组与光影：原版只能装资源包。"))

        self.loader_combo = QComboBox()
        for key, text in LOADERS:
            if key == "vanilla" and not allow_vanilla:
                continue
            self.loader_combo.addItem(text, key)
        index = self.loader_combo.findData(loader)
        self.loader_combo.setCurrentIndex(index if index >= 0 else 0)
        self.loader_combo.currentIndexChanged.connect(self._load_versions)
        layout.addWidget(self.loader_combo)

        self.version_combo = QComboBox()
        layout.addWidget(self.version_combo)

        self.info = QLabel("")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.ok_button = QPushButton("安装" if allow_vanilla else "添加加载器")
        self.ok_button.setObjectName("accent")
        self.ok_button.clicked.connect(self._accept)
        buttons.addWidget(self.ok_button)
        layout.addLayout(buttons)

        self._load_versions()

    def _load_versions(self) -> None:
        key = str(self.loader_combo.currentData() or "vanilla")
        self._token += 1
        token = self._token
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        if key == "vanilla":
            self.version_combo.addItem("不安装加载器（原版）", "")
            self.version_combo.setEnabled(False)
            self.version_combo.blockSignals(False)
            self.info.setText("原版实例不能安装模组与光影，资源包不受影响。")
            return
        self.version_combo.setEnabled(False)
        self.version_combo.addItem("正在读取可用版本……", "")
        self.version_combo.blockSignals(False)
        self.info.setText("正在从镜像读取版本列表……")

        mirror = Mirror(self.window.config.download_base or "https://mc.yjlt.top")
        downloader = self.window.make_downloader(progress=False, workers=3)

        def worker() -> None:
            try:
                builds = list_loader_versions(
                    key, self.mc_version, mirror=mirror, downloader=downloader, limit=40
                )
            except Exception as exc:  # noqa: BLE001 - 展示给用户
                self.window.post(lambda: self._failed(token, str(exc)))
                return
            self.window.post(lambda: self._loaded(token, builds))

        threading.Thread(target=worker, daemon=True).start()

    def _loaded(self, token: int, builds: list[LoaderBuild]) -> None:
        if token != self._token:
            return
        self._builds = list(builds)
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        if not builds:
            self.version_combo.addItem("没有可用版本", "")
            self.version_combo.setEnabled(False)
            self.info.setText("该加载器没有适配这个 MC 版本，换一个加载器试试。")
        else:
            for build in builds:
                self.version_combo.addItem(build.label, build.version)
            self.version_combo.setEnabled(True)
            self.info.setText(
                f"共 {len(builds)} 个版本，默认最新；Forge / NeoForge 需要本机已安装 Java。"
            )
        self.version_combo.blockSignals(False)

    def _failed(self, token: int, message: str) -> None:
        if token != self._token:
            return
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        self.version_combo.addItem("获取失败", "")
        self.version_combo.setEnabled(False)
        self.version_combo.blockSignals(False)
        self.info.setText(f"版本列表获取失败：{message}")

    def _accept(self) -> None:
        key = str(self.loader_combo.currentData() or "vanilla")
        if key != "vanilla" and not str(self.version_combo.currentData() or ""):
            QMessageBox.information(self, APP_NAME, "请先选择一个加载器版本。")
            return
        self.accept()

    def choice(self) -> tuple[str, str]:
        key = str(self.loader_combo.currentData() or "vanilla")
        if key == "vanilla":
            return "vanilla", ""
        return key, str(self.version_combo.currentData() or ""),


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
        self.setMinimumSize(*WINDOW_MIN)
        width = int((config.window or {}).get("w", WINDOW_DEFAULT[0]) or WINDOW_DEFAULT[0])
        # 长宽比固定 3:2：宽度为准，高度由此推算
        height = int(round(width * WINDOW_RATIO[1] / WINDOW_RATIO[0]))
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            if height > available.height() - 60:
                height = max(WINDOW_MIN[1], available.height() - 60)
                width = int(round(height * WINDOW_RATIO[0] / WINDOW_RATIO[1]))
            width = min(width, available.width() - 40)
        width = max(width, WINDOW_MIN[0])
        height = max(int(round(width * WINDOW_RATIO[1] / WINDOW_RATIO[0])), WINDOW_MIN[1])
        self.resize(width, height)

        self._build()
        QShortcut(QKeySequence("F12"), self, activated=self.open_source_dialog)
        self._refresh_source_badge()
        self._refresh_java_combo()
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
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.topbar = self._build_topbar()
        root.addWidget(self.topbar)

        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {
            "home": self._build_home_page(),
            "download": self._build_download_page(),
            "plugins": self._build_plugins_page(),
            "settings": self._build_settings_page(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)
        root.addWidget(self.stack, 1)

        # 版本号（右下）、下载源（左下）、任务面板（浮层）都不占内容区高度
        self._build_overlays()
        self.show_page("home")
        self._place_overlays()

    def _build_topbar(self) -> QWidget:
        """顶部菜单栏（5k）：logo 在左，四项导航居中，右侧显示当前状态。"""
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(TOPBAR_H)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(u(1.5), 0, u(1.5), 0)
        layout.setSpacing(u(0.75))

        side = u(14)

        brand = QWidget()
        brand.setFixedWidth(side)
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(u(0.5))
        logo = QLabel()
        logo.setFixedSize(36, 36)
        logo.setPixmap(block_pixmap("crmcdownload", 36))
        brand_row.addWidget(logo)
        name = QLabel(APP_NAME)
        name.setObjectName("brand")
        brand_row.addWidget(name)
        brand_row.addStretch(1)
        layout.addWidget(brand)
        layout.addStretch(1)

        self.nav_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        nav_row = QHBoxLayout()
        nav_row.setSpacing(u(0.375))
        for key, text in NAV_ITEMS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("topnav")
            button.setMinimumHeight(40)
            button.setMinimumWidth(u(6))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c, page=key: self.show_page(page))
            group.addButton(button)
            nav_row.addWidget(button)
            self.nav_buttons[key] = button
        layout.addLayout(nav_row)
        layout.addStretch(1)

        status = QWidget()
        status.setFixedWidth(side)
        status_row = QHBoxLayout(status)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(u(0.375))
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet(f"color: {OK}; font-size: 11pt;")
        status_row.addWidget(self.status_dot)
        self.status_label = ElidedLabel("就绪")
        self.status_label.setObjectName("status")
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        status_row.addWidget(self.status_label, 1)
        layout.addWidget(status)
        return bar

    def _build_home_page(self) -> QWidget:
        """启动页：左 20k（头像 / 名称 / 启动），右 55k（版本条 / 模组存档 / 配置）。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        column.setSpacing(0)

        panel = Card(margins=(u(1.25), u(1.25), u(1.25), u(1.25)))

        self.avatar = QLabel()
        self.avatar.setFixedSize(AVATAR_PX, AVATAR_PX)
        self.avatar.setPixmap(initial_pixmap(self.config.username, AVATAR_PX, "account"))
        avatar_row = QHBoxLayout()
        avatar_row.setContentsMargins(0, 0, 0, 0)
        avatar_row.addStretch(1)
        avatar_row.addWidget(self.avatar)
        avatar_row.addStretch(1)
        panel.add_layout(avatar_row)

        caption = self._label("离线账户", "mutedSmall")
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        panel.add(caption)

        self.account_name = QLineEdit(self.config.username)
        self.account_name.setObjectName("heroName")
        self.account_name.setFixedHeight(u(2.5))
        self.account_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.account_name.setMaxLength(16)
        self.account_name.setToolTip("离线账户名：点击即可修改，回车生效")
        self.account_name.editingFinished.connect(self._apply_account_name)
        panel.add(self.account_name)

        panel.body.addStretch(1)
        self.home_hint = self._label("", "muted")
        self.home_hint.setWordWrap(True)
        self.home_hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        panel.add(self.home_hint)

        panel.body.addSpacing(u(0.75))
        self.launch_button = QPushButton("启动游戏")
        self.launch_button.setObjectName("accentBig")
        self.launch_button.setFixedHeight(u(3.5))
        self.launch_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.launch_button.clicked.connect(self._launch_or_stop)
        panel.add(self.launch_button)

        panel.body.addSpacing(u(0.75))
        actions = QHBoxLayout()
        actions.setSpacing(u(0.75))
        instance_button = QPushButton("版本设置")
        instance_button.setFixedHeight(u(2.25))
        instance_button.clicked.connect(self._open_instance_dialog)
        actions.addWidget(instance_button, 1)
        open_button = QPushButton("打开目录")
        open_button.setFixedHeight(u(2.25))
        open_button.clicked.connect(self._open_game_dir)
        actions.addWidget(open_button, 1)
        panel.add_layout(actions)

        column.addWidget(panel)
        layout.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, 0, 0, 0)
        right_column.setSpacing(0)
        # 5k + 25k + 15k = 45k，随窗口高度等比缩放
        right_column.addWidget(self._build_home_version_bar(), 5)
        right_column.addWidget(self._build_home_middle(), 25)
        right_column.addWidget(self._build_home_config(), 15)
        layout.addWidget(right, 1)
        return page

    def _build_home_version_bar(self) -> QWidget:
        """右侧顶部 5k：左边是当前版本（可下拉切换），右边是目录路径。"""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(SIDE_PAD, u(0.75), SIDE_PAD, u(0.75))
        row.setSpacing(u(0.75))

        self.version_combo = QComboBox()
        self.version_combo.setObjectName("heroVersion")
        self.version_combo.setMinimumWidth(u(17))
        self.version_combo.setFixedHeight(u(3.5))
        self.version_combo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version_combo.setToolTip("当前版本：点击下拉切换")
        self.version_combo.currentIndexChanged.connect(self._on_version_selected)
        self.version_combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.version_combo.customContextMenuRequested.connect(
            lambda pos: self._instance_menu(self.version_combo.mapToGlobal(pos))
        )
        row.addWidget(self.version_combo)

        self.instance_menu_button = QPushButton("⋯")
        self.instance_menu_button.setObjectName("heroMenu")
        self.instance_menu_button.setFixedSize(u(2.5), u(3.5))
        self.instance_menu_button.setToolTip("版本设置 / 安装加载器 / 打开目录 / 删除实例")
        self.instance_menu_button.clicked.connect(
            lambda: self._instance_menu(
                self.instance_menu_button.mapToGlobal(QPoint(u(0.5), u(3.5)))
            )
        )
        row.addWidget(self.instance_menu_button)

        self.path_label = ElidedLabel("")
        self.path_label.setObjectName("mutedSmall")
        self.path_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.path_label.setToolTip("当前版本的目录")
        row.addWidget(self.path_label, 1)
        return bar

    def _build_home_middle(self) -> QWidget:
        """右侧中部 25k：左 27k 是模组列表，右 18k 是存档列表。"""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        mods = Card(margins=(u(1.25), u(0.75), u(1.25), u(1)))
        head = QHBoxLayout()
        head.setSpacing(u(0.5))
        head.addWidget(self._label("模组", "h2"))
        self.mods_count = self._label("", "mutedSmall")
        head.addWidget(self.mods_count)
        head.addStretch(1)
        mods_open = QPushButton("打开目录")
        mods_open.setObjectName("segment")
        mods_open.setToolTip("打开当前版本的 mods 目录")
        mods_open.clicked.connect(self._open_mods_dir)
        head.addWidget(mods_open)
        mods.add_layout(head)
        self.mods_list = QListWidget()
        self.mods_list.setObjectName("homeList")
        self.mods_list.setFrameShape(QFrame.Shape.NoFrame)
        mods.add(self.mods_list, 1)
        row.addWidget(mods, 27)

        saves = Card(margins=(u(1.25), u(0.75), u(1.25), u(1)))
        head2 = QHBoxLayout()
        head2.setSpacing(u(0.5))
        head2.addWidget(self._label("存档", "h2"))
        self.saves_count = self._label("", "mutedSmall")
        head2.addWidget(self.saves_count)
        head2.addStretch(1)
        saves_open = QPushButton("打开目录")
        saves_open.setObjectName("segment")
        saves_open.setToolTip("打开当前版本的 saves 目录")
        saves_open.clicked.connect(self._open_saves_dir)
        head2.addWidget(saves_open)
        saves.add_layout(head2)
        self.saves_list = QListWidget()
        self.saves_list.setObjectName("homeList")
        self.saves_list.setFrameShape(QFrame.Shape.NoFrame)
        saves.add(self.saves_list, 1)
        row.addWidget(saves, 18)
        return holder

    def _build_home_config(self) -> QWidget:
        """右侧底部 15k：内存与 Java（下拉框修改）。"""
        card = Card(margins=(u(1.25), u(0.75), u(1.25), u(1.25)))
        head = QHBoxLayout()
        head.setSpacing(u(0.5))
        head.addWidget(self._label("配置", "h2"))
        head.addStretch(1)
        self.config_hint = self._label("", "mutedSmall")
        head.addWidget(self.config_hint)
        card.add_layout(head)

        memory_row = QHBoxLayout()
        memory_row.setSpacing(u(0.75))
        memory_row.addWidget(self._label("内存"))
        self.home_memory = QSlider(Qt.Orientation.Horizontal)
        self.home_memory.setRange(1024, 32768)
        self.home_memory.setSingleStep(512)
        self.home_memory.setPageStep(1024)
        self.home_memory.setValue(int(self.config.memory_mb))
        self.home_memory.valueChanged.connect(self._on_memory_changed)
        memory_row.addWidget(self.home_memory, 1)
        self.memory_value = QLabel(f"{self.config.memory_mb} MB")
        self.memory_value.setMinimumWidth(u(5))
        self.memory_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.memory_value.setStyleSheet("font-weight: 700;")
        memory_row.addWidget(self.memory_value)
        card.add_layout(memory_row)

        java_row = QHBoxLayout()
        java_row.setSpacing(u(0.75))
        java_row.addWidget(self._label("Java"))
        self.home_java = QComboBox()
        self.home_java.setToolTip("启动游戏使用的 Java（自动选择会按版本要求匹配）")
        self.home_java.currentIndexChanged.connect(self._on_home_java_changed)
        java_row.addWidget(self.home_java, 1)
        detect = QPushButton("重新检测")
        detect.clicked.connect(self._refresh_java_combo)
        java_row.addWidget(detect)
        card.add_layout(java_row)

        jvm_row = QHBoxLayout()
        jvm_row.setSpacing(u(0.75))
        jvm_row.addWidget(self._label("参数"))
        self.home_jvm = QLineEdit(" ".join(self.config.extra_jvm))
        self.home_jvm.setPlaceholderText("额外 JVM 参数，例如 -XX:+UseG1GC")
        self.home_jvm.editingFinished.connect(self._apply_home_jvm)
        jvm_row.addWidget(self.home_jvm, 1)
        card.add_layout(jvm_row)
        card.body.addStretch(1)

        more_row = QHBoxLayout()
        more_row.setSpacing(u(0.75))
        note = self._label("参数与 Java 会随启动命令一起生效", "mutedSmall")
        more_row.addWidget(note)
        more_row.addStretch(1)
        more = QPushButton("更多设置")
        more.setFixedHeight(u(2))
        more.clicked.connect(lambda: self.show_page("settings"))
        more_row.addWidget(more)
        card.add_layout(more_row)
        return card

    def _apply_home_jvm(self) -> None:
        """启动页的 JVM 参数：回车即保存，并同步设置页。"""
        self.config.extra_jvm = [part for part in self.home_jvm.text().split(" ") if part]
        save_config(self.config, self.data_dir)
        if hasattr(self, "settings_jvm"):
            self.settings_jvm.setText(" ".join(self.config.extra_jvm))

    def _home_content_dir(self, name: str) -> Path:
        instance = self.selected
        base = self.game_dir_for(instance) if instance is not None else self.game_dir()
        return base / name

    def _open_mods_dir(self) -> None:
        _reveal(self._home_content_dir("mods"))

    def _open_saves_dir(self) -> None:
        _reveal(self._home_content_dir("saves"))

    @staticmethod
    def _add_empty_item(widget: QListWidget, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        widget.addItem(item)

    @staticmethod
    def _list_files(folder: Path, suffixes: tuple[str, ...]) -> list[tuple[Path, bool]]:
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        found: list[tuple[Path, bool]] = []
        for path in entries:
            if not path.is_file():
                continue
            lowered = path.name.lower()
            if lowered.endswith(suffixes):
                found.append((path, lowered.endswith(".disabled")))
        return found

    @staticmethod
    def _list_dirs(folder: Path) -> list[Path]:
        try:
            return sorted(
                (path for path in folder.iterdir() if path.is_dir()),
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return []

    def _refresh_home_lists(self) -> None:
        """重新读取当前版本的 mods / saves 目录。"""
        if not hasattr(self, "mods_list"):
            return
        instance = self.selected
        base = self.game_dir_for(instance) if instance is not None else self.game_dir()
        mods = self._list_files(base / "mods", (".jar", ".zip", ".jar.disabled", ".zip.disabled"))
        self.mods_list.clear()
        for path, disabled in mods:
            text = f"{path.name}（已禁用）" if disabled else path.name
            item = QListWidgetItem(QIcon(block_pixmap(path.name, 20)), text)
            item.setToolTip(str(path))
            self.mods_list.addItem(item)
        self.mods_count.setText(f"{len(mods)} 个" if mods else "")
        if not mods:
            self._add_empty_item(self.mods_list, "暂无模组")

        saves = self._list_dirs(base / "saves")
        self.saves_list.clear()
        for folder in saves:
            item = QListWidgetItem(QIcon(block_pixmap(folder.name, 20)), folder.name)
            try:
                item.setToolTip(f"{folder}\n{human_date(folder.stat().st_mtime)}")
            except OSError:
                item.setToolTip(str(folder))
            self.saves_list.addItem(item)
        self.saves_count.setText(f"{len(saves)} 个" if saves else "")
        if not saves:
            self._add_empty_item(self.saves_list, "暂无存档")

    def _build_download_page(self) -> QWidget:
        """下载页：左侧 20k 分类导航，右侧 55k 内容。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        column.setSpacing(u(0.5))
        column.addWidget(self._label("下载", "title"))
        self.download_sub = self._label("游戏版本、模组、整合包、资源包与光影，全部经由自建镜像加速")
        self.download_sub.setWordWrap(True)
        column.addWidget(self.download_sub)
        column.addSpacing(u(1.5))

        self.tab_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in DOWNLOAD_TABS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("nav")
            button.setMinimumHeight(u(2.75))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c, name=key: self.switch_tab(name))
            group.addButton(button)
            column.addWidget(button)
            self.tab_buttons[key] = button
        column.addStretch(1)

        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        self.source_badge = Badge("镜像", "green")
        badge_row.addWidget(self.source_badge)
        badge_row.addStretch(1)
        column.addLayout(badge_row)
        layout.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, u(1), SIDE_PAD, u(2.5))
        right_column.setSpacing(0)

        self.dl_stack = QStackedWidget()
        self.dl_stack.addWidget(self._build_game_tab())
        self.content_pages: dict[str, ContentPage] = {}
        for key, _text in CONTENT_TABS:
            content = ContentPage(self, key)
            self.content_pages[key] = content
            self.dl_stack.addWidget(content)
        self.dl_stack.addWidget(self._build_java_tab())
        right_column.addWidget(self.dl_stack, 1)
        layout.addWidget(right, 1)

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

    def _build_plugins_page(self) -> QWidget:
        """插件页：左 20k 分区导航，右 55k 空状态框架。"""
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        column = QVBoxLayout(left)
        column.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        column.setSpacing(u(0.5))
        column.addWidget(self._label("插件", "title"))
        intro = self._label("启动器插件：市场 / 官方 / 社区 / 其他 / 已下载（接口未接入，先做框架）")
        intro.setWordWrap(True)
        column.addWidget(intro)
        column.addSpacing(u(1.5))

        self.plugin_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for key, text in PLUGIN_TABS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setObjectName("nav")
            button.setMinimumHeight(u(2.75))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _c, name=key: self._switch_plugin(name))
            group.addButton(button)
            column.addWidget(button)
            self.plugin_buttons[key] = button
        column.addStretch(1)
        self.plugin_badge = Badge("框架", "gray")
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.addWidget(self.plugin_badge)
        badge_row.addStretch(1)
        column.addLayout(badge_row)
        self.plugin_buttons["market"].setChecked(True)
        layout.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, u(1), SIDE_PAD, u(2.5))
        right_column.setSpacing(0)

        empty = Card(margins=(28, 28, 28, 22))
        icon_row = QHBoxLayout()
        icon_row.addStretch(1)
        icon = QLabel()
        icon.setFixedSize(72, 72)
        icon.setPixmap(block_pixmap("plugins", 72))
        icon_row.addWidget(icon)
        icon_row.addStretch(1)
        empty.add_layout(icon_row)
        self.plugin_title = QLabel("插件市场")
        self.plugin_title.setObjectName("h2")
        self.plugin_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.add(self.plugin_title)
        self.plugin_desc = QLabel("")
        self.plugin_desc.setObjectName("muted")
        self.plugin_desc.setWordWrap(True)
        self.plugin_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.add(self.plugin_desc)
        empty.body.addStretch(1)
        tiles = QGridLayout()
        tiles.setSpacing(10)
        entries = [
            ("浏览插件", "连接插件源（待接入）"),
            ("导入本地插件", "从 .zip / .py 安装（待接入）"),
            ("插件目录", "打开插件存放目录"),
        ]
        for index, (title, hint) in enumerate(entries):
            tiles.addWidget(Tile(title, hint, lambda _c=False, t=title: self._plugin_stub(t)), 0, index)
        empty.add_layout(tiles)
        right_column.addWidget(empty, 1)
        layout.addWidget(right, 1)
        self._switch_plugin("market")
        return page

    PLUGIN_DESCS = {
        "market": "插件市场：后续可对接插件索引，目前仅保留页面框架与导航。",
        "official": "官方插件：随启动器发布的插件列表将在这里展示。",
        "community": "社区插件：来自社区仓库的插件将在这里展示。",
        "other": "其他插件：手动导入或第三方来源的插件。",
        "downloaded": "已下载插件：本地已安装的插件与启用状态。",
    }

    def _switch_plugin(self, key: str) -> None:
        for name, button in self.plugin_buttons.items():
            button.setChecked(name == key)
        for name, text in PLUGIN_TABS:
            if name == key:
                self.plugin_title.setText(text)
                break
        self.plugin_desc.setText(self.PLUGIN_DESCS.get(key, ""))

    def _plugin_stub(self, title: str) -> None:
        self.notice(f"{title}：插件接口尚未接入，本节只做框架", "WARN")

    def _build_java_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        banner = QFrame()
        banner.setObjectName("softCard")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(16, 12, 16, 12)
        banner_layout.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(44, 44)
        icon.setPixmap(block_pixmap("java", 44))
        banner_layout.addWidget(icon)
        text = QVBoxLayout()
        text.setSpacing(1)
        title = QLabel("Java 运行时")
        title.setObjectName("h3")
        text.addWidget(title)
        text.addWidget(
            self._label("官方 Java 下载；启动游戏与运行 Forge / NeoForge 安装器都要用到")
        )
        banner_layout.addLayout(text, 1)
        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self._refresh_java_tab)
        banner_layout.addWidget(refresh)
        layout.addWidget(banner)

        body = Card(margins=(12, 10, 12, 10))
        self.java_tree = QTreeWidget()
        self.java_tree.setHeaderLabels(["组件", "版本", "主版本", "状态"])
        self.java_tree.setRootIsDecorated(False)
        self.java_tree.setUniformRowHeights(True)
        head = self.java_tree.header()
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        body.add(self.java_tree, 1)

        footer = QHBoxLayout()
        self.java_status = self._label("正在读取 Java 列表……")
        footer.addWidget(self.java_status, 1)
        self.java_set_button = QPushButton("设为启动 Java")
        self.java_set_button.clicked.connect(self._set_selected_java)
        footer.addWidget(self.java_set_button)
        self.java_open_button = QPushButton("打开目录")
        self.java_open_button.clicked.connect(self._open_java_dir)
        footer.addWidget(self.java_open_button)
        self.java_install_button = QPushButton("下载并安装")
        self.java_install_button.setObjectName("accent")
        self.java_install_button.clicked.connect(self._install_selected_java)
        footer.addWidget(self.java_install_button)
        body.add_layout(footer)
        layout.addWidget(body, 1)

        self.java_runtimes: list[JavaRuntime] = []
        self._java_loading = False
        return page

    def _refresh_java_tab(self) -> None:
        if getattr(self, "_java_loading", False):
            return
        self._java_loading = True
        self.java_status.setText("正在读取 Java 列表……")
        mirror = Mirror(self.config.download_base or "https://mc.yjlt.top")
        downloader = self.make_downloader(progress=False, workers=2)

        def worker() -> None:
            try:
                runtimes = list_java_runtimes(mirror, downloader)
            except Exception as exc:  # noqa: BLE001 - 展示给用户
                self.post(lambda: self._java_failed(str(exc)))
                return
            self.post(lambda: self._apply_java_runtimes(runtimes))

        threading.Thread(target=worker, daemon=True).start()

    def _java_failed(self, message: str) -> None:
        self._java_loading = False
        self.java_status.setText(f"获取失败：{message}")

    def _apply_java_runtimes(self, runtimes: list[JavaRuntime]) -> None:
        self._java_loading = False
        self.java_runtimes = list(runtimes)
        mc_dir = self.game_dir()
        self.java_tree.clear()
        installed = 0
        for runtime in runtimes:
            done = runtime.installed(mc_dir)
            installed += int(done)
            node = QTreeWidgetItem(
                [
                    runtime.component,
                    runtime.version or "-",
                    f"Java {runtime.major}" if runtime.major else "-",
                    "已安装" if done else "未安装",
                ]
            )
            node.setData(0, Qt.ItemDataRole.UserRole, runtime)
            node.setIcon(0, QIcon(block_pixmap(runtime.component, 22, "java")))
            self.java_tree.addTopLevelItem(node)
        if self.java_tree.topLevelItemCount():
            self.java_tree.setCurrentItem(self.java_tree.topLevelItem(0))
        self.java_status.setText(
            f"共 {len(runtimes)} 个运行时，已安装 {installed} 个（存于 {mc_dir / 'runtime'}）"
        )

    def _selected_java_runtime(self) -> JavaRuntime | None:
        node = getattr(self, "java_tree", None)
        if node is None:
            return None
        current = node.currentItem()
        return current.data(0, Qt.ItemDataRole.UserRole) if current is not None else None

    def _install_selected_java(self) -> None:
        runtime = self._selected_java_runtime()
        if runtime is None:
            self.notice("请先选择一个 Java 运行时", "WARN")
            return
        mc_dir = self.game_dir()
        mirror = Mirror(self.config.download_base or "https://mc.yjlt.top")

        def worker(cancel: threading.Event) -> str:
            downloader = self.make_downloader()
            path = install_java_runtime(
                mc_dir,
                runtime,
                mirror=mirror,
                downloader=downloader,
                cancel=cancel,
                phase=self.set_phase,
            )
            return f"Java 已安装：{path}"

        self.run_task(
            f"下载 {runtime.component}",
            worker,
            on_done=lambda: (self._refresh_java_tab(), self._refresh_java_combo()),
        )

    def _set_selected_java(self) -> None:
        runtime = self._selected_java_runtime()
        if runtime is None:
            self.notice("请先选择一个 Java 运行时", "WARN")
            return
        path = runtime.java_path(self.game_dir())
        if not path.is_file():
            self.notice("先下载这个运行时，再设为启动 Java", "WARN")
            return
        self.config.java_overrides["__global__"] = str(path)
        save_config(self.config, self.data_dir)
        self._refresh_java_combo()
        self.notice(f"启动 Java 已设为 {runtime.component}")

    def _open_java_dir(self) -> None:
        _reveal(self.game_dir() / "runtime")

    def _build_settings_page(self) -> QWidget:
        """设置页：左 20k 标题与操作，右 55k 设置卡片。"""
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        left = QWidget()
        left.setFixedWidth(LEFT_W)
        side = QVBoxLayout(left)
        side.setContentsMargins(SIDE_PAD, u(1), SIDE_PAD, u(2))
        side.setSpacing(u(0.75))
        side.addWidget(self._label("设置", "title"))
        intro = self._label(
            "游戏目录、版本隔离、Java 与内存在这里调整；下载源由发布者固定，只有按 F12 才能修改。"
        )
        intro.setWordWrap(True)
        side.addWidget(intro)
        side.addSpacing(u(1.5))
        save_button = QPushButton("保存设置")
        save_button.setObjectName("accent")
        save_button.setFixedHeight(u(2.5))
        save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        save_button.clicked.connect(self.save_settings)
        side.addWidget(save_button)
        data_button = QPushButton("打开数据目录")
        data_button.clicked.connect(self._open_data_dir)
        side.addWidget(data_button)
        source_button = QPushButton("下载源设置（F12）")
        source_button.clicked.connect(self.open_source_dialog)
        side.addWidget(source_button)
        side.addStretch(1)
        outer.addWidget(left)

        right = QWidget()
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, u(1), SIDE_PAD, u(2))
        right_column.setSpacing(0)

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
        source_hint = self._label("客户端只与下载源通信，游戏文件 / Fabric / Forge / Modrinth 都经它代理")
        source_hint.setWordWrap(True)
        source.add(source_hint)
        self.settings_base = QLineEdit(self.config.download_base)
        self.settings_base.setReadOnly(True)
        self.settings_base.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        source.add(self.settings_base)
        pair = QHBoxLayout()
        pair.setSpacing(8)
        self.settings_token = QLineEdit(self.config.download_token)
        self.settings_token.setPlaceholderText("访问令牌（可选）")
        self.settings_token.setReadOnly(True)
        self.settings_token.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        pair.addWidget(self.settings_token, 1)
        self.settings_proxy = QLineEdit(self.config.proxy)
        self.settings_proxy.setPlaceholderText("代理（可选，如 http://127.0.0.1:7890）")
        self.settings_proxy.setReadOnly(True)
        self.settings_proxy.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        pair.addWidget(self.settings_proxy, 1)
        source.add_layout(pair)
        workers_row = QHBoxLayout()
        workers_row.setSpacing(8)
        workers_row.addWidget(self._label("并发下载数"))
        self.settings_workers = QSpinBox()
        self.settings_workers.setRange(1, 32)
        self.settings_workers.setValue(int(self.config.max_workers))
        self.settings_workers.setEnabled(False)
        workers_row.addWidget(self.settings_workers)
        workers_row.addStretch(1)
        test = QPushButton("测试连接")
        test.clicked.connect(self._test_source)
        workers_row.addWidget(test)
        change = QPushButton("修改下载源（F12）")
        change.clicked.connect(self.open_source_dialog)
        workers_row.addWidget(change)
        source.add_layout(workers_row)
        hint = self._label("下载源已由发布者固定，按 F12 打开修改窗口", "mutedSmall")
        hint.setWordWrap(True)
        source.add(hint)
        self.settings_source_state = self._label("")
        source.add(self.settings_source_state)
        layout.addWidget(source)

        about = Card()
        about.add(self._label("关于", "h2"))
        about_text = self._label(
            f"{APP_NAME} v{__version__} · 自建镜像启动器，仅依赖 Python 标准库 + PyQt6"
        )
        about_text.setWordWrap(True)
        about.add(about_text)
        data_text = self._label(f"数据目录：{config_dir(self.data_dir)}")
        data_text.setWordWrap(True)
        about.add(data_text)
        about_row = QHBoxLayout()
        about_row.setSpacing(8)
        open_data = QPushButton("打开数据目录")
        open_data.clicked.connect(self._open_data_dir)
        about_row.addWidget(open_data)
        source_dialog = QPushButton("下载源设置（F12）")
        source_dialog.clicked.connect(self.open_source_dialog)
        about_row.addWidget(source_dialog)
        about_row.addStretch(1)
        about.add_layout(about_row)
        layout.addWidget(about)

        layout.addStretch(1)
        scroll.setWidget(holder)
        right_column.addWidget(scroll, 1)
        outer.addWidget(right, 1)
        return page

    def _build_overlays(self) -> None:
        """版本号（右下）、下载源（左下）与任务面板（浮层），都不占内容区高度。"""
        central = self.centralWidget()
        if central is None:
            return
        self.source_label = QLabel("下载源：-", central)
        self.source_label.setObjectName("mutedSmall")
        self.source_label.setToolTip("下载源已由发布者固定，按 F12 可打开修改窗口")
        self.source_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.version_label = QLabel(f"v{__version__}", central)
        self.version_label.setObjectName("versionTag")
        self.version_label.setToolTip(f"{APP_NAME} v{__version__}")
        self.version_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.task_panel = self._build_task_panel(central)
        self.task_panel.setVisible(False)

    def _place_overlays(self) -> None:
        central = self.centralWidget()
        if central is None:
            return
        width, height = central.width(), central.height()
        bottom = max(0, height - u(0.4))
        if hasattr(self, "version_label"):
            self.version_label.adjustSize()
            self.version_label.move(
                max(0, width - self.version_label.width() - u(1.25)),
                max(0, bottom - self.version_label.height()),
            )
            self.version_label.raise_()
        if hasattr(self, "source_label"):
            self.source_label.adjustSize()
            self.source_label.move(u(1.25), max(0, bottom - self.source_label.height()))
            self.source_label.raise_()
        if hasattr(self, "task_panel") and self.task_panel.isVisible():
            hint = self.task_panel.sizeHint()
            panel_w = min(max(hint.width(), u(30)), max(u(20), width - u(4)))
            panel_h = max(hint.height(), u(5))
            stack_bottom = bottom - self.version_label.height() - u(0.75)
            self.task_panel.setGeometry(
                max(0, width - panel_w - u(1.25)),
                max(0, stack_bottom - panel_h),
                panel_w,
                panel_h,
            )
            self.task_panel.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._place_overlays()

    def _build_task_panel(self, parent: QWidget | None = None) -> Card:
        panel = Card(parent, margins=(u(1.25), u(0.75), u(1.25), u(0.75)))
        row = QHBoxLayout()
        row.setSpacing(u(0.5))
        self.task_title = QLabel("")
        self.task_title.setObjectName("h3")
        row.addWidget(self.task_title)
        self.task_phase = self._label("")
        row.addWidget(self.task_phase, 1)
        self.task_percent = QLabel("0%")
        self.task_percent.setStyleSheet("font-weight: 700;")
        row.addWidget(self.task_percent)
        self.task_cancel = QPushButton("取消")
        self.task_cancel.clicked.connect(self.cancel_task)
        row.addWidget(self.task_cancel)
        panel.add_layout(row)
        self.task_bar = QProgressBar()
        self.task_bar.setRange(0, 100)
        self.task_bar.setValue(0)
        panel.add(self.task_bar)
        self.task_detail = self._label("")
        panel.add(self.task_detail)
        panel.setVisible(False)
        return panel

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
        order = [name for name, _text in DOWNLOAD_TABS]
        if key not in order:
            key = "game"
        self._current_tab = key
        self.dl_stack.setCurrentIndex(order.index(key))
        for name, button in self.tab_buttons.items():
            button.setChecked(name == key)
        if key == "game":
            self._render_versions()
            return
        if key == "java":
            self._refresh_java_tab()
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
            if hasattr(self, "source_label"):
                self.source_label.setText(f"下载源：{host}")
        else:
            self.source_badge.set("官方源", "gray")
            self.download_sub.setToolTip("未配置下载源，将直连官方地址")
            if hasattr(self, "source_label"):
                self.source_label.setText("下载源：官方")

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
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for item in self.instances:
            loader = item.loader or "原版"
            label = item.id if loader in ("原版", "未知") else f"{item.id} · {loader}"
            self.version_combo.addItem(QIcon(block_pixmap(item.id, 22, item.loader)), label, item)
            index = self.version_combo.count() - 1
            self.version_combo.setItemData(
                index, item.broken or f"{item.id} · {loader}", Qt.ItemDataRole.ToolTipRole
            )

        target = -1
        for index, item in enumerate(self.instances):
            if item.id == previous:
                target = index
                break
        if target < 0 and self.instances:
            target = 0
        if target >= 0:
            self.version_combo.setCurrentIndex(target)
        self.version_combo.blockSignals(False)

        self.selected = self.version_combo.currentData() if target >= 0 else None
        self._update_hero()

        total = sum(item.size_bytes for item in self.instances)
        if self.instances:
            self.status(f"{len(self.instances)} 个实例 · {human_size(total)}")
        self._render_versions()

    def _on_version_selected(self, index: int) -> None:
        instance = self.version_combo.itemData(index) if index >= 0 else None
        self.selected = instance
        if instance is not None:
            self.config.last_version = instance.id
        self._update_hero()

    def _update_hero(self) -> None:
        """把当前实例的信息写到启动页（提示、目录、模组 / 存档列表）。"""
        instance = self.selected
        if instance is None:
            self.home_hint.setText("还没有可用的游戏版本，去「下载」页安装一个吧")
            self.path_label.setText(str(self.game_dir()))
            self.config_hint.setText("")
            self.launch_button.setEnabled(False)
            self._refresh_home_lists()
            return
        parts = [f"MC {instance.mc_version}", human_size(instance.size_bytes)]
        loader = instance.loader or "原版"
        if loader not in ("原版", "未知"):
            parts.insert(1, loader)
        pack = read_pack_metadata(self.game_dir(), instance.id)
        if pack and pack.get("name"):
            parts.insert(0, f"整合包 {pack.get('name')}")
        if not supports_mods(instance.loader):
            parts.append("原版 · 不能装模组（点 ⋯ 可安装加载器）")
        if instance.broken:
            parts.append("版本 JSON 损坏")
        self.home_hint.setText("  ·  ".join(parts))
        self.path_label.setText(str(self.game_dir_for(instance)))
        self.config_hint.setText(f"最近使用 {human_date(instance.mtime)}")
        self.launch_button.setEnabled(True)
        self._refresh_home_lists()

    def _on_memory_changed(self, value: int) -> None:
        self.config.memory_mb = int(value)
        self.memory_value.setText(f"{value} MB")
        if hasattr(self, "settings_memory"):
            self.settings_memory.blockSignals(True)
            self.settings_memory.setValue(int(value))
            self.settings_memory.blockSignals(False)
            self.settings_memory_value.setText(f"{value} MB")

    def _instance_menu(self, global_pos) -> None:
        """版本下拉框右侧的「⋯」菜单：版本设置 / 加载器 / 目录 / 删除。"""
        instance = self.selected
        menu = QMenu(self)
        settings = menu.addAction("版本设置")
        add_loader = menu.addAction("安装加载器…")
        menu.addSeparator()
        open_game = menu.addAction("打开游戏目录")
        open_version = menu.addAction("打开版本目录")
        menu.addSeparator()
        refresh = menu.addAction("刷新版本列表")
        remove = menu.addAction("删除实例")
        for action in (settings, add_loader, open_game, open_version, remove):
            action.setEnabled(instance is not None)
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is refresh:
            self.refresh_instances()
        elif chosen is remove and instance is not None:
            self._delete_instance(instance)
        elif chosen is settings:
            self._open_instance_dialog()
        elif chosen is add_loader and instance is not None:
            self._install_loader_on(instance)
        elif chosen is open_game:
            _reveal(self.game_dir_for(instance))
        elif chosen is open_version and instance is not None:
            _reveal(self.game_dir() / "versions" / instance.id)

    def _delete_instance(self, instance: Instance) -> None:
        answer = QMessageBox.question(
            self, APP_NAME, f"确定删除实例 {instance.id}？此操作不可恢复。"
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        shutil.rmtree(self.game_dir() / "versions" / instance.id, ignore_errors=True)
        self.notice(f"已删除实例 {instance.id}", "WARN")
        self.refresh_instances()

    def _apply_account_name(self) -> None:
        """名称框回车：校验后保存并刷新头像。"""
        name = self.account_name.text().strip()
        if name == self.config.username:
            self.account_name.setText(self.config.username)
            return
        try:
            validate_name(name)
        except ValueError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            self.account_name.setText(self.config.username)
            return
        self.config.username = name
        save_config(self.config, self.data_dir)
        self.avatar.setPixmap(initial_pixmap(name, u(15), "account"))
        self.notice(f"已切换离线账户：{name}")

    def _edit_account(self) -> None:
        """兼容旧入口：直接聚焦名称框改名。"""
        self.account_name.setFocus()
        self.account_name.selectAll()

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
        if self.cancel_event is not None and not self.cancel_event.is_set():
            self.notice("已有任务在进行，请等待完成或取消", "WARN")
            return

        dialog = LoaderPickDialog(self, summary.id, title=f"安装 {summary.id}")
        if not dialog.exec():
            return
        loader_key, loader_version = dialog.choice()
        loader_label = dict(LOADERS).get(loader_key, loader_key)
        title = f"安装 {summary.id}"
        if loader_key != "vanilla":
            title = f"安装 {summary.id} + {loader_label}"

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
            files = len(result.report.succeeded)
            version_id = summary.id
            if loader_key != "vanilla":
                self.set_phase(f"安装 {loader_label} {loader_version}")
                version_id = install_loader(
                    self.game_dir(),
                    summary.id,
                    loader_key,
                    loader_version,
                    mirror=mirror,
                    downloader=downloader,
                    manifest=manifest,
                    java=self.config.java_overrides.get("__global__"),
                    cancel=cancel,
                    phase=self.set_phase,
                )
            self.config.last_version = version_id
            save_config(self.config, self.data_dir)
            if version_id != summary.id:
                return f"{version_id} 安装完成（{files} 个文件 + {loader_label} {loader_version}）"
            return f"{summary.id} 安装完成，共 {files} 个文件"

        self.run_task(title, worker)

    def _install_loader_on(self, instance: Instance) -> None:
        """给已有实例补装加载器，装上后就能装模组与光影。"""
        if self.process is not None and self.process.running:
            self.notice("游戏正在运行，请先关闭游戏", "WARN")
            return
        dialog = LoaderPickDialog(
            self,
            instance.mc_version,
            allow_vanilla=False,
            title=f"给 {instance.id} 添加加载器",
        )
        if not dialog.exec():
            return
        key, version = dialog.choice()
        if key == "vanilla" or not version:
            return
        label = dict(LOADERS).get(key, key)

        def worker(cancel: threading.Event) -> str:
            downloader = self.make_downloader()
            mirror = Mirror(self.config.download_base or "https://mc.yjlt.top")
            manifest = self.ensure_manifest(cancel)
            self.set_phase(f"安装 {label} {version}")
            version_id = install_loader(
                self.game_dir(),
                instance.mc_version,
                key,
                version,
                mirror=mirror,
                downloader=downloader,
                manifest=manifest,
                java=self.config.java_overrides.get("__global__"),
                cancel=cancel,
                phase=self.set_phase,
            )
            self.config.last_version = version_id
            save_config(self.config, self.data_dir)
            return f"{version_id} 已就绪，可以安装模组了"

        self.run_task(f"安装 {label} {version}", worker)

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
        self._place_overlays()
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

    def open_source_dialog(self) -> None:
        dialog = SourceDialog(self, self.config)
        if not dialog.exec():
            return
        dialog.apply()
        save_config(self.config, self.data_dir)
        self._refresh_source_badge()
        self._load_settings_values()
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
        if hasattr(self, "home_jvm"):
            self.home_jvm.setText(" ".join(self.config.extra_jvm))
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
        save_config(self.config, self.data_dir)

        self.home_memory.blockSignals(True)
        self.home_memory.setValue(int(self.config.memory_mb))
        self.home_memory.blockSignals(False)
        self.memory_value.setText(f"{self.config.memory_mb} MB")
        self.home_jvm.setText(" ".join(self.config.extra_jvm))
        self._refresh_java_combo()

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
        if not hasattr(self, "settings_java") and not hasattr(self, "home_java"):
            return
        candidates = list_java(self.game_dir())
        override = self.config.java_overrides.get("__global__", "")
        for combo in (getattr(self, "home_java", None), getattr(self, "settings_java", None)):
            if combo is None:
                continue
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("自动选择（按版本要求）", "")
            for candidate in candidates:
                combo.addItem(
                    f"Java {candidate.major} · {candidate.source}", str(candidate.path)
                )
            index = combo.findData(override)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        if not candidates and hasattr(self, "settings_source_state"):
            self.settings_source_state.setText("未检测到 Java，可点「添加 Java」手动指定")

    def _on_java_changed(self, index: int) -> None:
        if index < 0:
            return
        self._set_java_override(self.settings_java.itemData(index) or "")

    def _on_home_java_changed(self, index: int) -> None:
        if index < 0:
            return
        self._set_java_override(self.home_java.itemData(index) or "")

    def _set_java_override(self, path: str) -> None:
        """两个 Java 下拉框（启动页 / 设置页）共用一份全局选择。"""
        if path:
            self.config.java_overrides["__global__"] = str(path)
        else:
            self.config.java_overrides.pop("__global__", None)
        save_config(self.config, self.data_dir)
        for combo in (getattr(self, "home_java", None), getattr(self, "settings_java", None)):
            if combo is None:
                continue
            index = combo.findData(path)
            if index >= 0 and combo.currentIndex() != index:
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

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
