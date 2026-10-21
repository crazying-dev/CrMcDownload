"""并发下载器：断点续传、SHA-1 校验、重试、取消与进度聚合。"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from offcraft import DEFAULT_USER_AGENT
from offcraft.net.proxy import ProxyConfig, open_tunnel, parse_proxy

__all__ = [
    "Cancelled",
    "DownloadError",
    "DownloadReport",
    "DownloadTask",
    "Downloader",
    "ProgressTracker",
    "sha1_file",
    "verify_file",
]

_CHUNK = 256 * 1024


class DownloadError(RuntimeError):
    """下载或校验失败。"""


class Cancelled(RuntimeError):
    """用户取消。"""


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK * 4), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, sha1: str | None, size: int | None) -> bool:
    """文件存在、大小与 SHA-1 均匹配时返回 True。"""
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file():
        return False
    if size is not None and stat.st_size != size:
        return False
    if sha1 and sha1_file(path).lower() != sha1.lower():
        return False
    return True


@dataclass(frozen=True, slots=True)
class DownloadTask:
    url: str
    dest: Path
    sha1: str | None = None
    size: int | None = None
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.dest.name


@dataclass(slots=True)
class ProgressTracker:
    """线程安全的进度聚合。"""

    total_bytes: int = 0
    done_bytes: int = 0
    files_total: int = 0
    files_done: int = 0
    current: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _samples: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=16), repr=False)

    def start(self, files_total: int, total_bytes: int) -> None:
        with self._lock:
            self.files_total = files_total
            self.total_bytes = max(total_bytes, 0)
            self.done_bytes = 0
            self.files_done = 0
            self._samples.clear()
            self._samples.append((time.monotonic(), 0))

    def add_bytes(self, count: int) -> None:
        with self._lock:
            self.done_bytes += count
            now = time.monotonic()
            self._samples.append((now, self.done_bytes))
            while len(self._samples) > 2 and now - self._samples[0][0] > 3.0:
                self._samples.popleft()

    def file_started(self, name: str) -> None:
        with self._lock:
            self.current = name

    def file_finished(self) -> None:
        with self._lock:
            self.files_done += 1

    @property
    def speed(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
            if t1 <= t0:
                return 0.0
            return (b1 - b0) / (t1 - t0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            done, total = self.done_bytes, self.total_bytes
            files_done, files_total = self.files_done, self.files_total
            current = self.current
        speed = self.speed
        remaining = max(total - done, 0) if total else None
        eta = remaining / speed if remaining is not None and speed > 0 else None
        return {
            "done_bytes": done,
            "total_bytes": total,
            "files_done": files_done,
            "files_total": files_total,
            "current": current,
            "speed": speed,
            "eta": eta,
            "percent": (done / total * 100.0) if total else None,
        }


@dataclass(slots=True)
class DownloadReport:
    succeeded: list[DownloadTask] = field(default_factory=list)
    skipped: list[DownloadTask] = field(default_factory=list)
    failed: list[tuple[DownloadTask, str]] = field(default_factory=list)
    bytes_written: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed


class Downloader:
    """基于标准库的并发下载器。

    ``progress`` 每完成一块数据会被调用（默认每 256 KiB）。
    """

    def __init__(
        self,
        *,
        proxy: str | ProxyConfig | None = None,
        token: str | None = None,
        workers: int = 8,
        timeout: float = 30.0,
        retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        progress_callback: Callable[[ProgressTracker], None] | None = None,
        progress_interval: float = 0.2,
    ) -> None:
        self.proxy = parse_proxy(proxy) if isinstance(proxy, str) else proxy
        self.token = token or None
        self.workers = max(1, int(workers))
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self.user_agent = user_agent
        self.progress_callback = progress_callback
        self.progress_interval = progress_interval
        self._ssl = ssl.create_default_context()
        self._last_progress = 0.0

    # ------------------------------------------------------------------ HTTP
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if self.token:
            headers["X-OffCraft-Token"] = self.token
        if extra:
            headers.update(extra)
        return headers

    def _open(self, url: str, extra_headers: dict[str, str] | None = None, method: str = "GET"):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise DownloadError(f"非法 URL：{url}")
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        raw = open_tunnel(host, port, self.proxy, self.timeout)
        if parts.scheme == "https":
            raw = self._ssl.wrap_socket(raw, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        conn.sock = raw
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn.request(method, path, headers=self._headers(extra_headers))
        return conn, conn.getresponse()

    def _request_with_retry(
        self,
        url: str,
        extra_headers: dict[str, str] | None = None,
        method: str = "GET",
        *,
        attempts: int | None = None,
    ):
        attempts = self.retries + 1 if attempts is None else attempts
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._open(url, extra_headers, method)
            except (OSError, http.client.HTTPException) as exc:
                last = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt * 0.5, 4.0))
        raise DownloadError(f"请求失败：{url}（{last}）") from last

    def fetch_bytes(self, url: str, *, max_size: int = 64 * 1024 * 1024) -> bytes:
        conn, resp = self._request_with_retry(url)
        try:
            if resp.status != 200:
                raise DownloadError(f"HTTP {resp.status}：{url}")
            data = resp.read(max_size + 1)
            if len(data) > max_size:
                raise DownloadError(f"响应超过 {max_size} 字节：{url}")
            return data
        finally:
            conn.close()

    def fetch_json(self, url: str) -> Any:
        raw = self.fetch_bytes(url)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DownloadError(f"JSON 解析失败：{url}（{exc}）") from exc

    # -------------------------------------------------------------- download
    def download(
        self,
        task: DownloadTask,
        tracker: ProgressTracker | None = None,
        cancel: threading.Event | None = None,
    ) -> bool:
        """下载单个文件；已存在且校验通过返回 ``False``（跳过）。"""
        dest = Path(task.dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if verify_file(dest, task.sha1, task.size):
            return False
        part = dest.with_name(dest.name + ".part")
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                written = self._download_attempt(task, part, tracker, cancel)
                if task.size is not None:
                    actual = part.stat().st_size
                    if actual != task.size:
                        raise DownloadError(f"大小不符：期望 {task.size}，实际 {actual}")
                if task.sha1 and sha1_file(part).lower() != task.sha1.lower():
                    raise DownloadError("SHA-1 校验失败")
                os.replace(part, dest)
                if tracker is None and written:
                    pass
                return True
            except Cancelled:
                raise
            except (OSError, http.client.HTTPException, DownloadError) as exc:
                last_error = exc
                if part.exists():
                    part.unlink(missing_ok=True)
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt * 0.5, 4.0))
        raise DownloadError(f"下载失败：{task.name}（{last_error}）") from last_error

    def _download_attempt(
        self,
        task: DownloadTask,
        part: Path,
        tracker: ProgressTracker | None,
        cancel: threading.Event | None,
    ) -> int:
        offset = part.stat().st_size if part.exists() else 0
        headers: dict[str, str] = {}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        conn, resp = self._request_with_retry(task.url, headers)
        written = 0
        try:
            if resp.status == 416:
                part.unlink(missing_ok=True)
                raise DownloadError("断点位置无效，已重置")
            if resp.status == 206 and offset:
                mode = "ab"
                expected = _content_range_total(resp) or task.size
            elif resp.status == 200:
                mode = "wb"
                offset = 0
                expected = task.size
            else:
                raise DownloadError(f"HTTP {resp.status}：{task.url}")
            if expected and task.size and expected != task.size:
                raise DownloadError(f"服务端大小 {expected} 与预期 {task.size} 不符")
            # 直接写 .part：续传时前缀已在该文件中，不能再另起临时文件
            with part.open(mode) as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if tracker is not None:
                        tracker.add_bytes(len(chunk))
                        self._emit_progress(tracker)
            return written
        finally:
            conn.close()

    def _emit_progress(self, tracker: ProgressTracker) -> None:
        if self.progress_callback is None:
            return
        now = time.monotonic()
        if now - self._last_progress >= self.progress_interval:
            self._last_progress = now
            self.progress_callback(tracker)

    # ------------------------------------------------------------------ bulk
    def run(
        self,
        tasks: Iterable[DownloadTask],
        *,
        tracker: ProgressTracker | None = None,
        cancel: threading.Event | None = None,
    ) -> DownloadReport:
        tasks = [t for t in tasks]
        report = DownloadReport()
        tracker = tracker or ProgressTracker()
        tracker.start(len(tasks), sum(t.size or 0 for t in tasks))

        pending: list[DownloadTask] = []
        for task in tasks:
            if verify_file(Path(task.dest), task.sha1, task.size):
                report.skipped.append(task)
                tracker.file_finished()
            else:
                pending.append(task)

        if not pending:
            return report

        errors: dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=min(self.workers, len(pending))) as pool:
            futures: dict[Future[bool], DownloadTask] = {}
            for task in pending:
                if cancel is not None and cancel.is_set():
                    report.failed.append((task, "已取消"))
                    continue
                tracker.file_started(task.name)
                future = pool.submit(self.download, task, tracker, cancel)
                futures[future] = task
            for future in as_completed(futures):
                task = futures[future]
                try:
                    future.result()
                    report.succeeded.append(task)
                    tracker.file_finished()
                except Cancelled:
                    report.failed.append((task, "已取消"))
                except Exception as exc:  # noqa: BLE001 - 汇总给 UI
                    report.failed.append((task, str(exc)))
                self._emit_progress(tracker)
        if errors:
            report.failed.extend((pending[i], msg) for i, msg in errors.items())
        report.bytes_written = tracker.done_bytes
        return report


def _content_range_total(resp) -> int | None:
    header = resp.getheader("Content-Range") or ""
    if "/" not in header:
        return None
    total = header.rsplit("/", 1)[1].strip()
    try:
        return int(total)
    except ValueError:
        return None
