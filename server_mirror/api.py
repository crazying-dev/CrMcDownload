"""``/api`` + ``/skin`` + ``/page`` 分流：把联机榜单、统计看板与皮肤接到 HTTP 层。

约定（与 ``proxy.py`` 协商）：本路由只在 ``_dispatch`` 里按前缀被调用，返回
``(status, headers_dict, body_bytes)``，由 handler 统一写回。读接口免令牌，
写接口（``POST /api/servers/report``、``POST /skin/*``）在没有配置令牌时放行、
配置了就必须带 ``X-OffCraft-Token``。响应统一 UTF-8 JSON，读接口带 15s 短缓存。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from server_mirror.db import Database
from server_mirror.skins import SkinError, SkinStore
from server_mirror.ssrf_guard import ServerAddressError, parse_host_port

__all__ = ["ApiRouter"]

_READ_CACHE = {"Cache-Control": "public, max-age=15"}


def _json(status: int, payload: Any, *, extra_headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    headers.update(extra_headers or {})
    return status, headers, body


def _err(status: int, message: str) -> tuple[int, dict[str, str], bytes]:
    return _json(status, {"error": message})


@dataclass(slots=True)
class ApiRouter:
    """聚合各子系统的只读/写入接口。"""

    settings: Any
    db: Database
    skins: SkinStore | None = None
    page: Any = None                       # StaticHandler
    metrics_snapshot: Callable[[], dict] | None = None
    cache_stats: Callable[[], dict] | None = None
    probe_status: Callable[[], dict] | None = None
    _report_buckets: dict[str, list[float]] = field(default_factory=dict)

    # -------------------------------------------------------------- 可用性
    def handles(self, path: str) -> bool:
        clean = path.split("?", 1)[0]
        return (
            clean.startswith("/api/")
            or clean.startswith("/skin/")
            or clean == "/page"
            or clean.startswith("/page/")
        )

    def _authorized(self, headers: dict[str, str]) -> bool:
        token = getattr(self.settings, "token", "") or ""
        if not token:
            return True
        got = headers.get("x-offcraft-token") or headers.get("X-OffCraft-Token") or ""
        return got == token

    # -------------------------------------------------------------- 分发
    def dispatch(
        self, method: str, target: str, headers: dict[str, str], body: bytes | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        raw_path, _, query = target.partition("?")
        path = unquote(raw_path)
        params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=False).items()}
        if path == "/page" or path.startswith("/page/"):
            return self._serve_page(path)
        if path.startswith("/skin/"):
            return self._serve_skin(method, path, params, headers, body)
        if path.startswith("/api/"):
            return self._serve_api(method, path, params, headers, body)
        return _err(404, "未知接口")

    def _serve_page(self, path: str):
        if self.page is None or not getattr(self.page, "available", False):
            return _err(404, "看板未构建")
        return self.page.serve(path)

    # --------------------------------------------------------------- /api
    def _serve_api(self, method: str, path: str, params: dict[str, str], headers: dict[str, str], body):
        if method not in ("GET", "HEAD"):
            if path == "/api/servers/report" and method == "POST":
                return self._report(body, headers)
            return _err(405, "方法不允许")
        if path == "/api/servers/list":
            return self._list(params)
        if path == "/api/servers/detail":
            return self._detail(params)
        if path == "/api/servers/status":
            return _json(200, self._servers_status(), extra_headers=_READ_CACHE)
        if path == "/api/skins/status":
            payload = self.skins.status() if self.skins else {}
            return _json(200, payload, extra_headers=_READ_CACHE)
        if path.startswith("/api/stats/"):
            return self._stats(path.rsplit("/", 1)[1])
        return _err(404, "未知接口")

    def _list(self, params: dict[str, str]):
        q = str(params.get("q") or "")[:64]
        limit = _clamp(params.get("limit"), 1, 50, 20)
        offset = _clamp(params.get("offset"), 0, 1_000_000, 0)
        rows, total = self.db.rank(q, limit=limit, offset=offset)
        return _json(
            200,
            {"total": total, "limit": limit, "offset": offset, "hits": [_listing(r) for r in rows]},
            extra_headers=_READ_CACHE,
        )

    def _detail(self, params: dict[str, str]):
        host = str(params.get("host") or "")
        if not host:
            return _err(400, "缺少 host")
        try:
            host, port = parse_host_port(f"{host}:{params.get('port') or 25565}")
        except ServerAddressError as exc:
            return _err(400, str(exc))
        item = self.db.detail(host, port)
        if item is None:
            return _err(404, "没有该服务器的记录")
        return _json(200, _listing(item, detail=True), extra_headers=_READ_CACHE)

    def _report(self, body, headers: dict[str, str]):
        if not self._authorized(headers):
            return _err(403, "需要访问令牌")
        payload = _decode_json(body)
        if payload is None:
            return _err(400, "请求体不是合法 JSON")
        host = str(payload.get("host") or "")
        try:
            host, port = parse_host_port(f"{host}:{payload.get('port') or 25565}")
        except ServerAddressError as exc:
            return _err(400, str(exc))
        label = str(payload.get("label") or "")[:40]
        if not self._allow_report(host):
            return _err(429, "上报过于频繁，请稍后再试")
        self.db.report(host, port, label=label)
        return _json(200, {"ok": True})

    def _allow_report(self, host: str) -> bool:
        """按主机限流：令牌桶，每秒补 1、容量 10。"""
        import time

        now = time.time()
        bucket = self._report_buckets.get(host, [10.0, now])
        tokens = min(10.0, bucket[0] + (now - bucket[1]))
        if tokens < 1:
            self._report_buckets[host] = [tokens, now]
            return False
        self._report_buckets[host] = [tokens - 1, now]
        return True

    def _servers_status(self):
        status = {"servers": self.db.overview()}
        if self.probe_status is not None:
            status["probe"] = self.probe_status()
        return status

    def _stats(self, name: str):
        if name == "overview":
            snap = self.metrics_snapshot() if self.metrics_snapshot else {}
            return _json(200, {"metrics": snap, "servers": self.db.overview()}, extra_headers=_READ_CACHE)
        if name == "cache":
            return _json(200, self.cache_stats() if self.cache_stats else {}, extra_headers=_READ_CACHE)
        if name == "download":
            snap = self.metrics_snapshot() if self.metrics_snapshot else {}
            return _json(200, {"metrics": snap}, extra_headers=_READ_CACHE)
        if name == "servers":
            return _json(200, self.db.overview(), extra_headers=_READ_CACHE)
        return _err(404, "未知统计项")

    # -------------------------------------------------------------- /skin
    def _serve_skin(self, method: str, path: str, params: dict[str, str], headers: dict[str, str], body):
        if self.skins is None:
            return _err(404, "皮肤服务未启用")
        name = _skin_name(path)
        model = str(params.get("model") or "").lower()
        slim = model == "slim" or name.endswith("_slim")
        if method in ("GET", "HEAD"):
            data = self.skins.read(name, slim=slim)
            if data is None:
                return _err(404, "没有该皮肤")
            return 200, {"Content-Type": "image/png", "Cache-Control": "public, max-age=300"}, data
        if method == "POST":
            if not self._authorized(headers):
                return _err(403, "需要访问令牌")
            try:
                used, width, height = self.skins.write(name, body or b"", model=model or "auto")
            except SkinError as exc:
                return _err(400, str(exc))
            return _json(200, {"ok": True, "model": used, "width": width, "height": height})
        return _err(405, "方法不允许")


# ---------------------------------------------------------------- 工具
def _skin_name(path: str) -> str:
    tail = path[len("/skin/"):]
    if tail.endswith(".png"):
        tail = tail[: -len(".png")]
    if "/" in tail or ".." in tail:
        raise SkinError("非法皮肤路径")
    return tail


def _listing(item: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    players = item.get("players") or []
    out = {
        "host": item["host"],
        "port": item["port"],
        "label": item.get("label") or "",
        "source": item.get("source") or "api",
        "reports": int(item.get("reports") or 0),
        "ok": bool(item.get("ok")),
        "error": item.get("error") or "",
        "latency_ms": item.get("latency_ms"),
        "motd": item.get("motd") or "",
        "version": item.get("version") or "",
        "online": int(item.get("online") or 0),
        "max": int(item.get("players_max") or 0),
        "players": players,
        "players_visible": bool(players),
        "updated_at": float(item.get("updated_at") or 0.0),
        "hot": round(float(item.get("hot") or 0.0), 3),
    }
    if detail:
        out["history"] = item.get("history") or []
    return out


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _decode_json(body: bytes | None) -> dict[str, Any] | None:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
