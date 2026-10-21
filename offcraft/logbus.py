"""线程安全日志总线：后台线程写入，UI 线程批量取走。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

__all__ = ["LogBus", "LogRecord"]


@dataclass(frozen=True, slots=True)
class LogRecord:
    time: float
    level: str
    text: str

    @property
    def stamp(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.time))


class LogBus:
    def __init__(self, maxlen: int = 5000) -> None:
        self._records: deque[LogRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def log(self, text: str, level: str = "INFO") -> None:
        record = LogRecord(time=time.time(), level=level.upper(), text=text)
        with self._lock:
            self._records.append(record)

    def info(self, text: str) -> None:
        self.log(text, "INFO")

    def warn(self, text: str) -> None:
        self.log(text, "WARN")

    def error(self, text: str) -> None:
        self.log(text, "ERROR")

    def drain(self) -> list[LogRecord]:
        with self._lock:
            items = list(self._records)
            self._records.clear()
            return items

    def snapshot(self) -> list[LogRecord]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
