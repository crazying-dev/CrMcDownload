"""通用界面控件。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from offcraft.logbus import LogRecord
from offcraft.ui.theme import COLORS, FONTS

__all__ = ["LogView", "format_bytes", "format_speed", "section"]


def format_bytes(value: float | int) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"


def format_speed(value: float) -> str:
    return format_bytes(value) + "/s"


class LogView(tk.Frame):
    def __init__(self, parent, **kwargs) -> None:
        super().__init__(parent, bg=COLORS["panel"], **kwargs)
        self.text = tk.Text(
            self,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            font=FONTS["mono"],
            relief="flat",
            wrap="none",
            height=10,
            padx=8,
            pady=6,
        )
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.text.tag_configure("INFO", foreground=COLORS["text"])
        self.text.tag_configure("WARN", foreground=COLORS["warn"])
        self.text.tag_configure("ERROR", foreground=COLORS["error"])
        self.text.tag_configure("SYSTEM", foreground=COLORS["accent"])
        self.text.configure(state="disabled")
        self.autoscroll = True

    def append(self, text: str, level: str = "INFO") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text + "\n", level)
        if self.autoscroll:
            self.text.see("end")
        self.text.configure(state="disabled")

    def append_record(self, record: LogRecord) -> None:
        level = record.level if record.level in ("INFO", "WARN", "ERROR", "SYSTEM") else "INFO"
        self.append(f"[{record.stamp}] {record.text}", level)

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def copy_all(self) -> str:
        return self.text.get("1.0", "end-1c")


def section(parent, title: str) -> ttk.Label:
    label = ttk.Label(parent, text=title, style="Heading.TLabel")
    return label
