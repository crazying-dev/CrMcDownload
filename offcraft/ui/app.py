"""主窗口：下载页、实例页、设置与日志。"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from offcraft import APP_NAME, __version__
from offcraft.config import Config, save_config
from offcraft.instances import Instance, discover_instances
from offcraft.launch.auth import make_profile
from offcraft.launch.command import LaunchError, LaunchOptions, build_command
from offcraft.launch.process import GameProcess
from offcraft.meta.manifest import VersionManifest, VersionSummary
from offcraft.meta.version import VersionError, load_version
from offcraft.mirror import Mirror
from offcraft.net.downloader import Downloader, ProgressTracker
from offcraft.ui import theme
from offcraft.ui.widgets import LogView, format_bytes, format_speed

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


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

        self._build()
        self.refresh_instances()
        if self.config.download_base:
            self.refresh_remote()
        else:
            self.log("尚未配置下载源，请在“设置”中填写镜像地址（下载页不可用，实例页仍可启动）", "WARN")
        self.root.after(120, self._drain)

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        self.root.title(f"{APP_NAME} · CrMcDownload")
        self.root.geometry(f"{self.config.window.get('w', 1180)}x{self.config.window.get('h', 760)}")
        self.root.minsize(960, 640)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        header = tk.Frame(self.root, bg=theme.COLORS["panel"], height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header, text=APP_NAME, bg=theme.COLORS["panel"], fg=theme.COLORS["accent"],
            font=theme.FONTS["title"],
        ).pack(side="left", padx=(20, 8), pady=10)
        tk.Label(
            header, text="CrMcDownload · Minecraft 启动器", bg=theme.COLORS["panel"],
            fg=theme.COLORS["muted"], font=theme.FONTS["small"],
        ).pack(side="left", pady=22)
        ttk.Button(header, text="设置", command=self.open_settings).pack(side="right", padx=16, pady=14)
        self.source_label = tk.Label(
            header, text="源：未配置", bg=theme.COLORS["panel"], fg=theme.COLORS["muted"],
            font=theme.FONTS["small"],
        )
        self.source_label.pack(side="right", padx=(0, 12))

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=16, pady=(12, 6))

        self.notebook = ttk.Notebook(body)
        self.notebook.pack(fill="both", expand=True)
        self._build_download_page()
        self._build_instance_page()

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill="both", padx=16, pady=(0, 6))
        bar = ttk.Frame(log_frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="日志", style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="清空", command=lambda: self.log_view.clear()).pack(side="right", padx=4)
        ttk.Button(bar, text="复制", command=self._copy_log).pack(side="right", padx=4)
        self.log_view = LogView(log_frame)
        self.log_view.pack(fill="both", expand=True, pady=(4, 0))

        self.status = tk.StringVar(value="就绪")
        tk.Label(
            self.root, textvariable=self.status, bg=theme.COLORS["panel"],
            fg=theme.COLORS["muted"], anchor="w", font=theme.FONTS["small"], padx=16, pady=4,
        ).pack(fill="x", side="bottom")

    def _build_download_page(self) -> None:
        page = ttk.Frame(self.notebook, style="Panel.TFrame", padding=12)
        self.notebook.add(page, text="下载")

        toolbar = ttk.Frame(page, style="Panel.TFrame")
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="搜索", style="CardMuted.TLabel").pack(side="left")
        self.search_var = tk.StringVar()
        entry = ttk.Entry(toolbar, textvariable=self.search_var, width=22)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _e: self._render_remote())
        self.snapshots_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            toolbar, text="显示快照/远古版", variable=self.snapshots_var,
            command=self._render_remote,
        ).pack(side="left", padx=8)
        ttk.Button(toolbar, text="刷新", command=self.refresh_remote).pack(side="right", padx=4)

        columns = ("id", "type", "date")
        self.remote_tree = ttk.Treeview(page, columns=columns, show="headings", selectmode="browse")
        for column, title, width in (("id", "版本", 220), ("type", "类型", 100), ("date", "发布时间", 140)):
            self.remote_tree.heading(column, text=title)
            self.remote_tree.column(column, width=width, anchor="w")
        self.remote_tree.pack(fill="both", expand=True)
        self.remote_tree.bind("<Double-1>", lambda _e: self.install_selected())

        bottom = ttk.Frame(page, style="Panel.TFrame")
        bottom.pack(fill="x", pady=(8, 0))
        self.install_btn = ttk.Button(
            bottom, text="安装所选版本", style="Accent.TButton", command=self.install_selected
        )
        self.install_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bottom, text="取消", command=self.cancel_task, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=12)
        self.progress_label = tk.Label(
            bottom, text="", bg=theme.COLORS["panel"], fg=theme.COLORS["muted"], font=theme.FONTS["small"]
        )
        self.progress_label.pack(side="left")

    def _build_instance_page(self) -> None:
        page = ttk.Frame(self.notebook, style="Panel.TFrame", padding=12)
        self.notebook.add(page, text="实例")

        columns = ("id", "loader", "java", "size")
        self.instance_tree = ttk.Treeview(page, columns=columns, show="headings", selectmode="browse")
        for column, title, width in (
            ("id", "实例", 300), ("loader", "加载器", 110), ("java", "Java", 70), ("size", "大小", 110)
        ):
            self.instance_tree.heading(column, text=title)
            self.instance_tree.column(column, width=width, anchor="w")
        self.instance_tree.pack(fill="both", expand=True)
        self.instance_tree.bind("<Double-1>", lambda _e: self.launch_selected())

        settings = ttk.Frame(page, style="Panel.TFrame")
        settings.pack(fill="x", pady=(10, 0))
        ttk.Label(settings, text="玩家名", style="CardMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.username_var = tk.StringVar(value=self.config.username)
        ttk.Entry(settings, textvariable=self.username_var, width=18).grid(row=0, column=1, padx=8)
        ttk.Label(settings, text="内存 MB", style="CardMuted.TLabel").grid(row=0, column=2, sticky="w", padx=(16, 0))
        self.memory_var = tk.StringVar(value=str(self.config.memory_mb))
        ttk.Spinbox(settings, from_=1024, to=32768, increment=512, width=8, textvariable=self.memory_var).grid(
            row=0, column=3, padx=8
        )
        ttk.Button(settings, text="应用", command=self.apply_settings).grid(row=0, column=4, padx=8)

        actions = ttk.Frame(page, style="Panel.TFrame")
        actions.pack(fill="x", pady=(10, 0))
        self.launch_btn = ttk.Button(
            actions, text="启动游戏", style="Accent.TButton", command=self.launch_selected
        )
        self.launch_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="结束游戏", command=self.stop_game, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="刷新实例", command=self.refresh_instances).pack(side="right")
        ttk.Button(actions, text="打开目录", command=self.open_game_dir).pack(side="right", padx=8)

    # --------------------------------------------------------------- events
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
                    snap = payload  # type: ignore[assignment]
                    self._show_progress(snap)
                elif kind == "remote":
                    self.manifest = payload  # type: ignore[assignment]
                    self._render_remote()
                elif kind == "instances":
                    self._render_instances(payload)  # type: ignore[arg-type]
                elif kind == "task_started":
                    self.install_btn.state(["disabled"])
                    self.cancel_btn.state(["!disabled"])
                elif kind == "task_done":
                    self.install_btn.state(["!disabled"])
                    self.cancel_btn.state(["disabled"])
                    self.progress.stop()
                    self.progress_label.configure(text="")
                    self.refresh_instances()
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "launch_started":
                    self.launch_btn.state(["disabled"])
                    self.stop_btn.state(["!disabled"])
                elif kind == "launch_done":
                    self.launch_btn.state(["!disabled"])
                    self.stop_btn.state(["disabled"])
                    self.process = None
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    # -------------------------------------------------------------- remote
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
            self.events.put(("remote", manifest))
            self.events.put(("status", f"共 {len(manifest.versions)} 个版本"))
        except Exception as exc:  # noqa: BLE001
            self.events.put(("log", ("ERROR", f"获取版本清单失败：{exc}")))
            self.events.put(("status", "版本清单获取失败"))

    def _render_remote(self) -> None:
        if self.manifest is None:
            return
        kinds = None if self.snapshots_var.get() else {"release"}
        items = self.manifest.filter(kinds, self.search_var.get())
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.remote_by_iid.clear()
        for item in items[:500]:
            iid = self.remote_tree.insert("", "end", values=(item.id, item.label, item.release_time[:10]))
            self.remote_by_iid[iid] = item

    # ----------------------------------------------------------- instances
    def refresh_instances(self) -> None:
        game_dir = Path(self.config.game_dir) if self.config.game_dir else default_game_dir()
        items = discover_instances(game_dir)
        self.events.put(("instances", items))

    def _render_instances(self, items: list[Instance]) -> None:
        self.instances = items
        self.instance_tree.delete(*self.instance_tree.get_children())
        self.instance_by_iid.clear()
        for item in items:
            iid = self.instance_tree.insert(
                "", "end",
                values=(item.id, item.loader, item.java_major or "?", format_bytes(item.size_bytes)),
            )
            self.instance_by_iid[iid] = item

    def selected_instance(self) -> Instance | None:
        selection = self.instance_tree.selection()
        return self.instance_by_iid.get(selection[0]) if selection else None

    # -------------------------------------------------------------- install
    def install_selected(self) -> None:
        selection = self.remote_tree.selection()
        if not selection:
            messagebox.showinfo(APP_NAME, "请先在下载页选择一个版本")
            return
        summary = self.remote_by_iid[selection[0]]
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
            game_dir = Path(self.config.game_dir) if self.config.game_dir else default_game_dir()
            options = InstallOptions(include_assets=not self.config.skip_assets)
            plan = prepare_install(game_dir, summary.id, manifest, mirror, downloader, options=options)
            self.events.put(("log", ("INFO", f"{summary.id}：{plan.summary()}")))
            for warning in plan.warnings:
                self.events.put(("log", ("WARN", warning)))
            result = install(
                plan, downloader, tracker=ProgressTracker(), cancel=self.cancel_event
            )
            if result.ok:
                self.events.put(
                    ("log", ("INFO", f"{summary.id} 安装完成：{len(result.report.succeeded)} 个文件，"
                                     f"{result.duration:.1f}s"))
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
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=percent)
        text = (
            f"{snap['done_bytes'] / 1048576:.1f} / {snap['total_bytes'] / 1048576:.1f} MiB  "
            f"{format_speed(snap['speed'])}  {snap['files_done']}/{snap['files_total']}"
        )
        self.progress_label.configure(text=text)
        self.status.set(f"{percent or 0:.1f}%  {snap['current'][:40]}")

    # --------------------------------------------------------------- launch
    def launch_selected(self) -> None:
        instance = self.selected_instance()
        if instance is None:
            messagebox.showinfo(APP_NAME, "请先选择一个实例")
            return
        if self.process is not None and self.process.running:
            messagebox.showinfo(APP_NAME, "游戏已在运行")
            return
        self.apply_settings()
        threading.Thread(target=self._launch_worker, args=(instance,), daemon=True).start()

    def _launch_worker(self, instance: Instance) -> None:
        try:
            game_dir = Path(self.config.game_dir) if self.config.game_dir else default_game_dir()
            version = load_version(game_dir, instance.id)
            profile = make_profile(self.config.username)
            options = LaunchOptions(
                memory_mb=int(self.memory_var.get() or self.config.memory_mb),
                width=self.config.width,
                height=self.config.height,
                fullscreen=self.config.fullscreen,
                java_path=self.config.java_overrides.get(instance.id),
                extra_jvm=list(self.config.extra_jvm),
            )
            plan = build_command(version, game_dir, profile, options=options)
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

    # ------------------------------------------------------------- settings
    def apply_settings(self) -> None:
        try:
            memory = int(self.memory_var.get())
        except ValueError:
            memory = self.config.memory_mb
        self.config.username = self.username_var.get().strip() or "Player"
        self.config.memory_mb = max(512, memory)
        save_config(self.config, self.data_dir)

    def open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("设置")
        dialog.configure(bg=theme.COLORS["bg"])
        dialog.geometry("620x460")
        dialog.transient(self.root)
        dialog.grab_set()

        fields = [
            ("下载源（镜像地址）", "download_base", self.config.download_base),
            ("访问令牌（可选）", "download_token", self.config.download_token),
            ("游戏目录", "game_dir", self.config.game_dir or str(default_game_dir())),
            ("代理（可选）", "proxy", self.config.proxy),
        ]
        entries: dict[str, ttk.Entry] = {}
        for row, (label, key, value) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=row, column=0, sticky="w", padx=16, pady=10)
            entry = ttk.Entry(dialog, width=52)
            entry.insert(0, value)
            entry.grid(row=row, column=1, sticky="we", padx=(0, 16), pady=10)
            entries[key] = entry

        ttk.Label(dialog, text="并发下载数").grid(row=4, column=0, sticky="w", padx=16, pady=10)
        workers = ttk.Spinbox(dialog, from_=1, to=16, width=8)
        workers.insert(0, str(self.config.max_workers))
        workers.grid(row=4, column=1, sticky="w", padx=(0, 16), pady=10)

        skip = tk.BooleanVar(value=self.config.skip_assets)
        ttk.Checkbutton(dialog, text="安装时跳过资源文件（更快）", variable=skip).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=16, pady=6
        )
        dialog.grid_columnconfigure(1, weight=1)

        def save() -> None:
            self.config.download_base = entries["download_base"].get().strip()
            self.config.download_token = entries["download_token"].get().strip()
            self.config.game_dir = entries["game_dir"].get().strip()
            self.config.proxy = entries["proxy"].get().strip()
            try:
                self.config.max_workers = max(1, min(16, int(workers.get())))
            except ValueError:
                pass
            self.config.skip_assets = bool(skip.get())
            save_config(self.config, self.data_dir)
            self.source_label.configure(
                text=f"源：{self.config.download_base or '未配置'}"
            )
            dialog.destroy()
            self.refresh_instances()
            if self.config.download_base:
                self.refresh_remote()

        ttk.Button(dialog, text="保存", style="Accent.TButton", command=save).grid(
            row=6, column=1, sticky="e", padx=16, pady=16
        )
        ttk.Button(dialog, text="取消", command=dialog.destroy).grid(
            row=6, column=0, sticky="w", padx=16, pady=16
        )

    # ---------------------------------------------------------------- misc
    def open_game_dir(self) -> None:
        game_dir = Path(self.config.game_dir) if self.config.game_dir else default_game_dir()
        game_dir.mkdir(parents=True, exist_ok=True)
        import os

        os.startfile(str(game_dir))  # type: ignore[attr-defined]

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_view.copy_all())

    def _on_close(self) -> None:
        try:
            self.config.window = {
                "w": self.root.winfo_width(),
                "h": self.root.winfo_height(),
            }
            self.apply_settings()
        finally:
            if self.process is not None and self.process.running:
                if messagebox.askyesno(APP_NAME, "游戏仍在运行，确定退出启动器？"):
                    self.process.stop()
                else:
                    return
            self.root.destroy()


def default_game_dir() -> Path:
    import os

    return Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"


def run_app(config: Config, data_dir: str | Path | None = None) -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    theme.apply_theme(root)
    app = LauncherApp(root, config, data_dir)
    app.source_label.configure(text=f"源：{config.download_base or '未配置'}")
    root.mainloop()
