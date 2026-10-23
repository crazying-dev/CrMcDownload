"""命令行入口：下载、安装、检查、离线包与本地实例。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from offcraft.config import Config, load_config, save_config
from offcraft.install.installer import install, prepare_install
from offcraft.install.plan import InstallOptions
from offcraft.instances import discover_instances, instance_game_dir
from offcraft.meta.manifest import VersionManifest
from offcraft.mirror import DirectSource, Mirror
from offcraft.net.downloader import Downloader, ProgressTracker
from offcraft.pack.offline import export_pack, import_pack

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
DEFAULT_GAME_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / ".minecraft"


def _game_dir(args, config: Config) -> Path:
    return Path(args.dir or config.game_dir or DEFAULT_GAME_DIR)


def _source(args, config: Config):
    base = (getattr(args, "base", None) or config.download_base or "").strip()
    if base:
        return Mirror(base)
    print("提示：未配置下载源，使用官方直连（仅限可直连 Mojang 的网络）", file=sys.stderr)
    return DirectSource()


def _downloader(args, config: Config) -> Downloader:
    return Downloader(
        proxy=getattr(args, "proxy", None) or config.proxy or None,
        token=getattr(args, "token", None) or config.download_token or None,
        workers=int(getattr(args, "workers", None) or config.max_workers),
        timeout=30,
        retries=3,
    )


def _manifest(downloader: Downloader, source) -> VersionManifest:
    return VersionManifest.from_json(downloader.fetch_json(source.rewrite(MANIFEST_URL)))


class _Progress:
    def __init__(self) -> None:
        self.last = 0.0

    def __call__(self, tracker: ProgressTracker) -> None:
        now = time.monotonic()
        if now - self.last < 0.5:
            return
        self.last = now
        snap = tracker.snapshot()
        pct = f"{snap['percent']:5.1f}%" if snap["percent"] is not None else "  ?  "
        speed = snap["speed"] / 1048576
        text = (
            f"\r{pct} {snap['done_bytes'] / 1048576:7.1f} MiB "
            f"{speed:5.2f} MiB/s {snap['files_done']}/{snap['files_total']} "
            f"{snap['current'][:40]:40}"
        )
        print(text, end="", file=sys.stderr, flush=True)


def cmd_list(args, config: Config) -> int:
    source = _source(args, config)
    manifest = _manifest(_downloader(args, config), source)
    kinds = {"release"} if not args.snapshots else {"release", "snapshot", "old_beta", "old_alpha"}
    versions = manifest.filter(kinds, args.search or "")
    if args.limit:
        versions = versions[: args.limit]
    for item in versions:
        marker = " *" if item.id == manifest.latest_release else "  "
        print(f"{marker} {item.id:28} {item.label:10} {item.release_time[:10]}")
    print(f"共 {len(versions)} 个版本（源：{source.base_url}）", file=sys.stderr)
    return 0


def cmd_install(args, config: Config) -> int:
    source = _source(args, config)
    downloader = _downloader(args, config)
    manifest = _manifest(downloader, source)
    options = InstallOptions(
        include_assets=not (args.skip_assets or (config.skip_assets and not args.with_assets))
    )
    plan = prepare_install(
        _game_dir(args, config), args.version, manifest, source, downloader, options=options
    )
    print(f"计划：{plan.summary()}", file=sys.stderr)
    for warning in plan.warnings:
        print(f"警告：{warning}", file=sys.stderr)
    downloader.progress_callback = _Progress()
    result = install(plan, downloader)
    print(file=sys.stderr)
    if not result.ok:
        for task, error in result.report.failed[:10]:
            print(f"失败：{task.name}: {error}", file=sys.stderr)
        return 1
    config.last_version = args.version
    save_config(config, args.data_dir)
    print(
        f"完成：新增 {len(result.report.succeeded)} 个文件，"
        f"跳过 {len(result.report.skipped)} 个，用时 {result.duration:.1f}s",
        file=sys.stderr,
    )
    return 0


def cmd_check(args, config: Config) -> int:
    from offcraft.meta.version import VersionError, load_version

    mc_dir = _game_dir(args, config)
    try:
        merged = load_version(mc_dir, args.version)
    except VersionError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"{args.version}: {merged.main_class} java={merged.java_major or '?'} 库={len(merged.libraries)}")
    print(f"目录：{mc_dir}")
    jar = mc_dir / "versions" / merged.jar_id / f"{merged.jar_id}.jar"
    print(f"client jar：{'存在' if jar.is_file() else '缺失'} ({jar})")
    missing = 0
    from offcraft.meta.rules import OsInfo
    from offcraft.install.plan import build_plan

    plan = build_plan(mc_dir, args.version, merged.raw, merged, DirectSource(), os_info=OsInfo())
    for task in plan.tasks:
        if not task.dest.is_file():
            missing += 1
            if missing <= 10:
                print(f"缺失：{task.dest}")
    print(f"缺失文件：{missing}/{len(plan.tasks)}")
    return 0 if missing == 0 else 1


def cmd_instances(args, config: Config) -> int:
    mc_dir = _game_dir(args, config)
    items = discover_instances(mc_dir)
    if not items:
        print(f"{mc_dir} 下没有已安装版本", file=sys.stderr)
        return 1
    for item in items:
        size = item.size_bytes / 1048576
        print(f"{item.id:34} {item.loader:9} {item.mc_version:10} java={item.java_major or '?':>2} {size:7.1f} MiB")
    return 0


def cmd_pack(args, config: Config) -> int:
    mc_dir = _game_dir(args, config)
    if args.action == "export":
        info = export_pack(mc_dir, args.version, args.file)
        print(f"已导出 {info.files} 个文件，{info.bytes / 1048576:.1f} MiB -> {args.file}")
        for warning in info.warnings:
            print(f"警告：{warning}", file=sys.stderr)
    else:
        info = import_pack(args.file, mc_dir)
        print(f"已导入 {info.files} 个文件，{info.bytes / 1048576:.1f} MiB（版本 {info.version_id}）")
    return 0


def cmd_launch(args, config: Config) -> int:
    from offcraft.launch.auth import make_profile
    from offcraft.launch.command import LaunchError, LaunchOptions, build_command
    from offcraft.launch.process import GameProcess
    from offcraft.meta.version import VersionError, load_version

    mc_dir = _game_dir(args, config)
    try:
        version = load_version(mc_dir, args.version)
        profile = make_profile(args.username or config.username)
    except (VersionError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    options = LaunchOptions(
        memory_mb=args.memory or config.memory_mb,
        game_dir=str(instance_game_dir(mc_dir, args.version, config.isolation)),
        width=config.width,
        height=config.height,
        fullscreen=config.fullscreen,
        java_path=args.java or config.java_overrides.get(args.version),
        extra_jvm=list(config.extra_jvm),
    )
    try:
        plan = build_command(version, mc_dir, profile, options=options)
    except LaunchError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    for warning in plan.warnings:
        print(f"警告：{warning}", file=sys.stderr)
    if args.dry_run:
        print(f"Java：{plan.java.display}")
        print(plan.command_line)
        return 0

    print(f"启动 {args.version}（{plan.java.display}）", file=sys.stderr, flush=True)
    exit_code: dict[str, int] = {}
    proc = GameProcess(
        plan,
        on_line=lambda line: print(line, flush=True),
        on_exit=lambda code: exit_code.setdefault("code", code),
    )
    proc.start()
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.stop()
        code = 130
    print(f"游戏进程退出，代码 {code}", file=sys.stderr)
    return code


def cmd_gui(args, config: Config) -> int:
    try:
        from offcraft.ui.app import run_app
    except ImportError as exc:
        print(f"界面模块尚未合并：{exc}\n可以先用 CLI：python -m offcraft.cli --help", file=sys.stderr)
        return 2
    run_app(config, args.data_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="offcraft", description="CrMcDownload Minecraft 启动器")
    parser.add_argument("--data-dir", default=None, help="配置目录（默认 %%APPDATA%%\\OffCraft）")
    parser.add_argument("--dir", default=None, help="游戏目录（默认配置或 %%APPDATA%%\\.minecraft）")
    parser.add_argument("--base", default=None, help="下载源地址（覆盖配置）")
    parser.add_argument("--proxy", default=None, help="代理，如 socks5h://127.0.0.1:1080")
    parser.add_argument("--token", default=None, help="镜像访问令牌")
    parser.add_argument("--workers", type=int, default=None, help="并发下载数")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出远程版本")
    p_list.add_argument("--snapshots", action="store_true", help="包含快照")
    p_list.add_argument("--search", default="", help="按版本号过滤")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.set_defaults(func=cmd_list)

    p_install = sub.add_parser("install", help="下载并安装版本")
    p_install.add_argument("version")
    p_install.add_argument("--skip-assets", action="store_true", help="跳过资源文件（更快）")
    p_install.add_argument("--with-assets", action="store_true", help="强制下载资源")
    p_install.set_defaults(func=cmd_install)

    p_check = sub.add_parser("check", help="检查本地版本完整性")
    p_check.add_argument("version")
    p_check.set_defaults(func=cmd_check)

    p_inst = sub.add_parser("instances", help="列出本地实例")
    p_inst.set_defaults(func=cmd_instances)

    p_launch = sub.add_parser("launch", help="启动已安装版本")
    p_launch.add_argument("version")
    p_launch.add_argument("--username", default=None)
    p_launch.add_argument("--memory", type=int, default=0, help="内存 MB")
    p_launch.add_argument("--java", default=None, help="指定 java.exe")
    p_launch.add_argument("--dry-run", action="store_true", help="只打印命令，不启动")
    p_launch.set_defaults(func=cmd_launch)

    p_pack = sub.add_parser("pack", help="离线包导出/导入")
    p_pack.add_argument("action", choices=["export", "import"])
    p_pack.add_argument("version", nargs="?", default="")
    p_pack.add_argument("file")
    p_pack.set_defaults(func=cmd_pack)

    p_gui = sub.add_parser("gui", help="打开图形界面")
    p_gui.set_defaults(func=cmd_gui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.data_dir)
    if args.dir:
        config.game_dir = args.dir
    if args.base:
        config.download_base = args.base
    if args.proxy:
        config.proxy = args.proxy
    if args.token:
        config.download_token = args.token
    try:
        return int(args.func(args, config))
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
