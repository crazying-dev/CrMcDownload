"""Modrinth 内容 API。

默认优先走自建镜像（``{download_base}/modrinth/v2``），镜像未部署新版或不可达时
自动回退到官方 ``api.modrinth.com``，并把回退结果缓存到本次进程。
文件下载 URL（``cdn.modrinth.com``）同样在镜像模式下改写为
``{download_base}/modcdn/...``，因此客户端在镜像可用时只与自己的域名通信。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from offcraft import DEFAULT_USER_AGENT
from offcraft.mirror import Mirror, UnknownUpstreamError
from offcraft.net.downloader import DownloadError, Downloader

__all__ = [
    "ContentError",
    "Dependency",
    "MODRINTH_API",
    "ModrinthClient",
    "PROJECT_TYPES",
    "ProjectHit",
    "ProjectSearch",
    "ProjectVersion",
    "VersionFile",
]

MODRINTH_API = "https://api.modrinth.com/v2"
UA = f"{DEFAULT_USER_AGENT} (+https://mc.yjlt.top)"

#: 界面可选的工程类型（与 Modrinth 的 project_type 对应）
PROJECT_TYPES = {
    "mod": "模组",
    "modpack": "整合包",
    "resourcepack": "资源包",
    "shader": "光影",
}

#: 模组加载器（Modrinth 的 categories facet）
LOADERS = {
    "fabric": "Fabric",
    "forge": "Forge",
    "neoforge": "NeoForge",
    "quilt": "Quilt",
}


class ContentError(RuntimeError):
    """内容接口请求失败。"""


@dataclass(frozen=True, slots=True)
class ProjectHit:
    id: str
    slug: str
    title: str
    description: str
    project_type: str
    author: str = ""
    downloads: int = 0
    follows: int = 0
    icon_url: str = ""
    categories: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    date_modified: str = ""
    client_side: str = ""
    server_side: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ProjectHit":
        return cls(
            id=str(data.get("project_id") or data.get("id") or ""),
            slug=str(data.get("slug") or data.get("project_id") or ""),
            title=str(data.get("title") or data.get("slug") or ""),
            description=str(data.get("description") or ""),
            project_type=str(data.get("project_type") or "mod"),
            author=str(data.get("author") or ""),
            downloads=int(data.get("downloads") or 0),
            follows=int(data.get("follows") or 0),
            icon_url=str(data.get("icon_url") or ""),
            categories=tuple(str(item) for item in (data.get("display_categories") or data.get("categories") or [])),
            versions=tuple(str(item) for item in (data.get("versions") or [])),
            date_modified=str(data.get("date_modified") or ""),
            client_side=str(data.get("client_side") or ""),
            server_side=str(data.get("server_side") or ""),
        )

    @property
    def page_url(self) -> str:
        kind = {"mod": "mod", "modpack": "modpack", "resourcepack": "resourcepack", "shader": "shader"}.get(
            self.project_type, "project"
        )
        return f"https://modrinth.com/{kind}/{self.slug}"


@dataclass(frozen=True, slots=True)
class ProjectSearch:
    hits: tuple[ProjectHit, ...]
    total: int
    offset: int


@dataclass(frozen=True, slots=True)
class VersionFile:
    url: str
    filename: str
    primary: bool = False
    size: int = 0
    sha1: str = ""
    sha512: str = ""

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "VersionFile":
        hashes = data.get("hashes") or {}
        return cls(
            url=str(data.get("url") or ""),
            filename=str(data.get("filename") or "download.bin"),
            primary=bool(data.get("primary")),
            size=int(data.get("size") or 0),
            sha1=str(hashes.get("sha1") or ""),
            sha512=str(hashes.get("sha512") or ""),
        )


@dataclass(frozen=True, slots=True)
class Dependency:
    version_id: str | None = None
    project_id: str | None = None
    file_name: str | None = None
    dependency_type: str = "required"

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Dependency":
        return cls(
            version_id=data.get("version_id") or None,
            project_id=data.get("project_id") or None,
            file_name=data.get("file_name") or None,
            dependency_type=str(data.get("dependency_type") or "required"),
        )

    @property
    def required(self) -> bool:
        return self.dependency_type == "required"


@dataclass(frozen=True, slots=True)
class ProjectVersion:
    id: str
    project_id: str
    name: str
    version_number: str
    version_type: str
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]
    date_published: str = ""
    downloads: int = 0
    files: tuple[VersionFile, ...] = ()
    dependencies: tuple[Dependency, ...] = ()

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ProjectVersion":
        return cls(
            id=str(data.get("id") or ""),
            project_id=str(data.get("project_id") or ""),
            name=str(data.get("name") or data.get("version_number") or ""),
            version_number=str(data.get("version_number") or ""),
            version_type=str(data.get("version_type") or "release"),
            game_versions=tuple(str(item) for item in (data.get("game_versions") or [])),
            loaders=tuple(str(item) for item in (data.get("loaders") or [])),
            date_published=str(data.get("date_published") or ""),
            downloads=int(data.get("downloads") or 0),
            files=tuple(VersionFile.from_json(item) for item in (data.get("files") or []) if item.get("url")),
            dependencies=tuple(
                Dependency.from_json(item) for item in (data.get("dependencies") or [])
            ),
        )

    @property
    def primary_file(self) -> VersionFile | None:
        for item in self.files:
            if item.primary:
                return item
        return self.files[0] if self.files else None

    @property
    def label(self) -> str:
        return self.version_number or self.name or self.id


class ModrinthClient:
    """轻量 Modrinth 客户端（零第三方依赖）。

    ``download_base`` 为空时直接访问官方 API；非空时先尝试镜像，失败后回退。
    """

    def __init__(
        self,
        download_base: str = "",
        *,
        downloader: Downloader | None = None,
        proxy: str = "",
        token: str = "",
        timeout: float = 25.0,
    ) -> None:
        self.downloader = downloader or Downloader(
            proxy=proxy or None,
            token=token or None,
            workers=4,
            timeout=timeout,
            user_agent=UA,
        )
        self._mirror: Mirror | None = None
        base = (download_base or "").strip().rstrip("/")
        if base:
            try:
                self._mirror = Mirror(base)
            except ValueError:
                self._mirror = None
        self._mirror_ok: bool | None = None
        self._active_mode = "direct" if self._mirror is None else "mirror"
        self.notes: list[str] = []

    # ------------------------------------------------------------------ HTTP
    @property
    def api_mode(self) -> str:
        return "镜像" if self._active_mode == "mirror" else "官方源"

    def _bases(self) -> list[tuple[str, str]]:
        bases: list[tuple[str, str]] = []
        if self._mirror is not None and self._mirror_ok is not False:
            bases.append(("mirror", self._mirror.base_url + "/modrinth/v2"))
        bases.append(("direct", MODRINTH_API))
        return bases

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = ""
        if params:
            clean = {key: value for key, value in params.items() if value not in (None, "")}
            if clean:
                query = "?" + urlencode(sorted(clean.items()), doseq=True)
        errors: list[str] = []
        for mode, base in self._bases():
            url = f"{base}{path}{query}"
            try:
                data = self.downloader.fetch_json(url)
            except DownloadError as exc:
                errors.append(f"{mode}: {exc}")
                if mode == "mirror":
                    self._mirror_ok = False
                    self.notes.append("内容镜像不可用，已回退到 Modrinth 官方源")
                    continue
                raise ContentError("Modrinth 请求失败：" + "；".join(errors)) from exc
            self._mirror_ok = mode == "mirror"
            self._active_mode = mode
            return data
        raise ContentError("Modrinth 请求失败：" + "；".join(errors))

    def _api_path(self, value: str) -> str:
        return quote(str(value), safe="")

    # ------------------------------------------------------------------ API
    def search(
        self,
        query: str,
        project_type: str = "mod",
        *,
        game_version: str = "",
        loader: str = "",
        limit: int = 20,
        offset: int = 0,
        index: str = "relevance",
    ) -> ProjectSearch:
        facets: list[list[str]] = [[f"project_type:{project_type}"]]
        if loader and project_type in ("mod", "modpack"):
            facets.append([f"categories:{loader}"])
        if game_version:
            facets.append([f"versions:{game_version}"])
        data = self._get_json(
            "/search",
            {
                "query": query or "",
                "facets": json.dumps(facets, separators=(",", ":")),
                "limit": max(1, min(100, int(limit))),
                "offset": max(0, int(offset)),
                "index": index,
            },
        )
        hits = tuple(ProjectHit.from_json(item) for item in (data.get("hits") or []))
        return ProjectSearch(hits=hits, total=int(data.get("total_hits") or 0), offset=max(0, int(offset)))

    def project(self, project_id: str) -> dict[str, Any]:
        return self._get_json(f"/project/{self._api_path(project_id)}")

    def versions(
        self,
        project_id: str,
        *,
        game_version: str = "",
        loader: str = "",
    ) -> tuple[ProjectVersion, ...]:
        params: dict[str, Any] = {}
        if game_version:
            params["game_versions"] = json.dumps([game_version])
        if loader:
            params["loaders"] = json.dumps([loader])
        data = self._get_json(f"/project/{self._api_path(project_id)}/version", params)
        return tuple(
            ProjectVersion.from_json(item) for item in (data or []) if isinstance(item, dict)
        )

    def version(self, version_id: str) -> ProjectVersion:
        data = self._get_json(f"/version/{self._api_path(version_id)}")
        return ProjectVersion.from_json(data)

    def best_version(
        self,
        project_id: str,
        *,
        game_version: str = "",
        loader: str = "",
    ) -> ProjectVersion | None:
        versions = self.versions(project_id, game_version=game_version, loader=loader)
        for item in versions:
            if item.primary_file is not None:
                return item
        return versions[0] if versions else None

    # ----------------------------------------------------------------- files
    def resolve_url(self, url: str) -> str:
        """把 Modrinth 返回的文件地址转换为下载地址（镜像模式改写 CDN）。"""
        if not url:
            return url
        if self._active_mode == "mirror" and self._mirror is not None:
            try:
                return self._mirror.rewrite(url)
            except UnknownUpstreamError:
                return url
        return url
