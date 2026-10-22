"""主窗口：启动 / 下载 / 设置 三个页面。

布局参照手绘稿：顶部导航 + 左侧分类栏 + 右侧列表/表单 + 底部日志。
"""

from __future__ import annotations

import os
import json
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from offcraft import APP_NAME, __version__
from offcraft.config import Config, config_dir, save_config
from offcraft.instances import Instance, discover_instances
from offcraft.launch.auth import make_profile
from offcraft.launch.command import LaunchError, LaunchOptions, build_command
from offcraft.launch.java import list_java
from offcraft.launch.process import GameProcess
from offcraft.meta.manifest import VersionManifest, VersionSummary
from offcraft.meta.version import VersionError, load_version
from offcraft.mirror import Mirror
from offcraft.net.downloader import Downloader, ProgressTracker
from offcraft.ui import theme
from offcraft.ui.widgets import LogView, format_bytes, format_speed

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

LAUNCH_CATEGORIES = ["原版游戏", "Mod", "整合包", "光影", "Java"]
DOWNLOAD_CATEGORIES = ["市场", "官方", "社区", "其他", "已下载"]


class LauncherApp:
    def __init__(self, root: tk.Tk, config: Config, data_dir: str | Path | None = None) -> None:
        self.root = root
        self.config = config
        self.data_dir = data_dir
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.manifest: VersionManifest | None = None
        self.remote: list[VersionSummary] = []
        self.instances: list[Instance] = []
        self.process: GameProcess | None = None
        self.cancel_event: threading.Event | None = None
        self.remote_by_iid: dict[str, VersionSummary] = {}
        self.instance_by_iid: dict[str, Instance] = {}
        self.launch_category = "原版游戏"
        self.download_category = "官方"
        self.nav_buttons: dict[str, tk.Button] = {}

        self._build()
        self.refresh_instances()
        self._load_cached_manifest()
        if self.config.download_base:
            self.refresh_remote()
        else:
            self.log("尚未配置下载源，可在“设置”中填写镜像地址（实例仍可启动）", "WARN")
        initial = os.environ.get("OFFCRAFT_TAB", "launch")
        self.show_page(initial if initial in self.pages else "launch")
        self.root.after(120, self._drain)

    # ------------------------------------------------------------- 基础布局
    def _build(self) -> None:
        self.root.title(f"{APP_NAME} · CrMcDownload")
        width = min(int(self.config.window.get("w", 1180)), 1000)
        height = min(int(self.config.window.get("h", 760)), 560)
        self.root.geometry(f"{width}x{height}+20+20")
        self.root.minsize(900, 520)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.status = tk.StringVar(value="就绪")

        header = tk.Frame(self.root, bg=theme.COLORS["panel"], height=60)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header, text=APP_NAME, bg=theme.COLORS["panel"], fg=theme.COLORS["accent"],
            font=theme.FONTS["title"],
        ).pack(side="left", padx=(18, 10), pady=8)

        for key, text in (("launch", "▶ 启动"), ("download", "⤓ 下载"), ("settings", "⚙ 设置")):
            button = tk.Button(
                header, text=text, bg=theme.COLORS["panel"], fg=theme.COLORS["muted"],
                activebackground=theme.COLORS["panel"], activeforeground=theme.COLORS["text"],
                relief="flat", bd=0, font=theme.FONTS["bold"], padx=14, pady=8,
                cursor="hand2", command=lambda name=key: self.show_page(name),
            )
            button.pack(side="left", padx=2, pady=12)
            self.nav_buttons[key] = button

        self.source_label = tk.Label(
            header, text=f"源：{self.config.download_base or '未配置'}", bg=theme.COLORS["panel"],
            fg=theme.COLORS["muted"], font=theme.FONTS["small"],
        )
        self.source_label.pack(side="right", padx=18)

        self.container = tk.Frame(self.root, bg=theme.COLORS["bg"])
        self.container.pack(fill="both", expand=True)
        self.pages = {
            "launch": self._build_launch_page(),
            "download": self._build_download_page(),
            "settings": self._build_settings_page(),
        }

        log_frame = tk.Frame(self.root, bg=theme.COLORS["bg"], height=112)
        log_frame.pack(fill="x", padx=12, pady=(0, 4))
        log_frame.pack_propagate(False)
        bar = tk.Frame(log_frame, bg=theme.COLORS["bg"])
        bar.pack(fill="x")
        tk.Label(bar, text="日志", bg=theme.COLORS["bg"], fg=theme.COLORS["muted"],
                 font=theme.FONTS["small"]).pack(side="left")
        tk.Button(bar, text="清空", bg=theme.COLORS["bg"], fg=theme.COLORS["muted"], bd=0,
                  font=theme.FONTS["small"], command=lambda: self.log_view.clear()).pack(side="right")
        self.log_view = LogView(log_frame)
        self.log_view.pack(fill="both", expand=True)
        self.log_view.text.configure(height=4)

        tk.Label(
            self.root, textvariable=self.status, bg=theme.COLORS["panel"],
            fg=theme.COLORS["muted"], anchor="w", font=theme.FONTS["small"], padx=14, pady=3,
        ).pack(fill="x", side="bottom")

    def show_page(self, name: str) -> None:
        if name not in self.pages:
            return
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True)
        for key, button in self.nav_buttons.items():
            active = key == name
            button.configure(
                fg=theme.COLORS["accent"] if active else theme.COLORS["muted"],
                bg=theme.COLORS["card"] if active else theme.COLORS["panel"],
            )
        if name == "download" and self.manifest is None and self.config.download_base:
            self.refresh_remote()
        if name == "launch":
            self.refresh_instances()

    # --------------------------------------------------------------- 启动页
    def _build_launch_page(self) -> tk.Frame:
        page = tk.Frame(self.container, bg=theme.COLORS["bg"])
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)

        side = tk.Frame(page, bg=theme.COLORS["panel"], width=190)
        side.grid(row=0, column=0, sticky="ns", padx=(10, 0), pady=10)
        side.grid_propagate(False)
        tk.Label(side, text="游戏", bg=theme.COLORS["panel"], fg=theme.COLORS["muted"],
                 font=theme.FONTS["small"], anchor="w").pack(fill="x", padx=14, pady=(14, 6))
        self.launch_cat_buttons: dict[str, tk.Button] = {}
        for category in LAUNCH_CATEGORIES:
            button = tk.Button(
                side, text=category, anchor="w", relief="flat", bd=0, padx=16, pady=9,
                bg=theme.COLORS["panel"], fg=theme.COLORS["text"], activebackground=theme.COLORS["selection"],
                font=theme.FONTS["base"], cursor="hand2",
                command=lambda name=category: self._select_launch_category(name),
            )
            button.pack(fill="x", padx=8, pady=1)
            self.launch_cat_buttons[category] = button
        tk.Frame(side, bg=theme.COLORS["border"], height=1).pack(fill="x", padx=14, pady=10)
        tk.Button(side, text="打开游戏目录", bg=theme.COLORS["panel"], fg=theme.COLORS["muted"],
                  bd=0, font=theme.FONTS["small"], command=self.open_game_dir).pack(fill="x", padx=8, pady=4)
        tk.Button(side, text="刷新实例", bg=theme.COLORS["panel"], fg=theme.COLORS["muted"],
                  bd=0, font=theme.FONTS["small"], command=self.refresh_instances).pack(fill="x", padx=8, pady=4)

        right = tk.Frame(page, bg=theme.COLORS["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        top = tk.Frame(right, bg=theme.COLORS["bg"])
        top.grid(row=0, column=0, sticky="we", pady=(0, 8))
        self.launch_search_var = tk.StringVar()
        tk.Entry(top, textvariable=self.launch_search_var, bg=theme.COLORS["card"],
                 fg=theme.COLORS["text"], insertbackground=theme.COLORS["text"], relief="flat",
                 font=theme.FONTS["base"]).pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        self.launch_search_var.trace_add("write", lambda *_: self._render_launch_list())
        tk.Button(top, text="搜索", bg=theme.COLORS["card"], fg=theme.COLORS["text"], bd=0,
                  font=theme.FONTS["base"], padx=16, command=self._render_launch_list).pack(side="left")

        self.launch_tree = ttk.Treeview(right, columns=("name", "loader", "java", "size"),
                                        show="headings", selectmode="browse")
        for column, title in (("name", "实例"), ("loader", "加载器"), ("java", "Java"), ("size", "大小")):
            self.launch_tree.heading(column, text=title)
            self.launch_tree.column(column, anchor="w", width=180 if column == "name" else 110)
        self.launch_tree.grid(row=1, column=0, sticky="nsew")
        self.launch_tree.bind("<Double-1>", lambda _e: self.launch_selected())

        bottom = tk.Frame(right, bg=theme.COLORS["bg"])
        bottom.grid(row=2, column=0, sticky="we", pady=(10, 0))
        tk.Label(bottom, text="玩家名", bg=theme.COLORS["bg"], fg=theme.COLORS["muted"],
                 font=theme.FONTS["small"]).pack(side="left")
        self.username_var = tk.StringVar(value=self.config.username)
        tk.Entry(bottom, textvariable=self.username_var, width=16, bg=theme.COLORS["card"],
                 fg=theme.COLORS["text"], insertbackground=theme.COLORS["text"],
                 relief="flat").pack(side="left", padx=6, ipady=4)
        tk.Label(bottom, text="内存 MB", bg=theme.COLORS["bg"], fg=theme.COLORS["muted"],
                 font=theme.FONTS["small"]).pack(side="left", padx=(14, 0))
        self.memory_var = tk.StringVar(value=str(self.config.memory_mb))
        tk.Spinbox(bottom, from_=1024, to=32768, increment=512, width=7, textvariable=self.memory_var,
                   bg=theme.COLORS["card"], fg=theme.COLORS["text"], relief="flat",
                   buttonbackground=theme.COLORS["card"]).pack(side="left", padx=6)
        self.stop_btn = tk.Button(bottom, text="结束游戏", bg=theme.COLORS["card"],
                                  fg=theme.COLORS["text"], bd=0, padx=14, pady=7,
                                  command=self.stop_game, state="disabled")
        self.stop_btn.pack(side="right")
        self.launch_btn = tk.Button(bottom, text="▶ 启动游戏", bg=theme.COLORS["accent"],
                                    fg="#ffffff", bd=0, padx=22, pady=8, font=theme.FONTS["bold"],
                                    activebackground=theme.COLORS["accent_hover"],
                                    command=self.launch_selected)
        self.launch_btn.pack(side="right", padx=8)
        self._select_launch_category("原版游戏")
        return page

    def _select_launch_category(self, category: str) -> None:
        self.launch_category = category
        for name, button in self.launch_cat_buttons.items():
            button.configure(
                bg=theme.COLORS["card"] if name == category else theme.COLORS["panel"],
                fg=theme.COLORS["text"] if name == category else theme.COLORS["muted"],
            )
        self._render_launch_list()

    def _render_launch_list(self) -> None:
        if not hasattr(self, "launch_tree"):
            return
        tree = self.launch_tree
        for iid in tree.get_children():
            tree.delete(iid)
        self.instance_by_iid.clear()

        if self.launch_category == "Java":
            try:
                candidates = list_java(self._game_dir())
            except Exception as exc:  # noqa: BLE001
                self.log(f"检测 Java 失败：{exc}", "ERROR")
                candidates = []
            for index, java in enumerate(candidates):
                iid = tree.insert("", "end", values=(java.label, f"Java {java.major}", java.source, str(java.path)))
                self.instance_by_iid.pop(iid, None)
            self.status.set(f"检测到 {len(candidates)} 个 Java")
            return

        if self.launch_category == "光影":
            tree.insert("", "end", values=("光影管理将在后续版本提供", "", "", ""))
            self.status.set("光影功能尚未实现")
            return

        query = self.launch_search_var.get().strip().lower()
        items = []
        for item in self.instances:
            if self.launch_category == "原版游戏" and item.loader != "原版":
                continue
            if self.launch_category == "Mod" and item.loader == "原版":
                continue
            if query and query not in item.id.lower() and query not in item.mc_version.lower():
                continue
            items.append(item)
        for item in items:
            iid = tree.insert(
                "", "end",
                values=(item.id, item.loader, item.java_major or "?", format_bytes(item.size_bytes)),
            )
            self.instance_by_iid[iid] = item
        self.status.set(f"{len(items)} 个实例")

    # --------------------------------------------------------------- 下载页
    def _build_download_page(self) -> tk.Frame:
        page = tk.Frame(self.container, bg=theme.COLORS["bg"])
        page.grid_columnconfigure(1, weight=1)
        page.grid_rowconfigure(0, weight=1)

        side = tk.Frame(page, bg=theme.COLORS["panel"], width=190)
        side.grid(row=0, column=0, sticky="ns", padx=(10, 0), pady=10)
        side.grid_propagate(False)
        tk.Label(side, text="资源", bg=theme.COLORS["panel"], fg=theme.COLORS["muted"],
                 font=theme.FONTS["small"], anchor="w").pack(fill="x", padx=14, pady=(14, 6))
        self.download_cat_buttons: dict[str, tk.Button] = {}
        for category in DOWNLOAD_CATEGORIES:
            button = tk.Button(
                side, text=category, anchor="w", relief="flat", bd=0, padx=16, pady=9,
                bg=theme.COLORS["panel"], fg=theme.COLORS["text"], activebackground=theme.COLORS["selection"],
                font=theme.FONTS["base"], cursor="hand2",
                command=lambda name=category: self._select_download_category(name),
            )
            button.pack(fill="x", padx=8, pady=1)
            self.download_cat_buttons[category] = button

        right = tk.Frame(page, bg=theme.COLORS["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        top = tk.Frame(right, bg=theme.COLORS["bg"])
        top.grid(row=0, column=0, sticky="we", pady=(0, 8))
        self.search_var = tk.StringVar()
        entry = tk.Entry(top, textvariable=self.search_var, bg=theme.COLORS["card"],
                         fg=theme.COLORS["text"], insertbackground=theme.COLORS["text"],
                         relief="flat", font=theme.FONTS["base"])
        entry.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 8))
        entry.bind("<Return>", lambda _e: self._render_remote())
        self.snapshots_var = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="快照", variable=self.snapshots_var, bg=theme.COLORS["bg"],
                       fg=theme.COLORS["muted"], activebackground=theme.COLORS["bg"],
                       selectcolor=theme.COLORS["card"], font=theme.FONTS["small"],
                       command=self._render_remote).pack(side="left", padx=4)
        tk.Button(top, text="刷新", bg=theme.COLORS["card"], fg=theme.COLORS["text"], bd=0,
                  font=theme.FONTS["base"], padx=14, command=self.refresh_remote).pack(side="left", padx=4)

        self.download_body = tk.Frame(right, bg=theme.COLORS["bg"])
        self.download_body.grid(row=1, column=0, sticky="nsew")
        self.download_body.grid_rowconfigure(0, weight=1)
        self.download_body.grid_columnconfigure(0, weight=1)

        self.remote_tree = ttk.Treeview(self.download_body, columns=("id", "type", "date"),
                                        show="headings", selectmode="browse")
        for column, title, width in (("id", "版本", 240), ("type", "类型", 110), ("date", "发布时间", 130)):
            self.remote_tree.heading(column, text=title)
            self.remote_tree.column(column, anchor="w", width=width)
        self.remote_tree.grid(row=0, column=0, sticky="nsew")
        self.remote_tree.bind("<Double-1>", lambda _e: self.install_selected())

        self.placeholder = tk.Label(
            self.download_body, text="市场 / 社区 / 其他资源仍在规划中\n当前可用：官方版本、已下载实例",
            bg=theme.COLORS["bg"], fg=theme.COLORS["muted"], font=theme.FONTS["base"],
            justify="center",
        )

        bottom = tk.Frame(right, bg=theme.COLORS["bg"])
        bottom.grid(row=2, column=0, sticky="we", pady=(8, 0))
        self.install_btn = tk.Button(bottom, text="安装所选版本", bg=theme.COLORS["accent"],
                                     fg="#ffffff", bd=0, padx=20, pady=8, font=theme.FONTS["bold"],
                                     activebackground=theme.COLORS["accent_hover"],
                                     command=self.install_selected)
        self.install_btn.pack(side="left")
        self.cancel_btn = tk.Button(bottom, text="取消", bg=theme.COLORS["card"], fg=theme.COLORS["text"],
                                    bd=0, padx=14, pady=8, command=self.cancel_task, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=12)
        self.progress_label = tk.Label(bottom, text="", bg=theme.COLORS["bg"],
                                       fg=theme.COLORS["muted"], font=theme.FONTS["small"])
        self.progress_label.pack(side="left")

        self._select_download_category("官方")
        return page

    def _select_download_category(self, category: str) -> None:
        self.download_category = category
        for name, button in self.download_cat_buttons.items():
            button.configure(
                bg=theme.COLORS["card"] if name == category else theme.COLORS["panel"],
                fg=theme.COLORS["text"] if name == category else theme.COLORS["muted"],
            )
        if category in ("官方", "已下载"):
            self.placeholder.grid_forget()
            self.remote_tree.grid(row=0, column=0, sticky="nsew")
            if category == "官方":
                self._render_remote()
            else:
                self._render_downloaded()
        else:
            self.remote_tree.grid_forget()
            self.placeholder.configure(text=f"{category} 资源仍在规划中\n当前可用：官方版本、已下载实例")
            self.placeholder.grid(row=0, column=0, sticky="nsew")

    def _render_downloaded(self) -> None:
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.remote_by_iid.clear()
        for item in self.instances:
            self.remote_tree.insert("", "end", values=(item.id, item.loader, format_bytes(item.size_bytes)))

    # --------------------------------------------------------------- 设置页
    def _build_settings_page(self) -> tk.Frame:
        page = tk.Frame(self.container, bg=theme.COLORS["bg"])
        card = tk.Frame(page, bg=theme.COLORS["panel"])
        card.pack(fill="both", expand=True, padx=14, pady=12)

        self.settings_vars: dict[str, tk.StringVar] = {}
        fields = [
            ("下载源（镜像地址）", "download_base", self.config.download_base),
            ("访问令牌", "download_token", self.config.download_token),
            ("游戏目录", "game_dir", self.config.game_dir or str(self._game_dir())),
            ("代理（可选）", "proxy", self.config.proxy),
            ("并发下载数", "max_workers", str(self.config.max_workers)),
        ]
        for row, (label, key, value) in enumerate(fields):
            tk.Label(card, text=label, bg=theme.COLORS["panel"], fg=theme.COLORS["text"],
                     font=theme.FONTS["base"], anchor="w").grid(row=row, column=0, sticky="w", padx=18, pady=12)
            var = tk.StringVar(value=value)
            entry = tk.Entry(card, textvariable=var, bg=theme.COLORS["card"], fg=theme.COLORS["text"],
                             insertbackground=theme.COLORS["text"], relief="flat", font=theme.FONTS["base"])
            entry.grid(row=row, column=1, sticky="we", padx=(0, 18), pady=12, ipady=6)
            self.settings_vars[key] = var

        self.skip_assets_var = tk.BooleanVar(value=self.config.skip_assets)
        tk.Checkbutton(card, text="安装时跳过资源文件（更快）", variable=self.skip_assets_var,
                       bg=theme.COLORS["panel"], fg=theme.COLORS["text"],
                       selectcolor=theme.COLORS["card"], activebackground=theme.COLORS["panel"]
                       ).grid(row=len(fields), column=0, columnspan=2, sticky="w", padx=18, pady=8)
        card.grid_columnconfigure(1, weight=1)

        buttons = tk.Frame(card, bg=theme.COLORS["panel"])
        buttons.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", padx=18, pady=16)
        tk.Button(buttons, text="保存设置", bg=theme.COLORS["accent"], fg="#ffffff", bd=0,
                  padx=20, pady=8, font=theme.FONTS["bold"], command=self.save_settings).pack(side="left")
        tk.Button(buttons, text="重新获取版本列表", bg=theme.COLORS["card"], fg=theme.COLORS["text"],
                  bd=0, padx=16, pady=8, command=self.refresh_remote).pack(side="left", padx=8)
        return page

    def save_settings(self) -> None:
        variables = self.settings_vars
        self.config.download_base = variables["download_base"].get().strip().rstrip("/")
        self.config.download_token = variables["download_token"].get().strip()
        self.config.game_dir = variables["game_dir"].get().strip()
        self.config.proxy = variables["proxy"].get().strip()
        try:
            self.config.max_workers = max(1, min(16, int(variables["max_workers"].get())))
        except ValueError:
            self.config.max_workers = 8
        self.config.skip_assets = bool(self.skip_assets_var.get())
        self.config.username = self.username_var.get().strip() or "Player"
        save_config(self.config, self.data_dir)
        self.source_label.configure(text=f"源：{self.config.download_base or '未配置'}")
        self.refresh_instances()
        if self.config.download_base:
            self.refresh_remote()
        self.status.set("设置已保存")
        self.log("设置已保存", "SYSTEM")

    # --------------------------------------------------------------- 事件流
    def log(self, text: str, level: str = "INFO") -> None:
        self.events.put(("log", (level, text)))

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    level, text = payload  # type: ignore[misc]
                    self.log_view.append(text, level)
                elif kind == "progress":
                    self._show_progress(payload)  # type: ignore[arg-type]
                elif kind == "remote":
                    self.manifest = payload  # type: ignore[assignment]
                    if self.download_category == "官方":
                        self._render_remote()
                elif kind == "instances":
                    self._apply_instances(payload)  # type: ignore[arg-type]
                elif kind == "task_started":
                    self.install_btn.configure(state="disabled")
                    self.cancel_btn.configure(state="normal")
                elif kind == "task_done":
                    self.install_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.progress.stop()
                    self.progress_label.configure(text="")
                    self.refresh_instances()
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "launch_started":
                    self.launch_btn.configure(state="disabled")
                    self.stop_btn.configure(state="normal")
                elif kind == "launch_done":
                    self.launch_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.process = None
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    # --------------------------------------------------------------- 数据
    def _game_dir(self) -> Path:
        return Path(self.config.game_dir) if self.config.game_dir else default_game_dir()

    def refresh_instances(self) -> None:
        threading.Thread(
            target=lambda: self.events.put(("instances", discover_instances(self._game_dir()))),
            daemon=True,
        ).start()

    def _apply_instances(self, items: list[Instance]) -> None:
        self.instances = items
        self._render_launch_list()

    def refresh_remote(self) -> None:
        if not self.config.download_base:
            messagebox.showwarning(APP_NAME, "请先在“设置”里填写下载源地址（例如 https://mc.yjlt.top）")
            return
        self.status.set("正在获取版本清单…")
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
                path = self._manifest_cache_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            self.events.put(("remote", manifest))
            self.events.put(("status", f"共 {len(manifest.versions)} 个版本"))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("log", ("ERROR", f"获取版本清单失败：{exc}")))
            self.events.put(("status", "版本清单获取失败"))

    def _manifest_cache_path(self) -> Path:
        return config_dir(self.data_dir) / "cache" / "version_manifest.json"

    def _load_cached_manifest(self) -> None:
        try:
            data = json.loads(self._manifest_cache_path().read_text(encoding="utf-8"))
            self.manifest = VersionManifest.from_json(data)
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if self.download_category == "官方":
            self._render_remote()
        self.log(f"已加载缓存的版本清单：{len(self.manifest.versions)} 个版本", "INFO")

    def _render_remote(self) -> None:
        if self.manifest is None or not hasattr(self, "remote_tree"):
            return
        kinds = None if self.snapshots_var.get() else {"release"}
        items = self.manifest.filter(kinds, self.search_var.get())
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.remote_by_iid.clear()
        for item in items[:500]:
            iid = self.remote_tree.insert("", "end", values=(item.id, item.label, item.release_time[:10]))
            self.remote_by_iid[iid] = item
        self.status.set(f"{len(items)} 个版本")

    def selected_instance(self) -> Instance | None:
        selection = self.launch_tree.selection()
        return self.instance_by_iid.get(selection[0]) if selection else None

    # --------------------------------------------------------------- 安装
    def install_selected(self) -> None:
        selection = self.remote_tree.selection()
        if not selection or not self.remote_by_iid:
            messagebox.showinfo(APP_NAME, "请先在“官方”列表中选择一个版本")
            return
        summary = self.remote_by_iid.get(selection[0])
        if summary is None or self.manifest is None:
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
                self.events.put(("log", ("INFO", f"{summary.id} 安装完成：{len(result.report.succeeded)} 个文件，"
                                                 f"{result.duration:.1f}s")))
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
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=percent)
        self.progress_label.configure(
            text=f"{snap['done_bytes'] / 1048576:.1f}/{snap['total_bytes'] / 1048576:.1f} MiB "
                 f"{format_speed(snap['speed'])} {snap['files_done']}/{snap['files_total']}"
        )
        self.status.set(f"{percent or 0:.1f}%  {snap['current'][:42]}")

    # --------------------------------------------------------------- 启动
    def launch_selected(self) -> None:
        if self.launch_category == "Java":
            messagebox.showinfo(APP_NAME, "当前页是 Java 列表，请切换回实例分类")
            return
        instance = self.selected_instance()
        if instance is None:
            messagebox.showinfo(APP_NAME, "请先选择一个实例")
            return
        if self.process is not None and self.process.running:
            messagebox.showinfo(APP_NAME, "游戏已在运行")
            return
        self.config.username = self.username_var.get().strip() or "Player"
        try:
            self.config.memory_mb = max(512, int(self.memory_var.get()))
        except ValueError:
            pass
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
                java_path=self.config.java_overrides.get(instance.id),
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
                level = "INFO" if code == 0 else "ERROR"
                self.events.put(("log", (level, f"游戏进程退出，代码 {code}")))
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

    def _on_close(self) -> None:
        try:
            self.config.window = {"w": self.root.winfo_width(), "h": self.root.winfo_height()}
            save_config(self.config, self.data_dir)
        finally:
            if self.process is not None and self.process.running:
                if messagebox.askyesno(APP_NAME, "游戏仍在运行，确定退出启动器？"):
                    self.process.stop()
                else:
                    return
            self.root.destroy()


def default_game_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"


def run_app(config: Config, data_dir: str | Path | None = None) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    theme.apply_theme(root)
    LauncherApp(root, config, data_dir)
    root.mainloop()
