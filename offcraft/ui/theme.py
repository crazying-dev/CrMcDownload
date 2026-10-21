"""深色主题与 ttk 样式。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

__all__ = ["COLORS", "FONTS", "apply_theme"]

COLORS = {
    "bg": "#15161a",
    "panel": "#1d1f25",
    "card": "#24272f",
    "border": "#32363f",
    "text": "#e8eaed",
    "muted": "#9aa0a6",
    "accent": "#5b8def",
    "accent_hover": "#6f9bf2",
    "ok": "#5cc46b",
    "warn": "#e0b341",
    "error": "#e06c6c",
    "selection": "#2f3b55",
}

FONTS = {
    "base": ("Microsoft YaHei UI", 10),
    "small": ("Microsoft YaHei UI", 9),
    "bold": ("Microsoft YaHei UI", 10, "bold"),
    "title": ("Microsoft YaHei UI", 18, "bold"),
    "heading": ("Microsoft YaHei UI", 12, "bold"),
    "mono": ("Consolas", 9),
}


def apply_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(bg=COLORS["bg"])
    style.configure(".", background=COLORS["bg"], foreground=COLORS["text"], font=FONTS["base"])
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Panel.TFrame", background=COLORS["panel"])
    style.configure("Card.TFrame", background=COLORS["card"])

    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", foreground=COLORS["muted"], background=COLORS["bg"])
    style.configure("Title.TLabel", font=FONTS["title"], foreground=COLORS["text"])
    style.configure("Heading.TLabel", font=FONTS["heading"], background=COLORS["bg"])
    style.configure("Accent.TLabel", foreground=COLORS["accent"], font=FONTS["bold"])
    style.configure("Card.TLabel", background=COLORS["card"], foreground=COLORS["text"])
    style.configure("CardMuted.TLabel", background=COLORS["card"], foreground=COLORS["muted"])

    style.configure(
        "TButton",
        background=COLORS["card"],
        foreground=COLORS["text"],
        borderwidth=0,
        focusthickness=0,
        padding=(14, 7),
    )
    style.map(
        "TButton",
        background=[("active", COLORS["border"]), ("disabled", COLORS["panel"])],
        foreground=[("disabled", COLORS["muted"])],
    )
    style.configure(
        "Accent.TButton",
        background=COLORS["accent"],
        foreground="#ffffff",
        font=FONTS["bold"],
        padding=(18, 9),
    )
    style.map("Accent.TButton", background=[("active", COLORS["accent_hover"])])

    style.configure("TEntry", fieldbackground=COLORS["card"], foreground=COLORS["text"], borderwidth=0)
    style.configure("TSpinbox", fieldbackground=COLORS["card"], foreground=COLORS["text"], borderwidth=0)
    style.configure("TCheckbutton", background=COLORS["bg"], foreground=COLORS["text"])
    style.map("TCheckbutton", background=[("active", COLORS["bg"])])

    style.configure(
        "Treeview",
        background=COLORS["panel"],
        fieldbackground=COLORS["panel"],
        foreground=COLORS["text"],
        borderwidth=0,
        rowheight=30,
    )
    style.map(
        "Treeview",
        background=[("selected", COLORS["selection"])],
        foreground=[("selected", COLORS["text"])],
    )
    style.configure("Treeview.Heading", background=COLORS["card"], foreground=COLORS["muted"], relief="flat")
    style.map("Treeview.Heading", background=[("active", COLORS["border"])])

    style.configure(
        "TNotebook", background=COLORS["bg"], borderwidth=0, tabmargins=(0, 4, 0, 0)
    )
    style.configure(
        "TNotebook.Tab",
        background=COLORS["bg"],
        foreground=COLORS["muted"],
        padding=(18, 8),
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", COLORS["panel"])],
        foreground=[("selected", COLORS["text"])],
    )

    style.configure(
        "Horizontal.TProgressbar",
        background=COLORS["accent"],
        troughcolor=COLORS["card"],
        borderwidth=0,
        thickness=8,
    )
    style.configure("TScrollbar", background=COLORS["card"], troughcolor=COLORS["bg"], borderwidth=0)
    return style
