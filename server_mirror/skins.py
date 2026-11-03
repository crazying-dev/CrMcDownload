"""皮肤托管：``/skin/<name>.png`` 的读取与上传（Pillow 校验）。

CSL（CustomSkinLoader）会把皮肤源指向 ``{base}/skin/``，请求 ``%s.png`` 或
``%s.png?model={model}``。这里只存经过 Pillow 解码校验的真 PNG，尺寸限定
``64×64``（new/slim）或 ``64×32``（classic/old）；上传需要令牌、``Content-Length``
与 64 KiB 上限；落盘用 ``.importing`` 临时名 + ``os.replace`` 原子替换。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MAX_SKIN_BYTES", "SkinError", "SkinStore", "NAME_PATTERN", "DEFAULT_SKIN_NAME"]

from .default_skin import DEFAULT_SKIN_NAME

#: Minecraft 玩家名规则（同时作为文件名，天然排除路径穿越）
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,16}$")
#: 皮肤上传上限（64 KiB）
MAX_SKIN_BYTES = 64 * 1024
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_ALLOWED_SIZES = {(64, 64), (64, 32)}


class SkinError(ValueError):
    """校验失败，携带面向用户的中文理由。"""


def _validate_png(data: bytes) -> tuple[int, int]:
    """确认是真 PNG 且尺寸合法，返回 ``(宽, 高)``；否则抛 :class:`SkinError`。"""
    if not data.startswith(_PNG_MAGIC):
        raise SkinError("文件不是有效的 PNG 格式")
    try:
        from PIL import Image  # 运行期依赖 Pillow
    except ImportError as exc:  # pragma: no cover - 部署缺库
        raise SkinError("服务端未安装 Pillow，无法校验皮肤") from exc
    try:
        with io.BytesIO(data) as buf:
            image = Image.open(buf)
            image.verify()  # 结构校验（verify 后对象不可再用）
        with io.BytesIO(data) as buf:
            image = Image.open(buf)
            size = (int(image.width), int(image.height))
            fmt = (image.format or "").upper()
    except SkinError:
        raise
    except Exception as exc:  # noqa: BLE001 - 解码失败统一成中文理由
        raise SkinError(f"PNG 解码失败：{exc}") from exc
    if fmt != "PNG":
        raise SkinError("只接受 PNG 格式的皮肤")
    if size not in _ALLOWED_SIZES:
        raise SkinError(f"皮肤尺寸必须是 64×64 或 64×32，当前 {size[0]}×{size[1]}")
    return size


@dataclass(slots=True)
class SkinStore:
    """皮肤文件的读写入口（``root`` 为皮肤根目录）。"""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def check_name(name: str) -> str:
        name = str(name or "")
        if not NAME_PATTERN.match(name):
            raise SkinError("文件名不是合法的 Minecraft 玩家名")
        return name

    def path_for(self, name: str, *, slim: bool) -> Path:
        self.check_name(name)
        return self.root / (f"{name}_slim.png" if slim else f"{name}.png")

    def read(self, name: str, *, slim: bool) -> bytes | None:
        try:
            path = self.path_for(name, slim=slim)
        except SkinError:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def write(self, name: str, data: bytes, *, model: str = "auto") -> tuple[str, int, int]:
        """校验并原子写入；返回 ``(实际模型 classic|slim, 宽, 高)``。"""
        name = self.check_name(name)
        if data is None:
            raise SkinError("缺少请求体")
        if len(data) > MAX_SKIN_BYTES:
            raise SkinError(f"皮肤文件超过 {MAX_SKIN_BYTES // 1024} KiB 上限")
        width, height = _validate_png(data)
        slim = model == "slim" or (model == "auto" and _looks_slim(data))
        path = self.path_for(name, slim=slim)
        # 同一账户只保留一种模型：切换 classic/slim 时清掉旧贴图，避免 GET 命中旧模型。
        other = self.path_for(name, slim=not slim)
        try:
            other.unlink()
        except OSError:
            pass
        tmp = path.with_name(path.name + ".importing")
        tmp.write_bytes(data)
        Path(tmp).replace(path)
        return ("slim" if slim else "classic"), width, height

    def delete(self, name: str) -> list[str]:
        """删除该账户的皮肤（classic / slim 两种都删），返回实际删除的文件名。"""
        self.check_name(name)
        removed: list[str] = []
        for slim in (False, True):
            path = self.path_for(name, slim=slim)
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
        return removed

    def list(self, *, query: str = "", limit: int = 100) -> list[dict[str, object]]:
        """列出已存皮肤（按更新时间倒序），供客户端「皮肤库」浏览。"""
        needle = str(query or "").strip().lower()
        entries: list[dict[str, object]] = []
        for path in self.root.glob("*.png"):
            stem = path.stem
            model = "slim" if stem.endswith("_slim") else "classic"
            name = stem[:-5] if model == "slim" else stem
            if not NAME_PATTERN.match(name):
                continue
            if needle and needle not in name.lower():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": name,
                    "model": model,
                    "size": int(stat.st_size),
                    "updated": round(stat.st_mtime, 3),
                }
            )
        entries.sort(key=lambda item: float(item["updated"]), reverse=True)
        return entries[: max(1, min(int(limit), 200))]

    def status(self) -> dict[str, object]:
        files = list(self.root.glob("*.png"))
        latest = max((f.stat().st_mtime for f in files), default=0.0)
        return {
            "count": len(files),
            "last_updated": round(latest, 3) if files else 0.0,
        }


def _looks_slim(data: bytes) -> bool:
    """按新版 64×64 布局右半区是否全透明判定 slim/classic（64×32 恒为 classic）。"""
    try:
        from PIL import Image

        with io.BytesIO(data) as buf:
            image = Image.open(buf).convert("RGBA")
        if image.size == (64, 32):
            return False
        right = image.crop((32, 0, 64, 32))
        alpha = right.getchannel("A")
        # 右半区（外层/附加区）有不透明像素 -> slim，否则按 classic 布局
        return alpha.getextrema()[1] > 0
    except Exception:  # noqa: BLE001 - 判定失败按 classic
        return False
