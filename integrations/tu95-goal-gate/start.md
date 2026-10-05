# Cairn 启动说明（后台运行）

本页用于记录当前项目的后台启动与重启命令。

## 零、Docker 启动（推荐，数据持久化 + 自动重启）

仓库根目录自带 `Dockerfile` 与 `docker-compose.yaml`，单容器同时跑 server + dispatcher
（`cairn launch`），数据库与运行产物通过 bind mount 落在 `./workspace/`，
容器删除重建不丢数据；`restart: unless-stopped` 保证崩溃或 Docker Desktop 重启后自动拉起。

```bash
cd /Users/gx1000/code/Cairn
docker compose up -d --build     # 首次或代码变更后：构建并启动
docker compose up -d             # 日常启动
docker compose logs -f           # 看日志
docker compose down              # 停止并删除容器（数据保留在 ./workspace）
```

- 访问地址：`http://127.0.0.1:8000`
- 数据位置：`./workspace/cairn.db`；Web UI 自定义模型列表保存在 `./workspace/dispatch.models.yaml`（备份 = 拷贝整个 `workspace/` 目录）
- `dispatch.yaml` 以可写方式挂载进容器；可在 Web UI 中保存 Worker 配置并触发 Dispatcher 热重载。手工修改配置后可执行 `docker compose restart`
- 默认镜像内置 Claude Code 与 Codex CLI，供 Web 配置页的连通性 ping 使用；Worker 的实际执行见下节
- 注意：容器内外都用 8000 端口，启动容器前请先停掉宿主机实例（`python3 start.py stop`），反之亦然

### Worker 容器架构（docker-only，无 host 降级）

Worker（claude/codex/pi CLI）只在独立 Docker 容器内执行，不再以本地进程运行：

- cairn 应用容器通过挂载的 `/var/run/docker.sock` 操作**宿主机** docker（硬性前提）。
- 每个项目一个长寿命容器 `cairn-worker-<pid>`（镜像 `cairn-worker-container:latest`，`sleep infinity`），
  任务经 `docker exec` 进入执行；项目 completed/stopped 后按 `container.completed_action`（默认 remove）处理，
  deleted 项目的容器由孤儿回收（label 扫描）清理，其 `workspace/<pid>` 目录随删除接口一并回收。
- 另有共享 startup 容器 `cairn-worker-startup`，用于启动健康检查与 intake 审查，dispatcher 退出时销毁。
- docker daemon 不可达或镜像缺失时 Dispatcher 启动直接报错退出，并打印修复指引，不降级。
- **权限提示**：挂载 docker.sock 等于把宿主机 docker 控制权交给应用容器，与单用户可信边界一致，不要再暴露给不可信网络。
- **挂载路径**：daemon 在宿主机，bind mount 源必须是宿主机真实路径。compose 已注入
  `CAIRN_WORKSPACE_HOST_PATH=${PWD}/workspace`，请在仓库根目录执行 `docker compose`；
  在别处执行或目录布局不同时，先 `export CAIRN_WORKSPACE_HOST_PATH=<仓库>/workspace` 再启动。
- 旧本地模式（含 `IS_SANDBOX`）已移除；升级后如有历史残骸可清理：

```bash
# 旧版容器命名 cairn-dispatch-* / cairn-startup-healthcheck-* / cairn-local-*
docker ps -aq --filter name=cairn-dispatch- | xargs -r docker rm -f
docker ps -aq --filter name=cairn-startup-healthcheck- | xargs -r docker rm -f
```

## 一、后台启动（screen 方式，已过时，建议用 Docker）

```bash
cd /Users/gx1000/code/Cairn
screen -dmS cairn-launch \
  zsh -lc 'cd /Users/gx1000/code/Cairn && uv run --project cairn cairn launch --config dispatch.yaml > /tmp/cairn-launch.log 2>&1'
```

- `-dmS cairn-launch`：在后台新建名为 `cairn-launch` 的 screen 会话，不占用当前终端。
- `zsh -lc '...'`：使用登录 shell 执行命令，保证环境变量、`uv` 等命令可用。
- `> /tmp/cairn-launch.log 2>&1`：将标准输出和错误都写到日志文件，便于排查问题。

## 二、查看运行状态

```bash
screen -ls | grep cairn-launch
ps -ef | grep -E 'cairn launch|SCREEN -dmS cairn-launch' | grep -v grep
tail -f /tmp/cairn-launch.log
```

- `screen -ls`：确认 `cairn-launch` 会话是否还在。
- `ps -ef ...`：确认 `cairn launch` 进程链路是否存在。
- `tail -f`：实时查看启动日志。

## 三、后台重启（推荐）

```bash
pkill -f "cairn launch --config dispatch.yaml" || true
screen -S cairn-launch -X quit || true
cd /Users/gx1000/code/Cairn
screen -dmS cairn-launch \
  zsh -lc 'cd /Users/gx1000/code/Cairn && uv run --project cairn cairn launch --config dispatch.yaml > /tmp/cairn-launch.log 2>&1'
```

- 先杀掉旧的 `cairn launch` 进程（`pkill -f`），避免端口冲突或多实例竞争。
- 再关闭旧 `screen` 会话（`screen -S ... -X quit`）。
- 重新按“后台启动”逻辑拉起新实例。

## 四、完整停止

```bash
pkill -f "cairn launch --config dispatch.yaml" || true
screen -S cairn-launch -X quit || true
```

- 两条命令一起执行可清理进程与会话。
- 如果你只想优雅退出，可只 `pkill` 先停服务，再确认 `ps` 无残留再 `quit` 会话。

## 五、端口与访问地址

- 本地服务启动后默认监听：`http://127.0.0.1:8000`
- 若配置了允许外网访问，会显示 `http://0.0.0.0:8000`，可按你的 `dispatch.yaml` 与防火墙策略访问。
