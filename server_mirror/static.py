"""``/page`` 静态资源：把 ``pnpm build`` 产物（``page/dist``）挂在镜像服务上。

规则：

- ``/page`` 与 ``/page/`` → ``index.html``；
- ``/page/<asset>`` → 资源文件，过路径穿越校验（解析后必须仍在 ``root`` 内）；
- 未知且非资源的 ``/page/<route>``（Vue Router history 模式）回落到 ``index.html``；
- ``dist`` 缺失时整体 404，不影响其它路由（看板是可选功能）。
- ``index.html`` 用 ``no-cache``，带指纹的构建资源用长缓存。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

__all__ = ["StaticHandler"]

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


@dataclass(slots=True)
class StaticHandler:
    """``MIRROR_PAGE_DIR`` 指向的 ``dist`` 目录只读服务。"""

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)

    @property
    def available(self) -> bool:
        return (self.root / "index.html").is_file()

    def _read(self, rel: str) -> tuple[bytes, str, bool] | None:
        """安全读取 ``root`` 下的相对路径；越界或不存在返回 ``None``。"""
        rel = unquote(rel).lstrip("/")
        if not rel or "\x00" in rel or "\\" in rel:
            return None
        candidate = (self.root / rel).resolve()
        try:
            root_resolved = self.root.resolve()
        except OSError:
            return None
        if root_resolved not in candidate.parents and candidate != root_resolved:
            return None
        if not candidate.is_file():
            return None
        try:
            data = candidate.read_bytes()
        except OSError:
            return None
        content_type = _TYPES.get(candidate.suffix.lower(), "application/octet-stream")
        html = candidate.name == "index.html" or candidate.suffix.lower() == ".html"
        return data, content_type, html

    def serve(self, path: str) -> tuple[int, dict[str, str], bytes]:
        """处理 ``/page`` 前缀之后的请求，返回 ``(status, headers, body)``。"""
        if not self.available:
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"dashboard not built"
        rel = path[len("/page"):].split("?", 1)[0]
        result = self._read(rel) if rel not in ("", "/") else None
        if result is None:
            # 资源命中不了就回落首页（history 路由）
            index = self._read("index.html")
            if index is None:
                return 404, {"Content-Type": "text/plain; charset=utf-8"}, b"not found"
            data, ctype, html = index
            headers = {"Content-Type": ctype}
            headers["Cache-Control"] = "no-cache" if html else "public, max-age=300"
            return 200, headers, data
        data, ctype, html = result
        headers = {"Content-Type": ctype}
        # index.html 不缓存；带指纹的 assets 长缓存
        headers["Cache-Control"] = "no-cache" if (html or rel.endswith("index.html")) else "public, max-age=31536000, immutable"
        return 200, headers, data
