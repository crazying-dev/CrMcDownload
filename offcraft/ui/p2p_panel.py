"""离线联机（P2P）面板：以「房间名 + 密码」创建/开放/加入/删除房间，走端到端加密隧道。

作为**特权官方插件**（capability ``net.tunnel``）的入口，只在插件页卡片「进入」后打开。
界面严守产品红线：不显示镜像域名 / 中继地址；密码只在会话内存派生密钥、落盘经机器绑定
加密（见 :mod:`offcraft.p2p.vault`）。所有信令与隧道都在后台线程进行，结果经 ``window.post``
回投主线程，主线程不阻塞。

房间语义（与服务端对齐）：

- ``创建房间``：专用入口，把全局唯一房间名登记进服务端（每设备配额，重名/超额报错）；
- ``开放``：房主为已创建的房间开一个实时会话供他人加入；
- ``加入``：仅连接已开放的会话（房间不存在/未开放即报错，绝不自动建房），成功后本地加密存名+密码；
- ``删除``：房主删除自己创建的房间，释放名额、名字可复用。
"""

from __future__ import annotations

import threading
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from offcraft.ui.theme import MUTED
from offcraft.ui.widgets import Card

__all__ = ["P2PPanel"]


def _engine():
    """解析离线联机引擎：优先用插件包提供的 ``offcraft_p2p``（冻结基础包不内置）。

    基础安装包排除了 ``offcraft.p2p``，引擎随特权插件下载后以顶层包 ``offcraft_p2p``
    进入 ``sys.path``；开发/测试环境则直接命中源码树内的 ``offcraft.p2p``。两者对外接口
    完全一致，故上层无需区分来源。
    """
    import importlib

    for name in ("offcraft_p2p", "offcraft.p2p"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise ImportError("离线联机引擎未就绪，请先在插件页下载并启用「离线联机」。")


def _vault_class():
    import importlib

    for pkg in ("offcraft_p2p", "offcraft.p2p"):
        try:
            return importlib.import_module(pkg + ".vault").SavedRooms
        except ImportError:
            continue
    raise ImportError("离线联机引擎未就绪，请先在插件页下载并启用「离线联机」。")


class P2PPanel(QWidget):
    def __init__(self, window: Any) -> None:
        super().__init__()
        self.window = window
        self._room: Any = None
        self._tunnel: Any = None
        self._cancel = threading.Event()
        self._build()
        self._reload_saved()

    # ---------------------------------------------------------------- 布局
    def _build(self) -> None:
        from offcraft.ui.app import u

        root = QVBoxLayout(self)
        root.setContentsMargins(u(0.75), u(0.75), u(0.75), u(0.75))
        root.setSpacing(10)

        hint = QLabel(
            "把不同局域网的设备拉进同一虚拟房间：先用「创建房间」登记一个全局唯一的房间名，"
            "房主再把世界「对局域网开放」并「开放」该房间；加入者凭房间名与密码「加入」，"
            "游戏会自动连接本地隧道端口。加入成功后房间名与密码会加密保存在本机（换机不可解）。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        root.addWidget(hint)

        form_card = Card(self, margins=(14, 12, 14, 12))
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("房间名（全局唯一，不区分大小写）")
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("密码（仅本地派生密钥，加密存本机）")
        self.world_label = QLineEdit("我的世界")
        self.world_label.setPlaceholderText("世界名称（房主填写）")
        self.lan_port = QLineEdit("25565")
        self.lan_port.setPlaceholderText("本机局域网端口（对局域网开放后显示）")
        form.addRow("房间名", self.name_edit)
        form.addRow("密码", self.password_edit)
        form.addRow("世界", self.world_label)
        form.addRow("端口", self.lan_port)
        form_card.add_layout(form)
        root.addWidget(form_card)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.create_button = QPushButton("创建房间")
        self.open_button = QPushButton("开放")
        self.open_button.setObjectName("primary")
        self.join_button = QPushButton("加入")
        self.join_button.setObjectName("primary")
        self.delete_button = QPushButton("删除")
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("ghost")
        self.stop_button.setEnabled(False)
        for button in (self.create_button, self.open_button, self.join_button,
                       self.delete_button, self.stop_button):
            button.setMinimumHeight(34)
            actions.addWidget(button)
        actions.addStretch(1)
        root.addLayout(actions)

        self.create_button.clicked.connect(self.on_create)
        self.open_button.clicked.connect(self.on_open)
        self.join_button.clicked.connect(self.on_join)
        self.delete_button.clicked.connect(self.on_delete)
        self.stop_button.clicked.connect(self.on_stop)

        self.status_label = QLabel("未连接。")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {MUTED};")
        root.addWidget(self.status_label)

        saved_row = QHBoxLayout()
        saved_title = QLabel("已存房间（双击可快速加入）")
        saved_title.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        saved_row.addWidget(saved_title)
        saved_row.addStretch(1)
        forget_button = QPushButton("移除所选")
        forget_button.setObjectName("ghost")
        forget_button.setMinimumHeight(26)
        forget_button.clicked.connect(self.on_forget)
        saved_row.addWidget(forget_button)
        root.addLayout(saved_row)

        self.saved_list = QListWidget()
        self.saved_list.itemDoubleClicked.connect(self._on_saved_activate)
        root.addWidget(self.saved_list, 1)

        self.roster_label = QLabel("房间内暂无其它成员。")
        self.roster_label.setWordWrap(True)
        self.roster_label.setStyleSheet(f"color: {MUTED}; font-size: 9pt;")
        root.addWidget(self.roster_label)

        self._sync_buttons()

    # ---------------------------------------------------------------- 状态
    def _running(self) -> bool:
        return self._room is not None

    def _sync_buttons(self) -> None:
        running = self._running()
        for button in (self.create_button, self.open_button, self.join_button, self.delete_button):
            button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _set_roster(self, peers: list[Any]) -> None:
        others = [p for p in peers if getattr(p, "pid", "") != (self._room.sid if self._room else "")]
        if not others:
            self.roster_label.setText("房间内暂无其它成员。")
            return
        names = "、".join(
            f"{(p.world or '远端世界')}:{p.port}" if getattr(p, "world", "") else "成员"
            for p in others
        )
        self.roster_label.setText(f"房间内成员：{names}")

    # ---------------------------------------------------------------- 依赖
    def _device_id(self) -> str:
        from offcraft.config import save_config

        device = _engine().load_or_create_device_id(self.window.config)
        save_config(self.window.config, getattr(self.window, "data_dir", None))
        return device

    def _mirror(self):
        from offcraft.mirror import Mirror

        return Mirror(self.window.config.download_base)

    def _vault(self):
        return _vault_class()

    def _client(self, name: str, password: str, *, world: str = "", port: int = 0):
        RoomClient = _engine().RoomClient
        return RoomClient(
            downloader=self.window.make_downloader(progress=False),
            mirror=self._mirror(),
            name=name,
            password=password,
            device_id=self._device_id(),
            world=world,
            port=port,
        )

    # ---------------------------------------------------------------- 输入校验
    def _read_inputs(self, *, need_target: bool = True) -> tuple[str, str, str, int] | None:
        name = self.name_edit.text().strip()
        password = self.password_edit.text()
        if not name:
            self.window.notice("请填写房间名。", "WARN")
            return None
        if need_target and not password:
            self.window.notice("请填写房间密码。", "WARN")
            return None
        world = self.world_label.text().strip() or "我的世界"
        try:
            port = int(self.lan_port.text().strip() or "0")
        except ValueError:
            port = 0
        return name, password, world, port

    def _validate_port(self, port: int) -> bool:
        if not 1 <= port <= 65535:
            self.window.notice("请填写 1-65535 的本机局域网端口。", "WARN")
            return False
        return True

    # ---------------------------------------------------------------- 动作
    def on_create(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs()
        if inputs is None:
            return
        name, password, _world, _port = inputs
        self._set_status("正在登记房间……")

        def worker() -> None:
            try:
                resp = self._client(name, password).create()
                message = f"房间「{name}」已创建。" if resp.get("ok") else f"创建失败：{resp}"
            except Exception as exc:  # noqa: BLE001 - 后台兜底
                message = f"创建失败：{exc}"
            self.window.post(lambda m=message: self._set_status(m))

        threading.Thread(target=worker, name="p2p-create", daemon=True).start()

    def on_delete(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs(need_target=False)
        if inputs is None:
            return
        name = inputs[0]
        self._set_status("正在删除房间……")

        def worker() -> None:
            try:
                resp = self._client(name, "x").delete()
                removed = bool(resp.get("ok"))
                if removed:
                    self._vault().remove(name)
                message = f"房间「{name}」已删除。" if removed else f"删除失败：{resp}"
            except Exception as exc:  # noqa: BLE001
                message = f"删除失败：{exc}"
            self.window.post(lambda m=message: (self._set_status(m), self._reload_saved()))

        threading.Thread(target=worker, name="p2p-delete", daemon=True).start()

    def on_open(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs()
        if inputs is None:
            return
        name, password, world, port = inputs
        if not self._validate_port(port):
            return
        self._start_session(name, password, world, port, host=True)

    def on_join(self) -> None:
        if self._running():
            return
        inputs = self._read_inputs()
        if inputs is None:
            return
        name, password, _world, _port = inputs
        self._start_session(name, password, "", 0, host=False)

    def _start_session(self, name: str, password: str, world: str, port: int, *, host: bool) -> None:
        self.window.config.p2p_enabled = True
        self.window.config.p2p_last_room = name[:64]
        self._cancel = threading.Event()
        self._set_status("正在连接信令服务……")
        self._sync_buttons()
        threading.Thread(
            target=self._worker, args=(name, password, world, port, host),
            name="p2p-session", daemon=True,
        ).start()

    def _worker(self, name: str, password: str, world: str, port: int, host: bool) -> None:
        try:
            room = self._client(name, password, world=world, port=port)
            resp = room.open() if host else room.join()
            if self._cancel.is_set():
                room.leave()
                return
            self.window.post(lambda r=room, rp=resp, h=host: self._on_joined(r, rp, h))
        except Exception as exc:  # noqa: BLE001 - 后台兜底
            message = f"连接失败：{exc}"
            self.window.post(lambda m=message: self._on_failed(m))

    def _on_joined(self, room: Any, resp: dict, host: bool) -> None:
        engine = _engine()
        self._room = room
        if host:
            tunnel = engine.HostTunnel(room, room.port, cancel=self._cancel)
            tunnel.start()
            self._tunnel = tunnel
            self._set_status("房间已开放，等待玩家加入。关闭此面板前请点「停止」。")
        else:
            self._vault().add(room.name, self.password_edit.text())
            self._reload_saved()
            tunnel = engine.JoinTunnel(room, room.host_sid, cancel=self._cancel)
            tunnel.serve_forever()
            self._tunnel = tunnel
            address = f"localhost:{tunnel.port}"
            self._set_status(f"已进入房间，正在通过本地隧道连接：{address}")
            self.window.join_server(address)
        self._set_roster(getattr(room, "peers", []))
        self._sync_buttons()

    def _on_failed(self, message: str) -> None:
        self._set_status(message)
        self._sync_buttons()

    def on_stop(self) -> None:
        self._cancel.set()
        room, tunnel = self._room, self._tunnel
        self._room = self._tunnel = None
        if tunnel is not None:
            try:
                tunnel.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if room is not None:
            threading.Thread(target=room.leave, name="p2p-leave", daemon=True).start()
        self._set_status("已停止。")
        self.roster_label.setText("房间内暂无其它成员。")
        self._sync_buttons()

    # ---------------------------------------------------------------- 已存房间
    def _reload_saved(self) -> None:
        try:
            names = self._vault().names()
        except Exception:  # noqa: BLE001 - 存储不可用时仅清空列表
            names = []
        self.saved_list.clear()
        for name in names:
            self.saved_list.addItem(QListWidgetItem(name))

    def _on_saved_activate(self, item: QListWidgetItem) -> None:
        if self._running():
            self.window.notice("请先停止当前会话再加入其它房间。", "WARN")
            return
        name = item.text()
        try:
            password = self._vault().password(name)
        except Exception as exc:  # noqa: BLE001 - 换机后无法解密等
            self.window.notice(f"读取本地密码失败：{exc}", "WARN")
            return
        self.name_edit.setText(name)
        self.password_edit.setText(password)
        if password:
            self.on_join()

    def on_forget(self) -> None:
        item = self.saved_list.currentItem()
        if item is None:
            return
        self._vault().remove(item.text())
        self._reload_saved()

    def shutdown(self) -> None:
        """面板销毁 / 退出时清理会话。"""
        self.on_stop()
