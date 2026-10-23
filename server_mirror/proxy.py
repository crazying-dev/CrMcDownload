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

from server_mirror.cache import Cache, is_metadata
from server_mirror.config import Settings
from server_mirror.flight import SingleFlight
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
        self.cache = cache or Cache(
            settings.cache_dir,
            max_bytes=settings.max_cache_bytes,
            max_file_bytes=settings.max_file_bytes,
        )
        self.metrics = Metrics()
        self.flight = SingleFlight()
        self.upstream_slots = (
            threading.BoundedSemaphore(settings.max_upstreams)
            if settings.max_upstreams > 0
            else None
        )


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
            self._json(
                200,
                {
                    "status": "ok",
                    **self.server.metrics.snapshot(),
                    "cache": self.server.cache.stats(),
                },
            )
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
        immutable = not is_metadata(route.prefix, route.relative)
        ttl = self.server.settings.metadata_ttl
        if self.server.cache.is_fresh(cache_path, ttl, immutable):
            self.server.metrics.hit()
            self._serve_file(cache_path, head_only, immutable)
            return
        self.server.metrics.miss()
        # 定期回收过期元数据，避免旧索引把磁盘占死（内部有速率限制）
        self.server.cache.sweep_expired(ttl, min_interval=self.server.settings.sweep_interval)

        # Range 请求不产生完整缓存，没必要参与单飞
        if self.headers.get("Range"):
            self._proxy(route, cache_path, head_only)
            return
        event, owner = self.server.flight.begin(route.cache_key)
        if not owner:
            # 已有请求在回源：等它写完（有界），然后直接读缓存
            waited = event.wait(self.server.settings.flight_wait)
            if waited and self.server.cache.is_fresh(cache_path, ttl, immutable):
                self.server.metrics.hit()
                self._serve_file(cache_path, head_only, immutable)
                return
            # leader 失败或超时：自己回源（不占单飞名额，靠并发信号量兜底）
            self._proxy(route, cache_path, head_only)
            return
        try:
            self._proxy(route, cache_path, head_only)
        finally:
            self.server.flight.end(route.cache_key)

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
        """带并发名额的上游回源（拿不到名额返回 503，不把线程吃满）。"""
        slots = self.server.upstream_slots
        acquired = False
        if slots is not None:
            acquired = slots.acquire(timeout=self.server.settings.upstream_wait)
            if not acquired:
                self.server.metrics.error()
                self._json(503, {"error": "too many upstream requests"})
                return
        try:
            self._fetch_upstream(route, cache_path, head_only)
        finally:
            if acquired:
                slots.release()

    def _fetch_upstream(self, route: Route, cache_path: Path, head_only: bool) -> None:
        upstream_url = route.upstream_base + route.upstream_path
        headers = {"User-Agent": "OffCraft-Mirror/0.1", "Accept": "*/*"}
        range_header = self.headers.get("Range")
        if range_header:
            headers["Range"] = range_header
        conn = None
        tmp_path: Path | None = None
        fh = None
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
            immutable = not is_metadata(route.prefix, route.relative)
            self.send_header(
                "Cache-Control",
                "public, max-age=31536000, immutable" if immutable else "public, max-age=600",
            )
            self.end_headers()

            if head_only:
                return

            cache = self.server.cache
            limit = cache.max_file_bytes
            declared = _as_int(length)
            cacheable = resp.status == 200 and cache.allows(declared or 0)
            if cacheable:
                tmp_path, fh = cache.begin_write(cache_path)
            written = 0
            try:
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    written += len(chunk)
                    if fh is None:
                        continue
                    try:
                        fh.write(chunk)
                    except OSError:
                        # 磁盘写满：放弃缓存但不影响正在传输的响应
                        fh.close()
                        fh = None
                        cache.abort_write(tmp_path)  # type: ignore[arg-type]
                        tmp_path = None
                        cache.free_space()
                        continue
                    if limit and written > limit:
                        # 上游没给 Content-Length 且文件超限：停止缓存
                        fh.close()
                        fh = None
                        cache.abort_write(tmp_path)  # type: ignore[arg-type]
                        tmp_path = None
            finally:
                if fh is not None:
                    fh.close()
                fh = None
            self.server.metrics.add_upstream(written)
            if tmp_path is not None:
                if cache.allows(written):
                    try:
                        cache.commit(tmp_path, cache_path, size=written)
                    except OSError:
                        cache.abort_write(tmp_path)
                        cache.free_space()
                    else:
                        tmp_path = None
                else:
                    cache.abort_write(tmp_path)
                    tmp_path = None
        except (OSError, http.client.HTTPException) as exc:
            self.server.metrics.error()
            try:
                self._json(502, {"error": f"upstream error: {exc}"})
            except OSError:
                pass
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            if tmp_path is not None:
                self.server.cache.abort_write(tmp_path)
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


def _as_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _path_of(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return path
