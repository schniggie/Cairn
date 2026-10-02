# Cairn 运维手册

本文档面向 Cairn 的日常部署与维护，命令默认在项目根目录执行。生产或长期运行环境推荐使用 Docker Compose；只有需要复用宿主机上已登录的 `claude`、`codex` 或 `pi` CLI 时，才使用本地模式。

> Cairn 可用于安全测试。请仅在已获得明确授权的目标和环境中运行。

## 1. 服务与数据说明

| 组件 | Compose 服务名 | 默认地址/用途 |
| --- | --- | --- |
| API 与 Web UI | `cairn-server` | `http://127.0.0.1:8000` |
| 调度器 | `cairn-dispatcher` | 读取 `dispatch.yaml`，分配并执行任务 |
| 项目 Worker | `cairn-dispatch-<项目 ID>` | 调度器按项目动态创建，不属于 Compose 服务 |
| SQLite 数据 | `./datas/cairn/cairn.db` | 通过目录挂载持久化到宿主机 |
| 调度配置 | `./dispatch.yaml` | 包含 Worker、并发、超时和凭据等配置 |

`dispatch.yaml` 可能包含 API Key。不要将真实配置提交到 Git，也不要把它直接粘贴到工单或日志中。

## 2. 环境要求与首次初始化

### 2.1 Docker Compose 模式（推荐）

需要：

- macOS 或 Linux；Windows 建议使用 Docker Desktop 的 Linux 容器模式；
- Docker Engine 与 Docker Compose v2；
- 可访问配置中所使用的模型服务。

检查环境：

```bash
docker version
docker compose version
docker compose config --services
```

首次部署时拉取基础镜像和 Worker 镜像：

```bash
docker pull ghcr.io/astral-sh/uv:python3.13-trixie
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
```

如果根目录还没有 `dispatch.yaml`，从示例复制后填写模型端点和凭据：

```bash
cp dispatch.example.yaml dispatch.yaml
```

PowerShell 对应命令：

```powershell
Copy-Item dispatch.example.yaml dispatch.yaml
```

不要覆盖已有的 `dispatch.yaml`。配置中的关键项包括：

- `server`：Compose 模式保持为 `http://cairn-server:8000`；
- `runtime.max_workers`：全局 Worker 并发上限；
- `runtime.max_running_projects`：同时调度的项目数；
- `runtime.max_project_workers`：单项目并发上限；
- `container.image`：项目 Worker 镜像；
- `workers`：Worker 类型、优先级、并发数及模型凭据。

配置文件只读校验：

```bash
docker compose config --quiet
```

### 2.2 本地模式

本地模式需要 Python 3.12 以上、`uv`，以及至少一个已经安装并登录的 Agent CLI。Agent 会直接继承当前用户权限，且没有容器沙箱，只应在可信环境中使用。

首次初始化：

```bash
cp dispatch.local.example.yaml dispatch.yaml
uv sync --project cairn
```

`dispatch.yaml` 中应设置 `runtime.execution: local`，`server` 通常为 `http://127.0.0.1:8000`。不要通过 Compose 启动本地模式的调度器，否则它无法复用宿主机上的 CLI 登录状态。

## 3. 启动

### 3.1 Docker Compose 启动

后台构建并启动全部服务：

```bash
docker compose up -d --build
```

查看状态：

```bash
docker compose ps
```

正常情况下，`cairn-server` 最终显示为 `healthy`，随后 `cairn-dispatcher` 启动。首次构建和首次拉取镜像所需时间可能较长。

启动后验证：

```bash
curl -fsS http://127.0.0.1:8000/projects
```

PowerShell 对应命令：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/projects
```

浏览器访问 `http://127.0.0.1:8000` 可打开 Web UI。

### 3.2 本地模式启动

打开两个终端。终端一启动服务端：

```bash
uv run --project cairn cairn serve
```

终端二先检查 Worker，再启动调度器：

```bash
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only
uv run --project cairn cairn dispatch --config dispatch.yaml
```

若需要供其他主机访问，可将服务端绑定到所有网卡：

```bash
uv run --project cairn cairn serve --host 0.0.0.0 --port 8000
```

绑定公网或局域网地址前，应通过防火墙或反向代理限制访问；项目本身未配置 TLS 和身份认证入口。

## 4. 停止、重启与移除

### 4.1 Compose 模式

为避免在调度任务时先关闭 API，建议先停调度器，再停服务端：

```bash
docker compose stop cairn-dispatcher
docker compose stop cairn-server
```

重新启动已停止的服务：

```bash
docker compose start
```

重启全部 Compose 服务：

```bash
docker compose restart
```

仅在修改 `dispatch.yaml` 后重启调度器：

```bash
docker compose restart cairn-dispatcher
```

停止并移除 Compose 管理的服务容器和网络：

```bash
docker compose down
```

`docker compose down` 不会删除绑定挂载的 `./datas/cairn/`，也不会删除调度器动态创建的 `cairn-dispatch-*` 项目 Worker 容器。不要随意使用 `docker compose down -v`、`docker system prune` 等扩大清理范围的命令。

### 4.2 本地模式

分别在调度器和服务端终端按 `Ctrl+C`。停止顺序仍建议先调度器、后服务端。长期运行时应交给 systemd、Supervisor 等进程管理器托管，并设置工作目录为项目根目录；仓库当前未提供现成的服务单元。

## 5. 日志与运行状态

查看全部 Compose 日志：

```bash
docker compose logs --tail 200
docker compose logs -f
```

按组件查看：

```bash
docker compose logs --tail 200 cairn-server
docker compose logs --tail 200 cairn-dispatcher
docker compose logs -f cairn-dispatcher
```

查看 Compose 服务和动态 Worker 容器：

```bash
docker compose ps --all
docker ps -a --filter name=cairn-dispatch-
```

查看某个项目 Worker 的状态和日志：

```bash
docker inspect cairn-dispatch-<项目 ID>
docker logs --tail 200 cairn-dispatch-<项目 ID>
```

常用检查：

```bash
docker stats --no-stream
docker system df
curl -fsS http://127.0.0.1:8000/projects
```

## 6. 配置变更与健康检查

修改 `dispatch.yaml` 前先备份，并避免在备份文件中泄露凭据。完成修改后，建议先运行一次启动健康检查。

Compose 模式：

```bash
docker compose stop cairn-dispatcher
docker compose run --rm --no-deps cairn-dispatcher uv run cairn dispatch --config dispatch.yaml --startup-healthcheck-only
docker compose up -d cairn-dispatcher
```

本地模式：

```bash
uv run --project cairn cairn dispatch --config dispatch.yaml --startup-healthcheck-only
```

常见配置调整：

- 资源不足或模型限流：降低 `runtime.max_workers` 和各 Worker 的 `max_running`；
- 单项目占满资源：降低 `runtime.max_project_workers`；
- 任务频繁超时：按任务类型增加 `tasks.*.timeout`；
- Worker 启动检查太慢：确认端点可达后再调整 `runtime.healthcheck_timeout`；
- 项目完成后不保留容器：将 `container.completed_action` 改为 `remove`。这会失去项目容器内的运行现场。

## 7. 数据备份与恢复

SQLite 使用 WAL 模式。最稳妥的文件级备份方式是先停止写入，再复制整个数据目录，而不是只复制 `cairn.db`。

### 7.1 备份

```bash
docker compose stop cairn-dispatcher
docker compose stop cairn-server
cp -a datas/cairn datas/cairn-backup-YYYYMMDD-HHMMSS
docker compose start
```

同时单独妥善备份 `dispatch.yaml`。它可能包含密钥，应使用受限权限和加密存储。

### 7.2 恢复

1. 停止调度器和服务端。
2. 再次备份当前 `datas/cairn/`，以便回退。
3. 用目标备份中的完整目录内容替换 `datas/cairn/`。
4. 执行 `docker compose start`。
5. 检查 `/projects`、Web UI 和服务日志。

恢复属于覆盖数据操作，执行前必须确认备份路径、恢复目标和回退副本均正确。

## 8. 升级与回滚

升级前先备份数据和 `dispatch.yaml`，再更新代码与镜像：

```bash
docker compose stop cairn-dispatcher
docker compose stop cairn-server
docker pull --platform=linux/amd64 ghcr.io/oritera/cairn-worker-container:latest
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail 200
```

升级后至少验证：

- `cairn-server` 为 `healthy`；
- `/projects` 返回成功；
- 调度器无持续报错；
- 新建或测试项目可以正常分配 Worker。

回滚时切回已验证的代码版本或镜像标签，恢复对应的数据备份，然后重新构建和启动。不要在没有数据库备份的情况下跨版本反复切换。

## 9. 测试与诊断命令

运行快速回归测试（不需要 Docker 或真实模型端点）：

```bash
uv run --project cairn --group dev pytest
```

查看 CLI 参数：

```bash
uv run --project cairn cairn --help
uv run --project cairn cairn serve --help
uv run --project cairn cairn dispatch --help
```

只执行一次调度循环后退出，适合观察配置和调度行为：

```bash
uv run --project cairn cairn dispatch --config dispatch.yaml --once
```

不要在常驻调度器仍运行时再执行 `--once`，否则两个调度进程可能同时竞争任务。

## 10. 常见故障

### 服务端一直不健康

```bash
docker compose ps
docker compose logs --tail 200 cairn-server
curl -v http://127.0.0.1:8000/projects
```

重点检查端口 `8000` 是否被占用、数据目录是否可写、镜像构建是否成功。

### 调度器没有启动

```bash
docker compose ps --all
docker compose logs --tail 300 cairn-dispatcher
```

调度器依赖服务端健康检查通过。还应检查 `dispatch.yaml` 是否存在、YAML 格式是否正确、模型凭据是否有效，以及 Docker Socket 是否已挂载。

### 无法创建项目 Worker

```bash
docker info
docker image inspect ghcr.io/oritera/cairn-worker-container:latest
docker ps -a --filter name=cairn-dispatch-
docker compose logs --tail 300 cairn-dispatcher
```

重点检查 Docker 权限、Worker 镜像架构、磁盘空间、`container.network_mode` 和模型端点网络连通性。

### 修改配置后未生效

`dispatch.yaml` 由调度器在启动时读取。修改后执行：

```bash
docker compose restart cairn-dispatcher
docker compose logs --tail 100 cairn-dispatcher
```

### 磁盘占用持续增长

先定位占用，不要直接做全局清理：

```bash
docker system df
docker ps -a --filter name=cairn-dispatch-
```

默认 `container.completed_action: stop` 会保留已完成项目的 Worker 容器，以便检查现场。确认某个具体容器已无保留价值后，再按准确名称执行：

```bash
docker rm cairn-dispatch-<项目 ID>
```

该操作会永久删除对应容器内未挂载的数据，不能恢复。

## 11. 快速命令表

| 场景 | 命令 |
| --- | --- |
| 启动 | `docker compose up -d --build` |
| 查看状态 | `docker compose ps` |
| 健康检查 | `curl -fsS http://127.0.0.1:8000/projects` |
| 跟踪调度日志 | `docker compose logs -f cairn-dispatcher` |
| 重启调度器 | `docker compose restart cairn-dispatcher` |
| 停止服务 | `docker compose stop` |
| 启动已停止服务 | `docker compose start` |
| 移除 Compose 服务 | `docker compose down` |
| 查看项目 Worker | `docker ps -a --filter name=cairn-dispatch-` |
| 回归测试 | `uv run --project cairn --group dev pytest` |

