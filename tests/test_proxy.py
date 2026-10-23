"""镜像服务 HTTP 行为：缓存、单飞、容量限制与并发上限。

用一个本地假上游充当 Mojang / Modrinth，避免测试依赖外网。
"""

import http.client
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from server_mirror.config import Settings
from server_mirror.proxy import MirrorHTTPServer

PAYLOAD = bytes(range(256)) * 16  # 4 KiB


class _Upstream(BaseHTTPRequestHandler):
    payload = PAYLOAD
    delay = 0.0
    hits: list[str] = []
    lock = threading.Lock()

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        cls = type(self)
        with cls.lock:
            cls.hits.append(self.path)
        if cls.delay:
            time.sleep(cls.delay)
        body = cls.payload
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass


class _Mirror:
    """起一台真实镜像服务（监听随机端口），并提供一个取数的小工具。"""

    def __init__(self, cache_dir: Path, **settings) -> None:
        handler = type(
            "Upstream",
            (_Upstream,),
            {"hits": [], "lock": threading.Lock(), "delay": settings.pop("upstream_delay", 0.0)},
        )
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.upstream_base = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        config = Settings(
            listen="127.0.0.1:0",
            cache_dir=cache_dir,
            upstreams={"libs": self.upstream_base},
            **settings,
        )
        self.server = MirrorHTTPServer(("127.0.0.1", 0), config)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def cache(self):
        return self.server.cache

    def upstream_hits(self) -> list[str]:
        return self.upstream.RequestHandlerClass.hits

    def get(self, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def close(self) -> None:
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()


class MirrorProxyTest(unittest.TestCase):
    def test_cache_hit_serves_from_disk(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _Mirror(Path(tmp))
            try:
                status, body = mirror.get("/libs/a.jar")
                self.assertEqual(status, 200)
                self.assertEqual(body, PAYLOAD)
                status, body = mirror.get("/libs/a.jar")
                self.assertEqual((status, body), (200, PAYLOAD))
                self.assertEqual(len(mirror.upstream_hits()), 1, "第二个请求应命中缓存")
                self.assertGreaterEqual(mirror.server.metrics.snapshot()["hits"], 1)
                self.assertTrue((Path(tmp) / "libs" / "a.jar").is_file())
            finally:
                mirror.close()

    def test_single_flight_dedupes_concurrent_cold_requests(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _Mirror(Path(tmp), upstream_delay=0.4, flight_wait=20.0)
            try:
                results: list[tuple[int, bytes]] = []
                lock = threading.Lock()
                start = threading.Barrier(6)

                def worker() -> None:
                    start.wait(timeout=10)
                    item = mirror.get("/libs/shared.jar")
                    with lock:
                        results.append(item)

                threads = [threading.Thread(target=worker) for _ in range(6)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertEqual(len(results), 6)
                for status, body in results:
                    self.assertEqual(status, 200)
                    self.assertEqual(body, PAYLOAD, "并发请求必须都拿到完整内容")
                self.assertEqual(len(mirror.upstream_hits()), 1, "同一键只能回源一次")
                cached = Path(tmp) / "libs" / "shared.jar"
                self.assertEqual(cached.read_bytes(), PAYLOAD, "缓存文件必须完整")
            finally:
                mirror.close()

    def test_oversized_file_streams_without_caching(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _Mirror(Path(tmp), max_file_bytes=1024)
            try:
                status, body = mirror.get("/libs/big.jar")
                self.assertEqual((status, body), (200, PAYLOAD))
                self.assertFalse((Path(tmp) / "libs" / "big.jar").exists())
                mirror.get("/libs/big.jar")
                self.assertEqual(len(mirror.upstream_hits()), 2, "未缓存的资源每次都回源")
            finally:
                mirror.close()

    def test_capacity_eviction_over_http(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _Mirror(Path(tmp), max_cache_bytes=6000)
            try:
                for name in ("a", "b", "c"):
                    self.assertEqual(mirror.get(f"/libs/{name}.jar")[0], 200)
                total = mirror.server.cache.total_bytes(refresh=True)
                self.assertLessEqual(total, 6000, "缓存总量必须受 MIRROR_MAX_CACHE_BYTES 约束")
                self.assertGreater(mirror.server.cache.evicted_bytes, 0)
            finally:
                mirror.close()

    def test_upstream_concurrency_limit_returns_503(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _Mirror(
                Path(tmp), max_upstreams=1, upstream_wait=0.2, upstream_delay=0.6
            )
            try:
                codes: list[int] = []
                lock = threading.Lock()
                start = threading.Barrier(3)

                def worker(name: str) -> None:
                    start.wait(timeout=10)
                    status, _body = mirror.get(f"/libs/{name}.jar")
                    with lock:
                        codes.append(status)

                threads = [
                    threading.Thread(target=worker, args=(name,))
                    for name in ("one", "two", "three")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertEqual(len(codes), 3)
                self.assertIn(503, codes, "超出并发上限的请求应快速失败")
                self.assertIn(200, codes)
            finally:
                mirror.close()

    def test_health_reports_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            mirror = _Mirror(Path(tmp), max_cache_bytes=100000)
            try:
                mirror.get("/libs/a.jar")
                conn = http.client.HTTPConnection("127.0.0.1", mirror.port, timeout=10)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
                import json

                data = json.loads(body)
                self.assertEqual(data["status"], "ok")
                self.assertEqual(data["cache"]["max_bytes"], 100000)
                self.assertGreaterEqual(data["cache"]["bytes"], len(PAYLOAD))
            finally:
                mirror.close()


if __name__ == "__main__":
    unittest.main()
