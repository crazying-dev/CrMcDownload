# 架构

## 数据流

```
客户端                        镜像服务端                     上游
------                        ----------                     ----
版本清单  ──/meta/...──────▶  白名单校验 ──▶ 磁盘缓存 ──▶  piston-meta.mojang.com
版本 JSON ──/meta/v1/...───▶                 │
client jar──/data/v1/...───▶                 ├──▶ piston-data / launcher.mojang.com
库文件    ──/libs/...──────▶                 ├──▶ libraries.minecraft.net
资源对象  ──/assets/xx/...─▶                 ├──▶ resources.download.minecraft.net
Fabric    ──/fabric/...────▶                 └──▶ meta.fabricmc.net
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

未识别主机直接报错，绝不回落到直连。

## 安装管线

```
版本清单 → 版本 JSON → client jar → 库（规则过滤 + natives 分类器）
        → 资源索引 → 资源对象 → logging 配置 → versions/<id>/<id>.json → 实例注册
```

- 每个文件：`.part` 临时文件 + `Range` 续传 + SHA-1/大小校验 + 原子 rename；
- 已存在且哈希正确即跳过，支持断网复用与离线包导入；
- 进度按字节聚合，支持取消与重试。

## 启动管线

```
合并 inheritsFrom → rules 过滤 → classpath（客户端 jar 置尾）
→ natives 解压 → 占位符展开（arguments / minecraftArguments）
→ 选择 Java（runtime 组件 / JAVA_HOME / 常见 JDK / PATH）
→ 子进程启动 + 日志流
```

离线账户：`--username` + v3 UUID（`MD5("OfflinePlayer:" + name)`）+ `accessToken=0`。

## 离线能力

- 已安装版本启动阶段零网络请求；
- 镜像不可达时回落到本地实例与缓存清单；
- 离线包：`versions/<id>` + 规则过滤后的 libraries + assets（可选 runtime）打包，导入端解压即可启动。
