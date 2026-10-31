"""离线联机（P2P）密码学：房间密钥派生、准入 proof、数据面 AEAD 帧通道。

设计（与 ``server_mirror.p2p`` 对齐）：

- 预共享密钥 PSK + 房间码 → ``room_secret``（BLAKE2b keyed）；再派生
  ``auth_key``（准入 MAC 密钥）与两个方向的数据面密钥 ``key_ab`` / ``key_ba``。
  PSK 与 room_secret 永不离开本机；``auth_key`` 仅由房主在开启房间时登记给镜像，
  服务端只能据此校验准入 HMAC，无法解密数据面。
- 准入 proof：``HMAC-SHA256(auth_key, PREFIX || client_pub || nonce)``（标准库实现，
  与服务端逐字节一致）。
- 数据面：ChaCha20-Poly1305(IETF) AEAD，每帧带 12 字节「单调递增」nonce；接收侧
  强制序号前进以拒绝重放，认证标签失败即视为篡改。

依赖 ``pynacl``（libsodium 绑定）仅用于 AEAD；派生与 MAC 用标准库。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from hashlib import sha256

from nacl.bindings.crypto_aead import (
    crypto_aead_chacha20poly1305_ietf_decrypt as _aead_open,
    crypto_aead_chacha20poly1305_ietf_encrypt as _aead_seal,
)
from nacl.exceptions import CryptoError

__all__ = [
    "PROOF_MESSAGE_PREFIX",
    "RoomKeys",
    "P2PError",
    "TamperError",
    "canonical_name",
    "derive_room_keys",
    "proof_message",
    "make_proof",
    "new_identity",
    "load_or_create_device_id",
    "CryptoChannel",
    "make_channel",
    "KEY_BYTES",
    "NONCE_BYTES",
    "TAG_BYTES",
]

#: proof 覆盖的域分隔前缀，必须与服务端 ``PROOF_MESSAGE_PREFIX`` 完全一致
PROOF_MESSAGE_PREFIX = b"offcraft-p2p-v1|"
KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
_NONCE_PREFIX = 4 * b"\x00"


class P2PError(RuntimeError):
    """离线联机通用错误。"""


class TamperError(P2PError):
    """AEAD 认证失败：数据被篡改、密钥不符或检测到重放。"""


def _blake2b(message: bytes, key: bytes) -> bytes:
    return hashlib.blake2b(message, key=key, digest_size=KEY_BYTES).digest()


def canonical_name(name: str) -> str:
    """房间名规范化：去首尾空白、转小写、截 64 字节（与服务端唯一键一致）。

    房主与加入者都必须用同一规范化后的房间名派生密钥，否则同一房间算不出同
    一把 ``auth_key``（服务端不区分大小写做唯一）。只影响密钥派生输入，不改密码。
    """
    return (name or "").strip().lower()[:64]


@dataclass(frozen=True, slots=True)
class RoomKeys:
    auth_key: bytes     # 准入 MAC 密钥（房主登记给镜像）
    key_ab: bytes       # 数据面：房主 A -> 加入者 B
    key_ba: bytes       # 数据面：加入者 B -> 房主 A


def derive_room_keys(password: str, name: str) -> RoomKeys:
    """从密码 + 房间名（规范化小写）确定性派生三把子密钥。"""
    if not password:
        raise P2PError("房间密码不能为空")
    key = canonical_name(name).encode("utf-8")
    if not key:
        raise P2PError("房间名不能为空")
    room_secret = _blake2b(password.encode("utf-8"), key=key)
    return RoomKeys(
        auth_key=_blake2b(b"offcraft-p2p|auth", key=room_secret),
        key_ab=_blake2b(b"offcraft-p2p|a2b", key=room_secret),
        key_ba=_blake2b(b"offcraft-p2p|b2a", key=room_secret),
    )


def proof_message(client_pub: bytes, nonce: bytes) -> bytes:
    return PROOF_MESSAGE_PREFIX + client_pub + nonce


def make_proof(auth_key: bytes, client_pub_hex: str, nonce_hex: str) -> str:
    """加入者用本地派生的 auth_key 计算准入 MAC（hex）。"""
    message = proof_message(bytes.fromhex(client_pub_hex), bytes.fromhex(nonce_hex))
    return hmac.new(auth_key, message, sha256).hexdigest()


def new_identity() -> tuple[str, str]:
    """临时身份：32 字节公钥标识 + 16 字节随机 nonce（均 hex）。

    Phase 1 只把它当作会话内唯一标识用于绑定 proof；Phase 2 打洞/DH 时换成真正的
    X25519 公钥。
    """
    return secrets.token_hex(KEY_BYTES), secrets.token_hex(16)


def load_or_create_device_id(config: object) -> str:
    """返回稳定的设备标识；缺失则生成一个 32 位 hex 写回 ``config.p2p_device_id``。

    调用方负责随后 ``save_config`` 落盘（``Config.normalize()`` 也会兜底补齐）。
    设备标识是服务端每房间配额（每设备 ≤ 10）的软锚点：重装/清配置会重置。
    """
    current = getattr(config, "p2p_device_id", "") or ""
    if 8 <= len(current) <= 64 and all(c in "0123456789abcdef" for c in current):
        return current
    device = secrets.token_hex(16)
    try:
        config.p2p_device_id = device  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 配置不可写时仍返回新标识（仅会话内用）
        pass
    return device


class CryptoChannel:
    """一条方向加密通道：持有发送/接收两把密钥，nonce 用递增计数器。

    TCP 保证有序，故接收侧要求序号严格前进；一旦回退即判定为重放。
    """

    def __init__(self, tx_key: bytes, rx_key: bytes) -> None:
        if len(tx_key) != KEY_BYTES or len(rx_key) != KEY_BYTES:
            raise P2PError("通道密钥必须是 32 字节")
        self._tx = tx_key
        self._rx = rx_key
        self._tx_seq = 0
        self._rx_seq = 0

    @staticmethod
    def _nonce(seq: int) -> bytes:
        return _NONCE_PREFIX + seq.to_bytes(8, "big")

    def encrypt(self, plaintext: bytes, channel: int = 0) -> tuple[bytes, bytes]:
        """返回 ``(nonce, ciphertext+tag)``，交由传输层组帧。"""
        ad = channel.to_bytes(4, "big")
        nonce = self._nonce(self._tx_seq)
        self._tx_seq += 1
        body = _aead_seal(plaintext, ad, nonce, self._tx)
        return nonce, body

    def decrypt(self, nonce: bytes, body: bytes, channel: int = 0) -> bytes:
        if len(nonce) != NONCE_BYTES:
            raise TamperError("nonce 长度非法")
        ad = channel.to_bytes(4, "big")
        try:
            plaintext = _aead_open(body, ad, nonce, self._rx)
        except CryptoError as exc:
            raise TamperError("解密失败：数据被篡改或密钥不符") from exc
        seq = int.from_bytes(nonce[4:], "big")
        if seq < self._rx_seq:
            raise TamperError("检测到重放：nonce 序号未前进")
        self._rx_seq = seq + 1
        return plaintext


def make_channel(keys: RoomKeys, *, is_host: bool) -> CryptoChannel:
    """按角色装配方向密钥：房主 A 发 key_ab 收 key_ba，加入者 B 反之。"""
    if is_host:
        return CryptoChannel(keys.key_ab, keys.key_ba)
    return CryptoChannel(keys.key_ba, keys.key_ab)
