"""单飞（single-flight）：同一个缓存键的并发回源只让一个请求真正下载。

对应 Issue #1 P2“并发回源争用临时文件”。除了不再混淆临时文件，
它也能避免 N 个客户端同时把一个 500 MB 的整合包从上游拉 N 遍。
"""

from __future__ import annotations

import threading

__all__ = ["SingleFlight"]


class SingleFlight:
    """按 key 去重。

    >>> flight = SingleFlight()
    >>> event, owner = flight.begin("libs/a.jar")
    >>> owner
    True
    >>> event2, owner2 = flight.begin("libs/a.jar")
    >>> owner2
    False
    >>> flight.end("libs/a.jar")
    >>> event.is_set()
    True
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}

    def begin(self, key: str) -> tuple[threading.Event, bool]:
        """返回 ``(event, is_owner)``；非 owner 拿到的是别人的 event。"""
        with self._lock:
            existing = self._events.get(key)
            if existing is not None:
                return existing, False
            event = threading.Event()
            self._events[key] = event
            return event, True

    def end(self, key: str) -> None:
        """leader 结束时唤醒所有等待者。"""
        with self._lock:
            event = self._events.pop(key, None)
        if event is not None:
            event.set()

    def active(self) -> int:
        with self._lock:
            return len(self._events)
