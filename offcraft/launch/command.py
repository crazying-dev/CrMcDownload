"""启动命令拼装：占位符展开、JVM/游戏参数、Java 选择。"""

from __future__ import annotations

import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from offcraft import APP_NAME, __version__
from offcraft.launch.auth import OfflineProfile
from offcraft.launch.java import JavaCandidate, detect_java
from offcraft.launch.libraries import ClasspathPlan, build_classpath, native_jar_paths
from offcraft.launch.natives import ensure_natives, natives_directory
from offcraft.meta.rules import OsInfo, is_allowed
from offcraft.meta.version import MergedVersion

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from offcraft.account import LaunchAccount

#: 启动命令需要的账户形态：旧 OfflineProfile 或新 LaunchAccount（字段兼容）
AccountLike = "OfflineProfile | LaunchAccount"

__all__ = ["CommandPlan", "LaunchError", "LaunchOptions", "build_command", "shell_quote"]

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


class LaunchError(RuntimeError):
    """启动参数无法生成。"""


@dataclass(slots=True)
class LaunchOptions:
    memory_mb: int = 4096
    #: 游戏目录（版本隔离时指向 ``versions/<id>``）；为空则使用 mc_dir
    game_dir: str | None = None
    width: int | None = None
    height: int | None = None
    fullscreen: bool = False
    java_path: str | None = None
    extra_jvm: list[str] = field(default_factory=list)
    extra_game: list[str] = field(default_factory=list)
    launcher_name: str = APP_NAME
    launcher_version: str = __version__
    #: 「加入游戏」直连的服务器地址（>=1.20.2 走 quickPlayMultiplayer）
    quick_play_server: str | None = None
    #: 账户提供的 javaagent（外置登录用）；为 None 时不注入
    java_agent: str | None = None

    def normalized_memory(self) -> int:
        return max(512, min(65536, int(self.memory_mb)))


@dataclass(slots=True)
class CommandPlan:
    argv: list[str]
    cwd: Path
    java: JavaCandidate
    natives_dir: Path
    classpath: ClasspathPlan
    warnings: list[str] = field(default_factory=list)
    #: 各段耗时（毫秒），供启动埋点；键如 classpath / natives / java / assemble
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def command_line(self) -> str:
        return " ".join(shell_quote(part) for part in self.argv)


def shell_quote(value: str) -> str:
    if value and not re.search(r'[\s"]', value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def _flatten(items: Iterable[Any], os_info: OsInfo, features: dict[str, bool]) -> list[str]:
    result: list[str] = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
            continue
        if not isinstance(item, dict):
            continue
        if not is_allowed(item.get("rules"), os_info, features):
            continue
        value = item.get("value")
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, list):
            result.extend(str(v) for v in value)
    return result


def _expand(text: str, subs: dict[str, str], warnings: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in subs:
            return subs[key]
        warnings.append(f"未知占位符：${{{key}}}")
        return ""

    return _PLACEHOLDER.sub(replace, text)


def _substitutions(
    version: MergedVersion,
    mc_dir: Path,
    profile: "AccountLike",
    options: LaunchOptions,
    classpath: str,
    natives_dir: Path,
) -> dict[str, str]:
    separator = ";" if sys.platform == "win32" else ":"
    game_dir = Path(options.game_dir) if options.game_dir else mc_dir
    return {
        "auth_player_name": profile.name,
        "auth_uuid": profile.uuid,
        "auth_access_token": profile.access_token,
        "auth_session": profile.auth_session,
        "auth_xuid": profile.xuid,
        "clientid": "",
        "user_type": profile.user_type,
        "version_name": version.id,
        "version_type": version.type,
        "game_directory": str(game_dir),
        "assets_root": str(mc_dir / "assets"),
        "assets_index_name": version.assets,
        "natives_directory": str(natives_dir),
        "classpath": classpath,
        "classpath_separator": separator,
        "library_directory": str(mc_dir / "libraries"),
        "launcher_name": options.launcher_name,
        "launcher_version": options.launcher_version,
        "resolution_width": str(options.width or 854),
        "resolution_height": str(options.height or 480),
        "quickPlayPath": "",
        "quickPlaySingleplayer": "",
        "quickPlayMultiplayer": options.quick_play_server or "",
        "quickPlayRealms": "",
    }


def _collect_excludes(version: MergedVersion, os_info: OsInfo, features: dict[str, bool]) -> list[str]:
    excludes: list[str] = []
    for library in version.libraries:
        if not is_allowed(library.get("rules"), os_info, features):
            continue
        extract = library.get("extract") or {}
        for item in extract.get("exclude") or []:
            if item not in excludes:
                excludes.append(str(item))
    return excludes


def build_command(
    version: MergedVersion,
    mc_dir: Path | str,
    profile: "AccountLike",
    *,
    options: LaunchOptions | None = None,
    os_info: OsInfo | None = None,
    extract_natives_now: bool = True,
) -> CommandPlan:
    mc_dir = Path(mc_dir)
    options = options or LaunchOptions()
    os_info = os_info or OsInfo()

    features: dict[str, bool] = {"is_demo_user": False, "has_custom_resolution": False}
    if options.width and options.height:
        features["has_custom_resolution"] = True

    timings: dict[str, float] = {}
    # 先做纯路径解析（无 I/O），再让 classpath 校验（大量 is_file）与 natives 解压（zip 读写）
    # 两个 I/O 密集型任务在线程池里并行；两者都释放 GIL，重叠后取较慢者而非之和。
    native_jars = native_jar_paths(version, mc_dir, os_info, features)
    excludes = _collect_excludes(version, os_info, features)
    natives_dir = natives_directory(mc_dir, version.id)

    def _do_classpath() -> ClasspathPlan:
        t0 = time.perf_counter()
        plan = build_classpath(version, mc_dir, os_info, features)
        timings["classpath"] = (time.perf_counter() - t0) * 1000
        return plan

    def _do_natives() -> None:
        if not extract_natives_now:
            natives_dir.mkdir(parents=True, exist_ok=True)
            return
        t0 = time.perf_counter()
        ensure_natives(version.id, mc_dir, native_jars, excludes)
        timings["natives"] = (time.perf_counter() - t0) * 1000

    with ThreadPoolExecutor(max_workers=2) as pool:
        cp_future = pool.submit(_do_classpath)
        nv_future = pool.submit(_do_natives)
        classpath = cp_future.result()
        nv_future.result()

    if classpath.missing:
        preview = "\n".join(f"  - {path}" for path in classpath.missing[:10])
        raise LaunchError(
            f"缺少 {len(classpath.missing)} 个依赖文件，请先安装/修复该版本：\n{preview}"
        )

    component = (version.raw.get("javaVersion") or {}).get("component")
    t0 = time.perf_counter()
    try:
        java = detect_java(
            mc_dir,
            version.java_major,
            override=options.java_path,
            preferred_component=component,
        )
    except Exception as exc:  # noqa: BLE001 - 转为启动错误
        raise LaunchError(str(exc)) from exc
    timings["java"] = (time.perf_counter() - t0) * 1000
    t_asm = time.perf_counter()

    subs = _substitutions(version, mc_dir, profile, options, classpath.classpath, natives_dir)
    warnings: list[str] = list(classpath.warnings)

    jvm_items = _flatten((version.arguments or {}).get("jvm") or [], os_info, features)
    jvm_args = [f"-Xms{min(options.normalized_memory(), 1024)}M", f"-Xmx{options.normalized_memory()}M"]
    jvm_args.extend(_expand(item, subs, warnings) for item in jvm_items)

    if not any(item.startswith("-Djava.library.path=") for item in jvm_args):
        jvm_args.append(f"-Djava.library.path={natives_dir}")
    if "-cp" not in jvm_args and "-classpath" not in jvm_args:
        jvm_args.extend(["-cp", classpath.classpath])
    # 账户携带的 javaagent（外置登录）+ 额外 JVM 参数
    agent = options.java_agent or getattr(profile, "java_agent", None)
    if agent:
        jvm_args.append(f"-javaagent:{agent}")
    for extra in getattr(profile, "extra_jvm", None) or []:
        jvm_args.append(extra)
    jvm_args.extend(options.extra_jvm)

    game_args: list[str] = []
    arguments = version.arguments or {}
    if arguments.get("game"):
        game_args.extend(
            _expand(item, subs, warnings)
            for item in _flatten(arguments["game"], os_info, features)
        )
    elif version.minecraft_arguments:
        game_args.extend(
            _expand(item, subs, warnings) for item in version.minecraft_arguments.split(" ")
        )
    else:
        warnings.append("版本既没有 arguments 也没有 minecraftArguments")

    if options.fullscreen and arguments.get("game"):
        game_args.append("--fullscreen")
    game_args.extend(options.extra_game)

    game_dir = Path(options.game_dir) if options.game_dir else mc_dir
    game_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(java.path), *jvm_args, version.main_class, *game_args]
    timings["assemble"] = (time.perf_counter() - t_asm) * 1000
    return CommandPlan(
        argv=argv,
        cwd=game_dir,
        java=java,
        natives_dir=natives_dir,
        classpath=classpath,
        warnings=warnings,
        timings=timings,
    )
