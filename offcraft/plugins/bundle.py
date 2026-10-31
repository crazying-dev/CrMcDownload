"""已下载官方插件包的落地与完整性校验。

离线联机作为**特权官方插件**：基础安装包不附带，启用时按需下载一个 zip，解包到
``config_dir()/plugins/<id>/``，并在本地记 ``installed.json``。安全边界：

- **下载时**：zip 字节的 ``sha256`` 必须等于服务端 manifest 声明值，不符即拒（挡传输篡改）；
- **加载时**：解包后逐文件重算 ``sha256``，与 ``installed.json`` 记录逐一比对，任一不符
  即拒绝加载（挡落盘后被替换/注入）——第三方动态加载始终禁止，这里只校验第一方 bundle。

``manifest.json``（随包发布）描述插件元信息与入口模块文件名；``installed.json``（本模块
在安装时写出）记录 abi、来源 zip sha256 与解包后文件指纹，二者职责不同、互不覆盖。
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

from offcraft.config import config_dir

__all__ = [
    "PLUGIN_ID",
    "ZIP_PATH",
    "MANIFEST_PATH",
    "BundleError",
    "bundle_dir",
    "sha256_bytes",
    "file_digests",
    "install_zip",
    "is_installed",
    "verify_bundle",
    "read_manifest",
]

#: 离线联机官方插件 id 与下载路径（相对 download_base）
PLUGIN_ID = "offline-multiplayer"
ZIP_PATH = "/plugin/offline-multiplayer.zip"
MANIFEST_PATH = "/plugin/offline-multiplayer.manifest.json"

MANIFEST_FILE = "manifest.json"
INSTALLED_FILE = "installed.json"


class BundleError(RuntimeError):
    """插件包完整性校验失败或结构非法。"""


def bundle_dir(data_dir: str | Path | None = None, plugin_id: str = PLUGIN_ID) -> Path:
    return config_dir(data_dir) / "plugins" / plugin_id


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_digests(directory: Path) -> dict[str, str]:
    """解包目录内除 ``installed.json`` 外每个文件的 ``sha256``（键为 posix 相对路径）。"""
    digests: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name == INSTALLED_FILE:
            continue
        digests[path.relative_to(directory).as_posix()] = _sha256_file(path)
    return digests


def _safe_extract(archive: zipfile.ZipFile, target: Path) -> None:
    """解压并阻止路径穿越（``..`` / 绝对路径 / 盘符）。"""
    root = target.resolve()
    for member in archive.namelist():
        dest = (target / member).resolve()
        if dest != root and root not in dest.parents:
            raise BundleError(f"插件包含越界路径：{member}")
    archive.extractall(target)


def install_zip(
    data_dir: str | Path | None,
    zip_bytes: bytes,
    *,
    expected_sha256: str,
    abi: str = "",
    plugin_id: str = PLUGIN_ID,
) -> Path:
    """校验并解包一个官方插件 zip，落地 ``manifest.json`` + ``installed.json``。"""
    if not expected_sha256:
        raise BundleError("缺少服务端声明的 sha256，无法校验下载")
    if sha256_bytes(zip_bytes) != expected_sha256:
        raise BundleError("下载内容 sha256 与服务端声明不符（疑似被篡改）")
    directory = bundle_dir(data_dir, plugin_id)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise BundleError(f"下载内容不是合法 zip：{exc}") from exc
    with archive:
        _safe_extract(archive, directory)
    if not (directory / MANIFEST_FILE).is_file():
        shutil.rmtree(directory, ignore_errors=True)
        raise BundleError("插件包缺少 manifest.json")
    installed = {
        "abi": abi,
        "source_sha256": expected_sha256,
        "files": file_digests(directory),
    }
    (directory / INSTALLED_FILE).write_text(
        json.dumps(installed, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory


def is_installed(data_dir: str | Path | None = None, plugin_id: str = PLUGIN_ID) -> bool:
    directory = bundle_dir(data_dir, plugin_id)
    return (directory / MANIFEST_FILE).is_file() and (directory / INSTALLED_FILE).is_file()


def verify_bundle(data_dir: str | Path | None = None, plugin_id: str = PLUGIN_ID) -> bool:
    """逐文件重算 sha256 与安装记录比对；任一不符（含缺记录）返回 False。"""
    directory = bundle_dir(data_dir, plugin_id)
    try:
        installed = json.loads((directory / INSTALLED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    recorded = installed.get("files")
    if not isinstance(recorded, dict) or not recorded:
        return False
    return file_digests(directory) == recorded


def read_manifest(data_dir: str | Path | None = None, plugin_id: str = PLUGIN_ID) -> dict[str, Any]:
    directory = bundle_dir(data_dir, plugin_id)
    data = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BundleError("manifest.json 根节点不是对象")
    return data
