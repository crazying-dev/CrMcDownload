"""从皮肤贴图裁出头部正面，作为启动页头像（Pillow，NEAREST 放大避免模糊）。"""

from __future__ import annotations

import io

__all__ = ["head_avatar"]

#: 正面脸在皮肤上的坐标（64×64 与 64×32 布局一致）
_FACE = (8, 8, 16, 16)
#: 帽子（外层）覆盖区
_HAT = (40, 8, 48, 16)


def head_avatar(data: bytes, *, size: int = 64) -> bytes:
    """把皮肤字节裁成 ``size×size`` 的头像 PNG 字节；失败返回空字节。"""
    try:
        from PIL import Image

        with io.BytesIO(data) as buf:
            image = Image.open(buf).convert("RGBA")
        width, height = image.size
        if width < 16 or height < 16:
            return b""
        face = image.crop(_FACE)
        hat = image.crop(_HAT)
        face.alpha_composite(hat)
        face = face.resize((size, size), Image.NEAREST)
        out = io.BytesIO()
        face.save(out, "PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 - 头像坏了不影响功能
        return b""
