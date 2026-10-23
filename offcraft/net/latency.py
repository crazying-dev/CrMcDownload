"""镜像与直连的时延探测：按路由择优。

客户端默认走自建镜像（``download_base``），但镜像并非总是更快：代理转发会
多一跳，服务器线路也可能比官方 CDN 差。本模块对**同一份资源**分别测一次
镜像与直连的首字节耗时，哪个快用哪个，并把结论按路由缓存一段时间，
避免每个文件都重新探测。

设计要点：

* 只做「二选一」，不改变镜像地址映射语义（仍是 :class:`offcraft.mirror.Mirror`）；
* 探测带 ``Range`` 限制，单次只读 2 KiB，成本极低；
* 探测失败即视为该项不可用，镜像不通时自动回到直连（反之亦然）；
* 结论缓存默认 10 分钟，并由锁保护，并发下载时同一路由只会探测一次。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit

from offcraft.mirror import HOST_ROUTES, DirectSource, Mirror, UnknownUpstreamError
from offcraft.net.downloader import DownloadError, Downloader

__all__ = [
    "DEFAULT_PROBE_TIMEOUT",
    "DEFAULT_TTL",
    "Decision",
    "SourceRouter",
    "make_source",
]

#: 探测结论的默认有效期（秒）
DEFAULT_TTL = 600.0
#: 单次探测的超时（秒）—— 宁可快失败，也不要卡住下载
DEFAULT_PROBE_TIMEOUT = 5.0
#: 探测读取的字节数
PROBE_BYTES = 2048
#: 每个候选测量次数，取最小值以抵消抖动
PROBE_SAMPLES = 2


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value * 1000:.0f}ms"


@dataclass(slots=True)
class Decision:
    """一次择优的结论。"""

    key: str
    mode: str  # "mirror" / "direct"
    mirror_sec: float | None = None
    direct_sec: float | None = None
    at: float = 0.0
    reason: str = ""

    @property
    def label(self) -> str:
        return "镜像" if self.mode == "mirror" else "直连"

    @property
    def detail(self) -> str:
        return (
            f"镜像 {_fmt_ms(self.mirror_sec)} / 直连 {_fmt_ms(self.direct_sec)}"
            f" → {self.label}"
            + (f"（{self.reason}）" if self.reason else "")
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "mode": self.mode,
            "mirror_ms": None if self.mirror_sec is None else round(self.mirror_sec * 1000, 1),
            "direct_ms": None if self.direct_sec is None else round(self.direct_sec * 1000, 1),
            "detail": self.detail,
        }


class SourceRouter:
    """在镜像与直连之间按实测时延择优。

    接口与 :class:`offcraft.mirror.Mirror` 兼容（``rewrite`` / ``rewrite_optional`` /
    ``is_mirror_url`` / ``base_url``），可以直接当作 ``mirror`` 参数传给安装计划等。
    """

    def __init__(
        self,
        mirror: Mirror,
        *,
        downloader: Downloader | None = None,
        proxy: str | None = None,
        token: str | None = None,
        ttl: float = DEFAULT_TTL,
        timeout: float = DEFAULT_PROBE_TIMEOUT,
        samples: int = PROBE_SAMPLES,
        enabled: bool = True,
    ) -> None:
        self.mirror = mirror
        self.ttl = max(0.0, float(ttl))
        self.timeout = max(0.5, float(timeout))
        self.samples = max(1, int(samples))
        self.enabled = bool(enabled)
        self._prober = downloader or Downloader(
            proxy=proxy or None,
            token=token or None,
            workers=1,
            timeout=self.timeout,
            retries=0,
        )
        self._lock = threading.RLock()
        self._cache: dict[str, Decision] = {}

    # ------------------------------------------------------------- 镜像接口
    @property
    def base_url(self) -> str:
        return self.mirror.base_url

    def is_mirror_url(self, url: str) -> bool:
        return self.mirror.is_mirror_url(url)

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.mirror.base_url

    def rewrite(self, url: str) -> str:
        """返回该资源应该使用的地址（镜像或官方）。"""
        target = self._mirrored(url)
        if target is None:
            return url
        mirrored, key = target
        if not self.enabled:
            return mirrored
        decision = self.decide(key, mirrored, url)
        return mirrored if decision.mode == "mirror" else url

    def rewrite_optional(self, url: str | None) -> str | None:
        return None if url is None else self.rewrite(url)

    # ------------------------------------------------------------- 探测
    def _mirrored(self, url: str) -> tuple[str, str] | None:
        """``url`` → (镜像地址, 路由键)；无法映射时返回 None。"""
        try:
            mirrored = self.mirror.rewrite(url)
        except UnknownUpstreamError:
            return None
        host = (urlsplit(url).hostname or "").lower()
        return mirrored, f"route:{HOST_ROUTES.get(host, host)}"

    def measure(self, url: str) -> float | None:
        """测一次首字节耗时（秒）；失败返回 None。

        成功时取多次采样的最小值以抵消抖动；一旦失败立即返回，
        避免不可达的候选把等待时间成倍拉长。
        """
        best: float | None = None
        for _ in range(self.samples):
            try:
                elapsed = self._prober.probe(url, first_chunk=PROBE_BYTES, timeout=self.timeout)
            except (DownloadError, OSError):
                break
            best = elapsed if best is None else min(best, elapsed)
        return best

    def _cached(self, key: str) -> Decision | None:
        """返回仍在有效期内的结论；``ttl=0`` 表示不缓存（每次都重新探测）。"""
        with self._lock:
            decision = self._cache.get(key)
        if decision is None or self.ttl <= 0:
            return None
        if (time.monotonic() - decision.at) < self.ttl:
            return decision
        return None

    def decide(self, key: str, mirror_url: str | None, direct_url: str | None) -> Decision:
        """比较两项并缓存结论；``mirror_url`` 为 None 时固定走直连。"""
        cached = self._cached(key)
        if cached is not None:
            return cached
        with self._lock:
            cached = self._cache.get(key)
            if (
                cached is not None
                and self.ttl > 0
                and (time.monotonic() - cached.at) < self.ttl
            ):
                return cached
            mirror_sec = self.measure(mirror_url) if mirror_url else None
            direct_sec = self.measure(direct_url) if direct_url else None
            reason = ""
            if mirror_url is None:
                mode, reason = "direct", "无镜像映射"
            elif mirror_sec is None and direct_sec is None:
                mode, reason = "direct", "两者均不可达"
            elif mirror_sec is None:
                mode, reason = "direct", "镜像不可用"
            elif direct_sec is None:
                mode, reason = "mirror", "直连不可达"
            elif mirror_sec <= direct_sec:
                mode, reason = "mirror", ""
            else:
                mode, reason = "direct", "直连更快"
            decision = Decision(
                key=key,
                mode=mode,
                mirror_sec=mirror_sec,
                direct_sec=direct_sec,
                at=time.monotonic(),
                reason=reason,
            )
            self._cache[key] = decision
            return decision

    def rank(
        self, candidates: Iterable[tuple[str, str]], key: str, probe_path: str = "/"
    ) -> list[tuple[str, str]]:
        """把 ``[(源名, 基地址), ...]`` 按实测时延排序（不可达的排最后）。

        各候选的探测地址为 ``基地址 + probe_path``，因此不同源的探测指向
        同一类资源；结论同样缓存。
        """
        items = list(candidates)
        if len(items) < 2 or not self.enabled:
            return items
        measured: list[tuple[float | None, int, tuple[str, str]]] = []
        for index, (name, base) in enumerate(items):
            cache_key = f"{key}:{name}"
            cached = self._cached(cache_key)
            if cached is not None:
                elapsed = cached.direct_sec
            else:
                elapsed = self.measure(base.rstrip("/") + probe_path)
                with self._lock:
                    self._cache[cache_key] = Decision(
                        key=cache_key,
                        mode="mirror" if name == "mirror" else "direct",
                        direct_sec=elapsed,
                        at=time.monotonic(),
                    )
            measured.append((elapsed, index, (name, base)))
        measured.sort(
            key=lambda item: (
                item[0] is None,
                item[0] if item[0] is not None else 0.0,
                item[1],
            )
        )
        return [item[2] for item in measured]

    # ------------------------------------------------------------- 观测
    def decisions(self) -> list[dict[str, object]]:
        with self._lock:
            return [item.as_dict() for item in self._cache.values()]

    def summary(self) -> str:
        """形如 ``libs: 镜像 12ms / 直连 240ms → 镜像`` 的一行描述。"""
        rows = self.decisions()
        if not rows:
            return "尚未探测"
        return "；".join(f"{row['key']}: {row['detail']}" for row in rows)

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()


def make_source(
    base: str,
    *,
    downloader: Downloader | None = None,
    proxy: str = "",
    token: str = "",
    enabled: bool = True,
    **kwargs: object,
) -> Mirror | DirectSource | SourceRouter:
    """按配置构造下载源：空地址走直连，其余交给 :class:`SourceRouter` 择优。"""
    base = (base or "").strip().rstrip("/")
    if not base:
        return DirectSource()
    try:
        mirror = Mirror(base)
    except ValueError:
        return DirectSource()
    if not enabled:
        return mirror
    return SourceRouter(mirror, downloader=downloader, proxy=proxy, token=token, **kwargs)  # type: ignore[arg-type]
