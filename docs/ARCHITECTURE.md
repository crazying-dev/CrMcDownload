# 架构

## 数据流

```
客户端                          镜像服务端                      上游
------                          ----------                     ----
版本清单  ──/meta/...────────▶  白名单校验 ──▶ 磁盘缓存 ──▶ piston-meta.mojang.com
版本 JSON ──/meta/v1/...─────▶                 │
client jar──/data/v1/...─────▶                 ├──▶ piston-data / launcher.mojang.com
库文件    ──/libs/...────────▶                 ├──▶ libraries.minecraft.net
资源对象  ──/assets/xx/...───▶                 ├──▶ resources.download.minecraft.net
Fabric    ──/fabric/...──────▶                 ├──▶ meta.fabricmc.net
Fabric jar──/fabricmaven/...─▶                 ├──▶ maven.fabricmc.net
Forge jar ──/forgemaven/...──▶                 ├──▶ maven.minecraftforge.net
NeoForge  ──/neoforgemaven/...▶                ├──▶ maven.neoforged.net
Java 运行时─/meta/v1/products/...▶              ├──▶ launchermeta（java-runtime all.json）
搜索 / 版本──/modrinth/...?...▶                ├──▶ api.modrinth.com（保留查询串）
内容文件  ──/modcdn/...──────▶                 └──▶ cdn.modrinth.com
```

## URL 重写

客户端只认 `download_base`，所有上游 URL 按主机映射：

| 上游主机 | 客户端路径 |
| --- | --- |
| `piston-meta.mojang.com`、`launchermeta.mojang.com` | `{base}/meta/<path>` |
| `piston-data.mojang.com`、`launcher.mojang.com` | `{base}/data/<path>` |
| `libraries.minecraft.net` | `{base}/libs/<path>` |
| `resources.download.minecraft.net` | `{base}/assets/<path>` |
| `meta.fabricmc.net` | `{base}/fabric/<path>` |
| `maven.fabricmc.net` | `{base}/fabricmaven/<path>` |
| `maven.minecraftforge.net` | `{base}/forgemaven/<path>` |
| `maven.neoforged.net` | `{base}/neoforgemaven/<path>` |
| `api.modrinth.com` | `{base}/modrinth/<path>?<query>` |
| `cdn.modrinth.com` | `{base}/modcdn/<path>` |

maven 路由下 `*.xml`（`maven-metadata.xml`）按元数据缓存（TTL 600s），其余 jar 视为不可变。

未识别主机直接报错，绝不回落到直连。唯一的例外是**内容层**（模组 / 整合包 / 资源包 / 光影）：
当镜像缺少新路由或返回非 2xx 时会主动回落到 Modrinth 官方 API，以免自建镜像未升级时功能不可用。

## 安装管线

```
版本清单 → 版本 JSON → client jar → 库（规则过滤 + natives 分类器）
        → 资源索引 → 资源对象 → logging 配置 → versions/<id>/<id>.json → 实例注册
```

- 每个文件：`.part` 临时文件 + `Range` 续传 + SHA-1/大小校验 + 原子 rename；
- 已存在且哈希正确即跳过，支持断网复用与离线包导入；
- 进度按字节聚合，支持取消与重试。

## 内容管线（模组 / 资源包 / 光影）

```
搜索（facets：project_type / categories:<加载器> / versions:<MC 版本>）
→ 版本列表（取 primary 文件 + required 依赖）
→ 下载（SHA-1 / SHA-512 / size 校验，.part + 原子 rename）
→ 安装到 <实例游戏目录>/{mods,resourcepacks,shaderpacks}
```

必需依赖按广度优先安装，单次安装上限 24 个文件，防止异常依赖图无限扩张。

## 加载器与 Java 运行时

```
安装版本 → 选择加载器对话框（原版 / Fabric / Forge / NeoForge）
  ├─ 原版      ：只装 Mojang 文件，不能装模组（资源包不受影响）
  ├─ Fabric    ：拉 loader profile JSON → versions/<fabric-...>/<id>.json → 补库
  └─ Forge/NeoForge：下载 installer jar → java -jar <installer> --installClient <mc_dir> → 识别新版本目录

Java：读取 Mojang java-runtime all.json → 按平台列出 jre-legacy / java-runtime-*
     → 展开清单 files（拒绝 ../ 越界）→ runtime/<component>/bin/java.exe
```

Forge / NeoForge 安装器会直接从自己的 maven 拉依赖（不受镜像控制），且必须先有可用的 Java。
游戏内“能不能装模组”由实例的加载器决定：`原版` 实例只能装资源包，模组 / 光影必须选带加载器的实例
（已有实例可在版本右键菜单「安装加载器…」补装）。

## 整合包管线

```
.mrpack → modrinth.index.json（dependencies → MC 版本 + loader）
→ 安装基础版本（Fabric：拉取 loader profile JSON 并展开为版本文件）
→ 逐个安装 index 文件（相对路径，拒绝 ../ 越界）
→ 解压 overrides/ → 写 offcraft-pack.json
```

Forge / NeoForge 加载器需要经由「安装加载器…」或安装时选择，整合包自身不会触发安装器。

## 启动管线

```
合并 inheritsFrom → rules 过滤 → classpath（客户端 jar 置尾）
→ natives 解压 → 占位符展开（arguments / minecraftArguments）
→ 选择 Java（runtime 组件 / JAVA_HOME / 常见 JDK / PATH）
→ 子进程启动（cwd = 游戏目录） + 日志流
```

离线账户：`--username` + v3 UUID（`MD5("OfflinePlayer:" + name)`）+ `accessToken=0`。

## 版本隔离

`isolation=true`（默认）时游戏目录 = `versions/<id>/`，`--gameDir` 与子进程工作目录均指向它，
`options.txt`、`saves`、`mods`、`screenshots` 互不干扰；整合包实例恒为隔离，并通过 `offcraft-pack.json` 定位。
关闭隔离后回到原生布局（共享 `.minecraft`）。

## 离线能力

- 已安装版本启动阶段零网络请求；
- 镜像不可达时回落到本地实例与缓存清单；
- 离线包：`versions/<id>` + 规则过滤后的 libraries + assets（可选 runtime）打包，导入端解压即可启动。
