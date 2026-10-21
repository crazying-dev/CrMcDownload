"""启动子进程与日志采集。"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from offcraft.launch.command import CommandPlan

__all__ = ["GameProcess"]

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class GameProcess:
    plan: CommandPlan
    on_line: Callable[[str], None] | None = None
    on_exit: Callable[[int], None] | None = None

    _proc: subprocess.Popen | None = None
    _reader: threading.Thread | None = None
    _exit_code: int | None = None

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("进程已启动")
        self._proc = subprocess.Popen(
            self.plan.argv,
            cwd=str(self.plan.cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            creationflags=_CREATE_NO_WINDOW,
        )
        self._reader = threading.Thread(target=self._pump, name="game-log", daemon=True)
        self._reader.start()

    def _pump(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stream = self._proc.stdout
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if self.on_line is not None:
                    self.on_line(line)
        except Exception:  # noqa: BLE001 - 管道关闭
            pass
        finally:
            stream.close()
            code = self._proc.wait()
            self._exit_code = code
            if self.on_exit is not None:
                self.on_exit(code)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code if self._exit_code is not None else (
            self._proc.poll() if self._proc else None
        )

    def stop(self, timeout: float = 5.0) -> int | None:
        if self._proc is None or self._proc.poll() is not None:
            return self.exit_code
        self._proc.terminate()
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            return self._proc.wait(timeout=timeout)

    def wait(self, timeout: float | None = None) -> int:
        if self._proc is None:
            raise RuntimeError("进程未启动")
        code = self._proc.wait(timeout=timeout)
        if self._reader is not None:
            self._reader.join(timeout=5)
        return code


def tail(path, lines: int = 40) -> str:  # pragma: no cover - UI 辅助
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:])
    except OSError:
        return ""
