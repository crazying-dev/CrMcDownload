# server-deploy（CrMcDownload 服务端·部署）

存放部署物与文档：

- `DEPLOY.md`：域名、证书、防火墙、上线步骤
- `systemd/offcraft-mirror.service`
- `docker/Dockerfile`、`docker/compose.yaml`
- `caddy/Caddyfile`、`nginx/offcraft.conf`

默认监听 `127.0.0.1:8081`，由 nginx/Caddy 反代到子域名。

> 部署动作（创建服务、修改 nginx、申请证书）需项目负责人批准后执行。
