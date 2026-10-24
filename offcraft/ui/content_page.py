"""内容下载页：模组 / 整合包 / 资源包 / 光影（数据来自 Modrinth）。"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from offcraft.content.modrinth import ContentError, ProjectHit, ProjectVersion
from offcraft.content.target import (
    KIND_NAMES,
    Target,
    target_from_folder,
    target_from_instance,
    target_state,
)
from offcraft.instances import read_pack_metadata
from offcraft.ui.theme import MUTED, human_count, initial_pixmap
from offcraft.ui.widgets import Badge, Card, ContentRow, ElidedLabel

__all__ = ["ContentPage", "KINDS"]

KINDS = {
    "mod": ("模组", "为游戏添加内容，需选择 Fabric / Forge / NeoForge 等加载器。"),
    "modpack": ("整合包", "一键安装带加载器与配置的完整实例（.mrpack）。"),
    "resourcepack": ("资源包", "改变贴图、音效与界面风格。"),
    "shader": ("光影", "需要 Iris / OptiFine 等光影前置支持。"),
}

SORTS = [
    ("相关度", "relevance"),
    ("下载量", "downloads"),
    ("最近更新", "updated"),
    ("最新发布", "newest"),
]

# 下拉框末尾的哨兵项：选中它等于「打开资源管理器挑一个文件夹」
PICK_FOLDER = "__pick_folder__"

LOADER_CHOICES = [
    ("全部加载器", ""),
    ("Fabric", "fabric"),
    ("Forge", "forge"),
    ("NeoForge", "neoforge"),
    ("Quilt", "quilt"),
]


class ContentPage(QWidget):
    def __init__(self, window: Any, kind: str) -> None:
        super().__init__()
        self.window = window
        self.kind = kind
        self.hits: list[ProjectHit] = []
        self.rows: list[ContentRow] = []
        self.total = 0
        self.offset = 0
        self.selected: ProjectHit | None = None
        self.versions: list[ProjectVersion] = []
        #: 手动挑选的文件夹目标（与实例目标一起组成下拉列表）
        self.extra_targets: list[Target] = []
        # 上一次有效的安装目标（取消选择文件夹时回退到这里）
        self._last_target_key = ""
        self._token = 0
        self._loaded = False
        self._build()

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        title, subtitle = KINDS[self.kind]
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QHBoxLayout()
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(self._label(title, "h2"))
        head.addWidget(self._label(subtitle, "muted"))
        header.addLayout(head, 1)
        self.mode_badge = Badge("Modrinth", "purple")
        header.addWidget(self.mode_badge)
        root.addLayout(header)

        filters = Card(margins=(14, 10, 14, 10))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText(f"搜索{title}……（回车搜索）")
        self.search.returnPressed.connect(self.reload)
        row.addWidget(self.search, 1)

        self.game_combo = QComboBox()
        self.game_combo.setMinimumWidth(128)
        self.game_combo.currentIndexChanged.connect(self.reload)
        row.addWidget(self.game_combo)

        self.loader_combo = QComboBox()
        self.loader_combo.setMinimumWidth(126)
        for label, value in LOADER_CHOICES:
            self.loader_combo.addItem(label, value)
        self.loader_combo.currentIndexChanged.connect(self.reload)
        self.loader_combo.setVisible(self.kind in ("mod", "modpack"))
        row.addWidget(self.loader_combo)

        self.sort_combo = QComboBox()
        for label, value in SORTS:
            self.sort_combo.addItem(label, value)
        self.sort_combo.currentIndexChanged.connect(self.reload)
        row.addWidget(self.sort_combo)

        button = QPushButton("搜索")
        button.setObjectName("accent")
        button.clicked.connect(self.reload)
        row.addWidget(button)
        filters.add_layout(row)
        root.addWidget(filters)

        body = QHBoxLayout()
        body.setSpacing(12)

        results = Card(margins=(8, 8, 8, 8))
        self.list = QListWidget()
        self.list.setSpacing(2)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.itemClicked.connect(self._on_select)
        results.add(self.list, 1)
        footer = QHBoxLayout()
        self.count_label = self._label("", "muted")
        footer.addWidget(self.count_label)
        footer.addStretch(1)
        self.more_button = QPushButton("加载更多")
        self.more_button.clicked.connect(lambda: self.load_more())
        self.more_button.setVisible(False)
        footer.addWidget(self.more_button)
        results.add_layout(footer)
        body.addWidget(results, 1)

        detail = Card(margins=(16, 16, 16, 16))
        detail.setFixedWidth(292)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.detail_icon = QLabel()
        self.detail_icon.setFixedSize(60, 60)
        self.detail_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.detail_icon)
        names = QVBoxLayout()
        names.setSpacing(2)
        self.detail_title = ElidedLabel("选择左侧项目")
        self.detail_title.setStyleSheet("font-size: 12pt; font-weight: 700;")
        names.addWidget(self.detail_title)
        self.detail_author = self._label("", "muted")
        names.addWidget(self.detail_author)
        top.addLayout(names, 1)
        detail.add_layout(top)

        self.detail_badges = QHBoxLayout()
        self.detail_badges.setSpacing(6)
        detail.add_layout(self.detail_badges)
        self.detail_desc = QLabel("")
        self.detail_desc.setWordWrap(True)
        self.detail_desc.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        self.detail_desc.setMaximumHeight(96)
        detail.add(self.detail_desc)
        self.detail_stats = self._label("", "muted")
        detail.add(self.detail_stats)
        detail.body.addStretch(1)

        detail.add(self._label("版本", "muted"))
        self.version_combo = QComboBox()
        self.version_combo.currentIndexChanged.connect(self._on_version_changed)
        detail.add(self.version_combo)

        if self.kind == "modpack":
            detail.add(self._label("实例名称", "muted"))
            self.name_edit = QLineEdit()
            self.name_edit.setPlaceholderText("新实例名称")
            detail.add(self.name_edit)
        else:
            detail.add(self._label("安装到", "muted"))
            self.target_combo = QComboBox()
            self.target_combo.setToolTip(
                "选择已装版本（自动换算成它的游戏目录），"
                "或选「选择文件夹…」自己挑一个游戏文件夹"
            )
            self.target_combo.currentIndexChanged.connect(self._on_target_changed)
            detail.add(self.target_combo)
            self.target_path = self._label("", "mutedSmall")
            self.target_path.setWordWrap(True)
            detail.add(self.target_path)

        self.install_button = QPushButton("安装所选版本")
        self.install_button.setObjectName("accent")
        self.install_button.setFixedHeight(40)
        self.install_button.clicked.connect(self.install_selected)
        self.install_button.setEnabled(False)
        detail.add(self.install_button)

        self.open_button = QPushButton("在 Modrinth 查看")
        self.open_button.clicked.connect(self._open_page)
        self.open_button.setEnabled(False)
        detail.add(self.open_button)
        self.detail_hint = self._label("", "mutedSmall")
        self.detail_hint.setWordWrap(True)
        detail.add(self.detail_hint)

        body.addWidget(detail)
        root.addLayout(body, 1)

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        return label

    # ----------------------------------------------------------------- data
    def refresh(self) -> None:
        self._loaded = True
        self._refresh_filters()
        self.reload()

    def ensure_loaded(self) -> None:
        """切到本页时调用：首次进入就直接拉列表 + 首个项目的版本。"""
        if not self._loaded:
            self.refresh()
            return
        self.sync_filters()
        if self.selected is None and self.hits:
            self.list.setCurrentRow(0)
            item = self.list.item(0)
            if item is not None:
                self._on_select(item)
            return
        if self.selected is not None and not self.versions:
            self._load_versions(self.selected)

    def sync_filters(self) -> None:
        """只刷新筛选框（不触发新的网络请求）。"""
        self._refresh_filters()

    def _mc_dir(self) -> Path:
        return self.window.game_dir()

    def _refresh_filters(self, select_key: str = "") -> None:
        current_game = self.game_combo.currentData()
        current_loader = self.loader_combo.currentData()
        instances = list(self.window.instances)
        selected = self.window.selected_instance()

        self.game_combo.blockSignals(True)
        self.game_combo.clear()
        self.game_combo.addItem("全部版本", "")
        seen: set[str] = set()
        ordered: list[str] = []
        if selected is not None:
            ordered.append(selected.mc_version)
        for item in instances:
            if item.mc_version and item.mc_version not in ordered:
                ordered.append(item.mc_version)
        for version in ordered[:20]:
            if version and version not in seen:
                seen.add(version)
                self.game_combo.addItem(f"MC {version}", version)
        index = self.game_combo.findData(current_game)
        self.game_combo.setCurrentIndex(index if index >= 0 else (1 if selected is not None else 0))
        self.game_combo.blockSignals(False)

        self.loader_combo.blockSignals(True)
        index = self.loader_combo.findData(current_loader)
        if index < 0 and selected is not None:
            loader = selected.loader.lower()
            index = self.loader_combo.findData(loader)
        self.loader_combo.setCurrentIndex(index if index >= 0 else 0)
        self.loader_combo.blockSignals(False)

        if self.kind != "modpack":
            self._rebuild_targets(select_key)

        try:
            self.mode_badge.setText(f"Modrinth · {self.window.content_client().api_mode}")
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- target
    def target_options(self) -> list[Target]:
        """已装版本 + 手动选的文件夹，按路径去重。"""
        mc_dir = self._mc_dir()
        options: list[Target] = []
        for item in list(self.window.instances):
            isolated = bool(getattr(self.window.config, "isolation", False)) or (
                read_pack_metadata(mc_dir, item.id) is not None
            )
            options.append(target_from_instance(mc_dir, item, isolated=isolated))
        seen = {option.key for option in options}
        for extra in self.extra_targets:
            if extra.key not in seen:
                seen.add(extra.key)
                options.append(extra)
        return options

    @staticmethod
    def _target_text(target: Target) -> str:
        if target.source == "instance":
            return f"{target.instance_id} · {target.loader or '原版'}"
        name = Path(target.path).name or str(target.path)
        loader = target.loader or "未知版本"
        return f"{name} · 文件夹（{loader}）"

    def _rebuild_targets(self, select_key: str = "") -> None:
        options = self.target_options()
        current = self.target_combo.currentData()
        keep = select_key or (current.key if isinstance(current, Target) else "")
        if not keep:
            keep = self._last_target_key
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for target in options:
            self.target_combo.addItem(self._target_text(target), target)
        index = -1
        if keep:
            for position, target in enumerate(options):
                if target.key == keep:
                    index = position
                    break
        if index < 0:
            selected = self.window.selected_instance()
            if selected is not None:
                for position, target in enumerate(options):
                    if target.instance_id == selected.id:
                        index = position
                        break
        if index < 0 and options:
            index = 0
        # 末尾固定放一个「选择文件夹…」，选中它就会打开资源管理器
        self.target_combo.addItem("选择文件夹…", PICK_FOLDER)
        self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)
        target = self.current_target()
        self._last_target_key = target.key if target is not None else ""
        self._refresh_install_state()

    def current_target(self) -> Target | None:
        target = self.target_combo.currentData()
        return target if isinstance(target, Target) else None

    def _last_target(self) -> Target | None:
        key = self._last_target_key
        if not key:
            return None
        for item in self.target_options():
            if item.key == key:
                return item
        return None

    def _restore_target(self) -> None:
        """取消文件选择时回退到上一次有效的安装目标。"""
        self.target_combo.blockSignals(True)
        index = -1
        key = self._last_target_key
        if key:
            for position in range(self.target_combo.count()):
                data = self.target_combo.itemData(position)
                if isinstance(data, Target) and data.key == key:
                    index = position
                    break
        if index < 0 and self.target_combo.count() > 1:
            index = 0
        self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)
        self._refresh_install_state()

    def pick_folder(self) -> None:
        """手动挑一个游戏文件夹作为安装目标；直接退出则回退到上一次的选择。"""
        start = str(self._mc_dir())
        target = self.current_target() or self._last_target()
        if target is not None and Path(target.path).is_dir():
            start = str(target.path)
        folder = QFileDialog.getExistingDirectory(
            self, f"选择要安装{KIND_NAMES.get(self.kind, '内容')}的游戏文件夹", start
        )
        if not folder:
            self._restore_target()
            return
        picked = target_from_folder(folder)
        if not any(item.key == picked.key for item in self.extra_targets):
            self.extra_targets.append(picked)
        self._rebuild_targets(select_key=picked.key)
        self._match_filters(picked)
        ok, reason = target_state(picked, self.kind)
        if not ok:
            self.window.notice(reason, "WARN")
        elif picked.mc_version:
            self.window.notice(
                f"安装目标：{picked.mc_version} · {picked.loader or '未知加载器'}，列表已按它筛选",
                "INFO",
            )

    def _match_filters(self, target: Target) -> None:
        """把筛选框对齐到目标，避免装错游戏版本 / 加载器的内容。"""
        changed = False
        index = self.game_combo.findData(target.mc_version) if target.mc_version else -1
        if index >= 0 and self.game_combo.currentIndex() != index:
            self.game_combo.setCurrentIndex(index)  # 信号会触发 reload
            changed = True
        if self.kind in ("mod", "modpack") and self.loader_combo.isVisible():
            loader = (target.loader or "").lower()
            lindex = self.loader_combo.findData(loader) if loader else -1
            if lindex >= 0 and self.loader_combo.currentIndex() != lindex:
                self.loader_combo.setCurrentIndex(lindex)
                changed = True
        if not changed:
            self.reload()

    def _filters(self) -> tuple[str, str, str, str]:
        return (
            self.search.text().strip(),
            str(self.game_combo.currentData() or ""),
            str(self.loader_combo.currentData() or ""),
            str(self.sort_combo.currentData() or "relevance"),
        )

    def reload(self) -> None:
        self.offset = 0
        self.hits = []
        self.list.clear()
        self.rows = []
        self.selected = None
        self.more_button.setVisible(False)
        self.count_label.setText("正在搜索……")
        self._search(append=False)

    def load_more(self) -> None:
        if self.offset >= self.total:
            return
        self._search(append=True)

    def _search(self, *, append: bool) -> None:
        query, game_version, loader, index = self._filters()
        token = self._token = self._token + 1
        self.more_button.setEnabled(False)

        def worker() -> None:
            try:
                result = self.window.content_client().search(
                    query,
                    self.kind,
                    game_version=game_version,
                    loader=loader,
                    limit=20,
                    offset=self.offset,
                    index=index,
                )
            except ContentError as exc:
                self.window.post(lambda: self._search_failed(token, str(exc)))
                return
            except Exception as exc:  # noqa: BLE001
                self.window.post(lambda: self._search_failed(token, f"搜索失败：{exc}"))
                return
            self.window.post(lambda: self._apply(token, result, append))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, token: int, result, append: bool) -> None:
        if token != self._token:
            return
        if not append:
            self.list.clear()
            self.rows = []
            self.hits = []
        self.total = result.total
        self.offset = result.offset + len(result.hits)
        for hit in result.hits:
            item = QListWidgetItem()
            row = ContentRow(hit)
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            self.hits.append(hit)
            self.rows.append(row)
            if hit.icon_url:
                self.window.request_icon(self.window.content_client().resolve_url(hit.icon_url), row.set_icon)
        self.count_label.setText(f"共 {human_count(self.total)} 个结果，已加载 {len(self.hits)}")
        self.more_button.setVisible(self.offset < self.total)
        self.more_button.setEnabled(True)
        self.mode_badge.setText(f"Modrinth · {self.window.content_client().api_mode}")
        if self.list.count() and self.selected is None:
            self.list.setCurrentRow(0)
            self._on_select(self.list.item(0))

    def _search_failed(self, token: int, message: str) -> None:
        if token != self._token:
            return
        self.count_label.setText(message)
        self.count_label.setObjectName("error")
        self.count_label.setStyleSheet(f"color: #C2453F; font-size: 9pt;")
        self.more_button.setEnabled(True)
        self.window.notice(message, "ERROR")

    # ----------------------------------------------------------------- detail
    def _on_select(self, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        if not 0 <= row < len(self.hits):
            return
        hit = self.hits[row]
        self.selected = hit
        self.detail_title.setText(hit.title)
        self.detail_title.setToolTip(hit.title)
        self.detail_author.setText(hit.author or "Modrinth")
        self.detail_desc.setText(hit.description)
        self.detail_icon.setPixmap(initial_pixmap(hit.title, 60, hit.slug))
        self.detail_stats.setText(
            f"下载 {human_count(hit.downloads)}  ·  关注 {human_count(hit.follows)}"
            + (f"  ·  {hit.date_modified[:10]}" if hit.date_modified else "")
        )
        while self.detail_badges.count():
            widget = self.detail_badges.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        for category in hit.categories[:3]:
            self.detail_badges.addWidget(Badge(category, "gray"))
        self.detail_badges.addStretch(1)
        self.open_button.setEnabled(True)
        if self.kind == "modpack":
            self.name_edit.setText(hit.title)
        if hit.icon_url:
            self.window.request_icon(
                self.window.content_client().resolve_url(hit.icon_url), self.detail_icon.setPixmap, 60
            )
        self._load_versions(hit)

    def _load_versions(self, hit: ProjectHit) -> None:
        """拉取当前选中项目的兼容版本（自动带上筛选框里的游戏版本/加载器）。"""
        self.versions = []
        self.version_combo.clear()
        self.version_combo.addItem("正在读取版本……", None)
        self.install_button.setEnabled(False)
        game_version, loader = "", ""
        if self.game_combo.currentData():
            game_version = str(self.game_combo.currentData())
        if self.loader_combo.isVisible() and self.loader_combo.currentData():
            loader = str(self.loader_combo.currentData())

        def worker() -> None:
            try:
                versions = self.window.content_client().versions(
                    hit.id, game_version=game_version, loader=loader
                )
            except Exception as exc:  # noqa: BLE001
                self.window.post(lambda: self._versions_failed(hit.id, str(exc)))
                return
            self.window.post(lambda: self._show_versions(hit, versions))

        threading.Thread(target=worker, daemon=True).start()

    def _versions_failed(self, project_id: str, message: str) -> None:
        if self.selected is None or self.selected.id != project_id:
            return
        self.version_combo.clear()
        self.version_combo.addItem("版本读取失败", None)
        self.detail_hint.setText(message)

    def _show_versions(self, hit: ProjectHit, versions) -> None:
        if self.selected is None or self.selected.id != hit.id:
            return
        self.versions = list(versions)
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for version in self.versions[:80]:
            suffix = " · 测试版" if version.version_type != "release" else ""
            self.version_combo.addItem(f"{version.label}{suffix}", version)
        self.version_combo.blockSignals(False)
        if not self.versions:
            self.version_combo.addItem("没有兼容版本", None)
            self.detail_hint.setText("试试切换上方的游戏版本或加载器筛选。")
        else:
            self.detail_hint.setText(f"共 {len(self.versions)} 个兼容版本" if len(self.versions) > 1 else "")
        self._on_version_changed(self.version_combo.currentIndex())

    def _on_version_changed(self, index: int) -> None:
        if index < 0:
            return
        version = self.version_combo.itemData(index)
        if version is not None and self.kind == "modpack":
            self.name_edit.setText(f"{self.selected.title} {version.label}" if self.selected else "")
        self._refresh_install_state()

    def _on_target_changed(self, _index: int) -> None:
        if self.target_combo.currentData() == PICK_FOLDER:
            # 选中「选择文件夹…」= 立即弹资源管理器；取消则回退
            self.pick_folder()
            return
        target = self.current_target()
        self._last_target_key = target.key if target is not None else self._last_target_key
        self._refresh_install_state()

    def _dest_text(self, dest: Path) -> str:
        """安装目标路径的短描述：游戏目录下只显示相对部分。"""
        try:
            return f"游戏目录\\{Path(dest).relative_to(self._mc_dir())}"
        except ValueError:
            return "…\\" + "\\".join(Path(dest).parts[-3:])

    def _refresh_install_state(self) -> None:
        """安装按钮：要有版本，且目标目录能装这种内容。"""
        if not hasattr(self, "install_button"):
            return
        version = self._version()
        if self.kind == "modpack":
            self.install_button.setEnabled(version is not None)
            return
        target = self.current_target()
        if target is None:
            self.install_button.setEnabled(False)
            self.target_path.setText("还没有可选的游戏目录：先下载一个版本，或在上面选「选择文件夹…」")
            self.detail_hint.setText("先安装一个游戏版本，才能安装内容。")
            return
        dest = target.subdir(self.kind)
        self.target_path.setText(self._dest_text(dest))
        ok, reason = target_state(target, self.kind)
        self.target_path.setToolTip(f"{dest}\n{reason}" if reason else str(dest))
        if not ok:
            self.install_button.setEnabled(False)
            self.detail_hint.setText(reason)
            return
        if version is None:
            self.install_button.setEnabled(False)
            return
        self.install_button.setEnabled(True)
        if reason:
            self.detail_hint.setText(reason)

    def _open_page(self) -> None:
        if self.selected is not None:
            QDesktopServices.openUrl(QUrl(self.selected.page_url))

    # ----------------------------------------------------------------- install
    def _version(self) -> ProjectVersion | None:
        return self.version_combo.currentData()

    def install_selected(self) -> None:
        hit = self.selected
        version = self._version()
        if hit is None or version is None:
            return
        if self.kind == "modpack":
            self.window.install_modpack(hit, version, self.name_edit.text().strip())
            return
        target = self.current_target()
        if target is None:
            self.window.notice("请先选择安装位置：已装版本或任意游戏文件夹", "WARN")
            return
        ok, reason = target_state(target, self.kind)
        if not ok:
            self.window.notice(reason, "WARN")
            return
        self.window.install_content(hit, version, target, self.kind)
