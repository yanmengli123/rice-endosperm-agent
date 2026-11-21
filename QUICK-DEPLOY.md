# 下载并部署 Yuxi beta5 兼容版

从 GitHub 下载 `codex/yuxi-v08-compat` 分支的 ZIP 并解压，或克隆分支。启动 Docker Desktop，切换到 Linux 容器，然后双击 `deploy.cmd`。首次启动需联网拉取基础镜像和构建依赖。

```powershell
git clone --branch codex/yuxi-v08-compat --single-branch https://github.com/yanmengli123/rice-endosperm-agent.git
cd rice-endosperm-agent
.\deploy.cmd
```

启动后打开 [网页](http://127.0.0.1:5180) 创建管理员并配置模型供应商。APISIX 监听本机 `9089`。脚本在 `%LOCALAPPDATA%\Yuxi\yuxi-beta5` 保存数据库、上传文件、运行配置和密钥。空间紧张时可从 PowerShell 选择其他盘。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -DataDir 'D:\YuxiData\beta5'
```

`-Validate` 可先校验 Compose 配置。`-Project`、`-WebPort` 和 `-GatewayPort` 用于部署另一套实例。同一实例更新时使用原参数和数据目录，脚本会沿用现有配置及密钥。

如果构建时 Docker Hub 握手超时，可通过 `-DockerHubRegistry` 指定部署环境可访问的 Docker Hub 镜像站。该参数用于构建基础镜像，运行镜像仍从 Compose 声明的仓库拉取。

下载内容包含完整服务端、网页与构建文件。部署生成的数据库、上传、模型密钥和加密主密钥保存在数据目录，迁移原现网时需另行备份并恢复这些数据。当前旧部署仍可能挂载原 `Yuxi/docker/volumes`、本机配置和冻结发行源码，确认迁移及恢复完成后再清理相应目录。

Linux 可使用仓库中的 [生产部署说明](docs/advanced/deployment.md)。使用 `docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build`，并按实际地址配置反向代理及凭据。
