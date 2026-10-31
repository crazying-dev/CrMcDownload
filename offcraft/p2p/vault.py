"""离线联机本地存储：机器绑定加密保存「加入过的房间名 + 密码」。

红线更新：按产品决策，加入房间后把房间名与密码**加密**存本地，解密方法**每台机器不同**：

- 首选 **Windows DPAPI**（``crypt32.CryptProtectData``，per-user + per-machine 作用域）：
  密文只能在同一台机器、同一 Windows 账户下解出，换机即解不开，天然满足「每台机器解密
  方法不同」，且无需落盘任何密钥文件。
- 非 Windows / DPAPI 不可用时，回落首启随机生成的 ``machine.key``（32 字节，``0600``）
  + pynacl ``SecretBox``（XSalsa20-Poly1305）加密；密钥文件同样每机唯一。

局限（已与用户确认）：同账户同机可还原明文密码——本特性用于离线联机，接受该权衡。
明文密码只在内存短暂存在，落盘的永远是密文 blob（带方法前缀 ``dpapi:`` / ``sb:``）。
"""

from __future__ import annotations

import base64
import ctypes
import os
import secrets
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from offcraft.config import config_dir

__all__ = ["protect", "unprotect", "SavedRooms"]

# DPAPI 附加熵：常量，挡不住整机同账户的重放，但挡住「不知情地用别的用途 blob」
_ENTROPY = b"offcraft-p2p-vault-v1"
_MACHINE_KEY_BYTES = 32
_DPAPI_FORBIDDEN_UI = 0x01


class VaultError(RuntimeError):
    """解密失败或存储后端不可用。"""


# ------------------------------------------------------------------ DPAPI
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> "_DATA_BLOB":
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _dpapi_call(fn, data: bytes) -> bytes:
    in_blob = _blob(data)
    entropy = _blob(_ENTROPY)
    out = _DATA_BLOB()
    ok = fn(ctypes.byref(in_blob), None, ctypes.byref(entropy), None, None,
            _DPAPI_FORBIDDEN_UI, ctypes.byref(out))
    if not ok:
        raise OSError(f"CryptProtectData/CryptUnprotectData 失败：{ctypes.GetLastError()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(ctypes.windll.crypt32.CryptProtectData, data)


def _dpapi_unprotect(data: bytes) -> bytes:
    return _dpapi_call(ctypes.windll.crypt32.CryptUnprotectData, data)


# --------------------------------------------------- 回落：机器密钥 + SecretBox
def _machine_key_file() -> Path:
    return config_dir() / "machine.key"


def _machine_key() -> bytes:
    path = _machine_key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        raw = path.read_bytes()
        if len(raw) == _MACHINE_KEY_BYTES:
            return raw
    key = secrets.token_bytes(_MACHINE_KEY_BYTES)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    except OSError:
        path.write_bytes(key)
    return key


def _secretbox_encrypt(plaintext: bytes) -> bytes:
    from nacl.secret import SecretBox

    box = SecretBox(_machine_key())
    return box.encrypt(plaintext)  # nonce=None → 库内随机生成，前置拼入密文


def _secretbox_decrypt(blob: bytes) -> bytes:
    from nacl.secret import SecretBox
    from nacl.exceptions import CryptoError

    box = SecretBox(_machine_key())
    try:
        return box.decrypt(blob)
    except CryptoError as exc:  # 机器密钥换了 / 密文损坏
        raise VaultError("本地密码解密失败（换机或密钥变更）") from exc


def _dpapi_available() -> bool:
    return sys.platform == "win32"


# ------------------------------------------------------------------ 对外接口
def protect(data: bytes) -> str:
    """加密任意字节，返回带方法前缀的 base64 文本（可直接落 JSON）。"""
    if _dpapi_available():
        try:
            return "dpapi:" + base64.b64encode(_dpapi_protect(data)).decode("ascii")
        except OSError:
            pass  # DPAPI 失败 → 回落机器密钥
    return "sb:" + base64.b64encode(_secretbox_encrypt(data)).decode("ascii")


def unprotect(token: str) -> bytes:
    """解密 :func:`protect` 产出的文本；方法不符或换机时抛 :class:`VaultError`。"""
    kind, _, rest = str(token or "").partition(":")
    if not rest:
        raise VaultError("密文格式非法")
    blob = base64.b64decode(rest)
    if kind == "dpapi":
        if not _dpapi_available():
            raise VaultError("该密文由 Windows DPAPI 加密，本机无法解密")
        try:
            return _dpapi_unprotect(blob)
        except OSError as exc:
            raise VaultError("DPAPI 解密失败（换机或换账户）") from exc
    if kind == "sb":
        return _secretbox_decrypt(blob)
    raise VaultError(f"未知加密方法：{kind}")


class SavedRooms:
    """「加入过的房间」列表：房间名明文可列，密码逐条机器绑定加密。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else (
            config_dir() / "plugins" / "offline-multiplayer" / "rooms.json")
        self._rooms: dict[str, str] = {}   # name(lower) -> protected(password)
        self.load()

    def load(self) -> None:
        import json

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            items = data.get("rooms") or []
            for item in items:
                if isinstance(item, dict) and item.get("name"):
                    self._rooms[str(item["name"]).strip().lower()] = str(item.get("enc") or "")

    def save(self) -> None:
        import json

        payload = {"rooms": [{"name": name, "enc": enc} for name, enc in self._rooms.items()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, name: str, password: str) -> None:
        key = str(name or "").strip().lower()
        if not key:
            return
        self._rooms[key] = protect(password.encode("utf-8"))
        self.save()

    def remove(self, name: str) -> bool:
        removed = self._rooms.pop(str(name or "").strip().lower(), None) is not None
        if removed:
            self.save()
        return removed

    def names(self) -> list[str]:
        return sorted(self._rooms)

    def password(self, name: str) -> str:
        enc = self._rooms.get(str(name or "").strip().lower(), "")
        if not enc:
            return ""
        return unprotect(enc).decode("utf-8", "replace")

    def as_list(self) -> list[dict[str, Any]]:
        return [{"name": name} for name in self.names()]
