"""OffCraft 入口：无参数时打开图形界面，否则交给 CLI。"""

from __future__ import annotations

import sys

from offcraft.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("gui")
    raise SystemExit(main())
