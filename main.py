"""OffCraft 入口：无子命令时打开图形界面，否则交给 CLI。"""

from __future__ import annotations

import sys

from offcraft.cli import main

_COMMANDS = {"list", "install", "check", "instances", "launch", "pack", "gui"}

if __name__ == "__main__":
    if not any(arg in _COMMANDS for arg in sys.argv[1:]):
        sys.argv.append("gui")
    raise SystemExit(main())
