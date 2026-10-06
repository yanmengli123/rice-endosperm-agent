# 文献原生冷备与隔离恢复

适用 `yuxi-native-cold-v1`。这是部署管理员使用的原生状态灾备工具，不是普通用户 `/literature` ZIP 导入。当前用户已要求暂不停机；以下是维护窗口运行手册，**本轮不执行生产停机或恢复**。

## 范围和前提

保存 PostgreSQL、MinIO、Milvus、etcd、Redis、Neo4j 及 API/worker 的 saves 等状态挂载原生字节；版本历史、回收站、队列及账本随原生存储保留。只收集明确指定 `docker/volumes` 根下的可写绑定及部署命名卷。不读取容器环境变量或把 .env/源代码打进归档。

维护前应独立保管匹配的代码快照、镜像 ID（含 helper 镜像）、部署配置、TLS/供应商配置及 secret_crypto 主密钥。归档加密密钥是另一个 32 随机字节二进制文件，存于单独密钥库；不能与备份一起外发，不能作为 CLI 参数或写入仓库。工具依赖已有 Docker CLI/cryptography，无新 Python SDK。

所有 Compose 服务和任何共享挂载的写入者须停止，并保证整个窗口无其他宿主写入；工具只检查和拒绝，不替操作者停机。先等待在途操作排空并保存运行清单。正在运行的项目不能生成“成功”冷备。跨卷外部 tablespace/链接被拒绝，不承诺备份未声明的外部挂载。单批 100 GiB，单清单 1000 卷，超过时应制定分层灾备方案。

## 操作入口

不要在已停止的 api 容器中 `docker exec`。运行一个**无 Compose 项目标签**、无外网、仅挂载 Docker socket/只读代码/独立归档与密钥目录的临时运维容器。Docker socket 等同宿主管理员权限，仅限受信管理员；不要暴露为网页接口。

以下 PowerShell 示例需要事先准备 `D:/yuxi/backups/native` 和独立 `D:/yuxi/backups/native-key`，key 目录中已有 32 字节 `snapshot.key`，且进入维护窗口。归档目录不得位于源状态根内。`$nativeImage` 从维护前的 api inspect 取得并保管；根目录必须使用 Docker inspect 显示的 Source 格式（本机是 Windows 绝对路径），其他主机应据实修改。

```powershell
$nativeImage = docker inspect api-dev --format '{{.Image}}'
$nativeMounts = @(
  '--mount', 'type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock',
  '--mount', 'type=bind,source=D:/yuxi/Yuxi/backend/package,target=/app/package,readonly',
  '--mount', 'type=bind,source=D:/yuxi/backups/native,target=/native',
  '--mount', 'type=bind,source=D:/yuxi/backups/native-key,target=/keys,readonly'
)
docker run --rm --network none @nativeMounts --entrypoint uv $nativeImage run --no-sync python -m yuxi.literature.native_recovery preflight --project yuxi --state-root D:/yuxi/Yuxi/docker/volumes
docker run --rm --network none @nativeMounts --entrypoint uv $nativeImage run --no-sync python -m yuxi.literature.native_recovery backup --project yuxi --state-root D:/yuxi/Yuxi/docker/volumes --directory /native/snapshot-001 --key-file /keys/snapshot.key
docker run --rm --network none @nativeMounts --entrypoint uv $nativeImage run --no-sync python -m yuxi.literature.native_recovery verify --directory /native/snapshot-001 --key-file /keys/snapshot.key
docker run --rm --network none @nativeMounts --entrypoint uv $nativeImage run --no-sync python -m yuxi.literature.native_recovery restore --directory /native/snapshot-001 --key-file /keys/snapshot.key --prefix yuxi-restore-lab
```

备份目的目录必须不存在；失败产生的未封存文件保留供排查，不当成成功快照。`manifest.enc` 为认证加密清单，各卷分别加密、校验 SHA256。verify 和 restore 验证整个清单及所有 tar 后才允许恢复写入。密钥错误、篡改、缺卷、越界链接或不匹配文件失败关闭。

restore 输出新卷名、消费服务/挂载位置和原镜像 ID，`started=false`。卷名包含唯一操作 ID，现有卷不覆盖；失败已创建的隔离卷保留，不自动删除取证材料。定位可使用 `yuxi.native-recovery` 标签，必须确认精确名称、标签和无消费者后才清理，禁止按生产目录或模糊前缀批量删除。

## 恢复后门禁

1. 在独立 Compose 项目中以输出映射挂载新卷，固定原镜像和代码，独立端口/网络、配置及加密主密钥；**禁止直接复用生产 volume 映射**。
2. 隔离环境先禁外部 OCR/LLM/Embedding/MCP/消息通道、停止 worker/scheduler/outbox 消费。Redis 恢复保留队列不等于授权重放；不得恢复供应商调用后再次计费。
3. 先逐一启动存储，检查健康、PG schema/约束/RLS/序列、对象摘要和有效版本、图/向量索引血缘；再启动无外部消费的 API 验证正常身份、来源 ACL、阅读、历史和账本只读。
4. 对运行中任务、outbox、租约、fencing 和账本进行人工审计；恢复执行必须遵循原生幂等及审批。不得清空/改写 append-only usage_ledger，不在归档里执行 SQL/脚本。
5. 完整演练通过、审核失败补偿和回滚路径后，另行批准上线切换。工具不自动切换、启 worker、重放旧任务或恢复其他租户权限。

## 已完成与未完成的验收

Docker 真实文件夹具验证字节/模式、加密认证及篡改拒绝；network-none PostgreSQL 克隆验证历史/冻结指针/回收站/账本/任务/索引**夹具记录**与 BIGSERIAL 序列。测试使用唯一标签资源，结束清理这些夹具，不访问或停机生产存储。

尚未执行当前生产数据冷备、完整多存储服务启动恢复、实际大库往返/故障压测，不能据此宣称整机灾备已验收。冷备工具不是在线一致性快照方案；不停机备份需另行实施各引擎受支持的协调快照及可审计水位协议，不可直接拷贝运行中的卷。
