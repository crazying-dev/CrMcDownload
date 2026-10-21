import hashlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from offcraft.net.downloader import (
    Cancelled,
    DownloadError,
    DownloadTask,
    Downloader,
    DownloadReport,
    ProgressTracker,
    verify_file,
)

PAYLOAD = bytes(range(256)) * 4096  # 1 MiB
PAYLOAD_SHA1 = hashlib.sha1(PAYLOAD).hexdigest()


class _Handler(BaseHTTPRequestHandler):
    payload = PAYLOAD
    hits: list[tuple[str, str | None]] = []

    def log_message(self, *args):  # noqa: D401 - 静默
        pass

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        _Handler.hits.append((self.path, self.headers.get("Range")))
        if self.path.startswith("/missing"):
            self.send_error(404)
            return
        data = self.payload
        range_header = self.headers.get("Range")
        start = 0
        if range_header and range_header.startswith("bytes="):
            start = int(range_header.split("=", 1)[1].split("-", 1)[0] or 0)
            if start >= len(data):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(data)}")
                self.end_headers()
                return
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        self.wfile.write(body)


class DownloaderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_download_and_skip(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            task = DownloadTask(f"{self.base}/a", dest, PAYLOAD_SHA1, len(PAYLOAD))
            downloader = Downloader(workers=2)
            report = downloader.run([task])
            self.assertIsInstance(report, DownloadReport)
            self.assertTrue(report.ok, report.failed)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertEqual(len(report.succeeded), 1)

            again = downloader.run([task])
            self.assertEqual(len(again.skipped), 1)
            self.assertEqual(len(again.succeeded), 0)

    def test_sha1_mismatch(self) -> None:
        with TemporaryDirectory() as tmp:
            task = DownloadTask(f"{self.base}/a", Path(tmp) / "a.bin", "0" * 40, len(PAYLOAD))
            report = Downloader(retries=0).run([task])
            self.assertFalse(report.ok)
            self.assertFalse((Path(tmp) / "a.bin").exists())

    def test_resume_from_part(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            part = dest.with_name(dest.name + ".part")
            part.write_bytes(PAYLOAD[: len(PAYLOAD) // 2])
            _Handler.hits.clear()
            task = DownloadTask(f"{self.base}/a", dest, PAYLOAD_SHA1, len(PAYLOAD))
            report = Downloader(workers=1).run([task])
            self.assertTrue(report.ok, report.failed)
            self.assertEqual(dest.read_bytes(), PAYLOAD)
            self.assertEqual(_Handler.hits[0][1], f"bytes={len(PAYLOAD) // 2}-")
            self.assertEqual(len(_Handler.hits), 1, "续传不应整包重下")

    def test_http_error_reported(self) -> None:
        with TemporaryDirectory() as tmp:
            task = DownloadTask(f"{self.base}/missing", Path(tmp) / "x.bin")
            report = Downloader(retries=0).run([task])
            self.assertFalse(report.ok)
            self.assertIn("HTTP 404", report.failed[0][1])

    def test_progress_and_cancel(self) -> None:
        with TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.bin"
            task = DownloadTask(f"{self.base}/a", dest, PAYLOAD_SHA1, len(PAYLOAD))
            cancel = threading.Event()
            tracker = ProgressTracker()

            def on_progress(state: ProgressTracker) -> None:
                if state.done_bytes > 0:
                    cancel.set()

            report = Downloader(
                workers=1, progress_callback=on_progress, progress_interval=0.0
            ).run([task], tracker=tracker, cancel=cancel)
            self.assertFalse(report.ok)
            self.assertEqual(report.failed[0][1], "已取消")
            self.assertFalse(dest.exists())

    def test_verify_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "v.bin"
            path.write_bytes(PAYLOAD)
            self.assertTrue(verify_file(path, PAYLOAD_SHA1, len(PAYLOAD)))
            self.assertFalse(verify_file(path, PAYLOAD_SHA1, len(PAYLOAD) + 1))
            self.assertFalse(verify_file(path, "0" * 40, None))

    def test_cancel_before_start(self) -> None:
        with TemporaryDirectory() as tmp:
            cancel = threading.Event()
            cancel.set()
            task = DownloadTask(f"{self.base}/a", Path(tmp) / "a.bin")
            report = Downloader().run([task], cancel=cancel)
            self.assertFalse(report.ok)
            self.assertEqual(report.failed[0][1], "已取消")


if __name__ == "__main__":
    unittest.main()
