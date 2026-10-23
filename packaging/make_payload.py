# -*- coding: utf-8 -*-
"""Pack dist/CrMCDownload into a flat zip (entries relative to the dist root).

Used by build_installer.ps1: the zip becomes the `payload.zip` managed
resource of the C# setup executable.
"""
import os
import sys
import zipfile

SKIP_TOP = {"uninstall.exe", "uninstall.exe.old", "uninstall.old.exe"}


def main(argv):
    if len(argv) < 3:
        print("usage: make_payload.py <dist_dir> <out_zip>")
        return 2
    src = os.path.abspath(argv[1])
    dst = os.path.abspath(argv[2])
    if not os.path.isdir(src):
        print("missing dir: %s" % src)
        return 1
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    count = 0
    total = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for base, dirs, files in os.walk(src):
            dirs.sort()
            for name in sorted(files):
                if base == src and name.lower() in SKIP_TOP:
                    continue
                full = os.path.join(base, name)
                rel = os.path.relpath(full, src).replace("\\", "/")
                zf.write(full, rel)
                count += 1
                total += os.path.getsize(full)
    print("payload entries=%d raw=%d zip=%d" % (count, total, os.path.getsize(dst)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
