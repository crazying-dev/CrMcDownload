"""HTTP 服务：缓存优先、Range 透传、上游回源与指标。"""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from server_mirror.cache import Cache, is_metadata
from server_mirror.config import Settings
from server_mirror.netproxy import open_upstream
from server_mirror.routes import BadPath, Route, resolve

__all__ = ["Metrics", "MirrorHTTPServer", "MirrorHandler"]

_CHUNK = 256 * 1024


@dataclass
class Metrics:
    hits: int = 0
    misses: int = 0
    upstream_bytes: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def hit(self) -> None:
        with self._lock:
            self.hits += 1

    def miss(self) -> None:
        with self._lock:
            self.misses += 1

    def add_upstream(self, count: int) -> None:
        with self._lock:
            self.upstream_bytes += count

    def error(self) -> None:
        with self._lock:
            self.errors += 1

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "hits": self.hits,
                "misses": self.misses,
                "upstream_bytes": self.upstream_bytes,
                "errors": self.errors,
                "uptime": round(time.time() - self.started_at, 1),
            }


class MirrorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, settings: Settings, cache: Cache | None = None) -> None:
        super().__init__(address, MirrorHandler)
        self.settings = settings
        self.cache = cache or Cache(settings.cache_dir)
        self.metrics = Metrics()


class MirrorHandler(BaseHTTPRequestHandler):
    server: MirrorHTTPServer
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ HTTP
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head_only=True)

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401 - 静默访问日志
        if os.environ.get("MIRROR_VERBOSE"):
            super().log_message(fmt, *args)

    # -------------------------------------------------------------- routing
    def _dispatch(self, head_only: bool) -> None:
        if not self._authorized():
            self._json(403, {"error": "forbidden"})
            return
        if self.path == "/health":
            self._json(200, {"status": "ok", **self.server.metrics.snapshot()})
            return
        if self.path == "/metrics":
            self._text(200, "\n".join(f"{k} {v}" for k, v in self.server.metrics.snapshot().items()))
            return
        try:
            route = resolve(self.path, self.server.settings.upstreams)
        except BadPath as exc:
            self._json(400, {"error": str(exc)})
            return

        cache_path = self.server.cache.path_for(route.cache_key)
        immutable = not is_metadata(route.prefix)
        if self.server.cache.is_fresh(cache_path, self.server.settings.metadata_ttl, immutable):
            self.server.metrics.hit()
            self._serve_file(cache_path, head_only, immutable)
            return
        self.server.metrics.miss()
        self._proxy(route, cache_path, head_only)

    def _authorized(self) -> bool:
        token = self.server.settings.token
        if not token:
            return True
        if self.path in ("/health", "/metrics"):
            return True
        return self.headers.get("X-OffCraft-Token", "") == token

    # ----------------------------------------------------------- local file
    def _serve_file(self, path: Path, head_only: bool, immutable: bool) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self._json(404, {"error": "not found"})
            return
        etag = f'"{size:x}-{int(path.stat().st_mtime)}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return

        start, end, partial = self._parse_range(size)
        if start is None:
            self._json(416, {"error": "range not satisfiable"})
            return

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", self._content_type(path))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header(
            "Cache-Control",
            "public, max-age=31536000, immutable" if immutable else "public, max-age=600",
        )
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _parse_range(self, size: int) -> tuple[int | None, int, bool]:
        header = self.headers.get("Range")
        if not header or not header.startswith("bytes="):
            return 0, size - 1, False
        try:
            spec = header.split("=", 1)[1].split(",", 1)[0]
            start_text, _, end_text = spec.partition("-")
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
            else:
                length = int(end_text)
                start = max(0, size - length)
                end = size - 1
        except ValueError:
            return None, 0, False
        if start >= size or end < start:
            return None, 0, False
        return start, min(end, size - 1), True

    # ------------------------------------------------------------- upstream
    def _proxy(self, route: Route, cache_path: Path, head_only: bool) -> None:
        upstream_url = route.upstream_base + "/" + quote(route.relative, safe="/")
        headers = {"User-Agent": "OffCraft-Mirror/0.1", "Accept": "*/*"}
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        conn = None
        tmp_path: Path | None = None
        try:
            conn = open_upstream(upstream_url, self.server.settings.proxy, timeout=30)
            conn.request("GET", _path_of(upstream_url), headers=headers)
            resp = conn.getresponse()
            if resp.status not in (200, 206):
                body = resp.read(512).decode("utf-8", "replace")
                self.server.metrics.error()
                self._json(502, {"error": f"upstream {resp.status}: {body[:200]}"})
                return

            length = resp.getheader("Content-Length")
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type") or self._content_type(cache_path))
            if length is not None:
                self.send_header("Content-Length", length)
            self.send_header("Accept-Ranges", "bytes")
            for name in ("ETag", "Last-Modified"):
                value = resp.getheader(name)
                if value:
                    self.send_header(name, value)
            immutable = not is_metadata(route.prefix)
            self.send_header(
                "Cache-Control",
                "public, max-age=31536000, immutable" if immutable else "public, max-age=600",
            )
            self.end_headers()

            if head_only:
                return

            cacheable = resp.status == 200
            fh = None
            if cacheable:
                tmp_path, fh = self.server.cache.begin_write(cache_path)
            written = 0
            try:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    written += len(chunk)
                    if fh is not None:
                        fh.write(chunk)
            finally:
                if fh is not None:
                    fh.close()
            self.server.metrics.add_upstream(written)
            if cacheable and tmp_path is not None:
                self.server.cache.commit(tmp_path, cache_path)
                tmp_path = None
        except (OSError, http.client.HTTPException) as exc:
            self.server.metrics.error()
            try:
                self._json(502, {"error": f"upstream error: {exc}"})
            except OSError:
                pass
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            if conn is not None:
                conn.close()

    # --------------------------------------------------------------- output
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    @staticmethod
    def _content_type(path: Path) -> str:
        guessed, _ = mimetypes.guess_type(path.name)
        return guessed or "application/octet-stream"


def _path_of(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return path
