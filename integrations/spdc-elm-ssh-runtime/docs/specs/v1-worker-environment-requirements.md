# Cairn Worker Environment Requirements v1.1

日期：2026-05-18

状态：当前需求基线。v1.1 修订 v1 中“server 完全不持久化 LLM endpoint/secret”的判断：`base_url` 与 `api_key` 在实际部署中通常同属一个 provider endpoint，并且 endpoint 往往随 execution environment 的网络位置变化。因此 endpoint 应归属于 server-side environment；worker 仍只描述模型与调度能力。

## 1. 核心判断

Cairn 的边界应是：

- server 管“任务、地点、该地点可用的 provider endpoint”。
- dispatcher 管“兵力、模型 profile、调度策略”。

换言之，project 绑定 execution environment；environment 描述代码在哪里运行，以及从该地点如何访问 LLM provider；worker/model_profile 决定用什么模型、什么上下文能力、多少并发、可跑哪些任务。

关键修正：

- `base_url + provider_api + api_key` 是一个 provider endpoint，通常不可拆开。
- endpoint 与执行地点强相关，应随 environment 存在。
- `model` 不属于 environment；同一 environment endpoint 可被多个 model worker 复用。

## 2. v0/v1 已完成基础

已有基础：

- dispatcher 任务层已从 `ContainerManager` 解耦到 `WorkEnvironment`。
- Docker 与 SSH 都是 environment backend。
- SSH backend 可通过 OpenSSH 连接远端，准备 workspace，安装/调用远端 `cairn-runner`，支持 stdout/stderr 回流、timeout、cancel、cleanup。
- server 已有 environment CRUD、healthcheck、project 创建选择 environment 的 UI/API。
- dispatcher 已能从 server 拉取 environments，并按 project `environment_id` 选择 backend。
- Pi worker 已能在远端 workspace 下生成私有 `.cairn/pi/<worker>` 配置并运行。

v1 的不足：

- 将 `base_url` 与 `api_key` 放在 dispatcher profile 中，假设同一 worker profile 在所有 environment 中都能用同一个 endpoint。
- 实际上 Docker、宿主机、SSH 远端、内网机器访问同一服务的 URL 可能不同；换中转 API 时 key 往往也随之变化。
- 因此 v1.1 将 endpoint 移入 server environment。

## 3. 目标分工

### server.db

server 持久化：

- projects
- work environments
- project -> environment binding
- environment provider endpoints
- environment healthcheck result

environment 字段：

- `id`
- `label`
- `backend`: `ssh` 或 `docker`
- `ssh_command`
- `workspace_root`
- `harness`
- `cleanup`
- `terminal`
- `provider_endpoints[]`

provider endpoint 字段：

- `id`
- `type`: `pi`、`codex`、`claudecode` 等
- `base_url`
- `provider_api`
- `api_key`
- `created_at`
- `updated_at`

server 不负责选择 worker，不持久化 model，不持久化 worker 并发/优先级策略。

### dispatcher config files

dispatcher 配置：

- dispatcher runtime：server URL、poll interval、timeouts、并发上限。
- scheduling policy：worker priority、max_running、task_types、allowed_environments。
- model profiles：model、context_window、driver type。
- workers：worker 类型、model_profile 引用、endpoint id 引用、任务能力。

配置文件分三类：

- `dispatch.example.yaml`：唯一可提交样例；不得含真实 secret 或真实 endpoint。
- `dispatch.dev.yaml`：本机开发配置；必须 gitignored；通常用于 mock/dev。
- `dispatch.local.yaml`：个人私密配置；必须 gitignored；v1.1 起不再需要放 LLM API key，除非后续显式支持 local endpoint override。

不再使用 `dispatch.yaml` 主文件名。运行 dispatcher 时显式选择 `--config dispatch.dev.yaml` 或 `--config dispatch.local.yaml`。

### model_profile

model_profile 描述模型能力：

- `id`
- `type`
- `model`
- `context_window`

model_profile 不包含 `base_url`、`provider_api`、`api_key`。

### worker

worker 描述调度与执行能力：

- `name`
- `type`
- `model_profile`
- `endpoint`
- `task_types`
- `max_running`
- `priority`
- `allowed_environments`
- 非 secret `env`

`worker.endpoint` 是逻辑 endpoint id。dispatcher 在具体 environment 上运行该 worker 时，从该 environment 的 `provider_endpoints` 中查找同名 endpoint。

## 4. Secret 边界

v1.1 允许 server DB 存 `api_key`，但必须按 secret 处理。

最低要求：

- 普通 `GET /environments` 不返回明文 key。
- 响应只可返回 `has_api_key: true`、`api_key_preview` 或 redacted 标记。
- 只有 dispatcher 使用的内部/显式参数可请求 endpoint secret。
- UI input 允许写入/更新 key，但默认永不展示明文。
- update 时空 key 表示保留旧 key；显式 `clear_api_key: true` 才删除。
- run log、startup healthcheck、server healthcheck、error preview 不得出现 API key 明文。

MVP 可先使用 SQLite 明文存储，但文档与 UI 必须明确这是 local/trusted deployment 假设。后续可加 DB encryption、keychain、Vault 或 per-endpoint secret backend；这些不改变外部模型。

## 5. 不做 snapshot

v1.1 仍不做 `environment_snapshot_json`。

执行真相仍是：

```text
project.environment_id -> current server environment -> current provider endpoint
```

若以后需要审计，可新增只含 redacted endpoint metadata 的历史记录；不得把明文 key 或完整 snapshot 用作调度输入。

## 6. 不做兼容

v1.1 不兼容 v1 临时 dispatcher profile schema：

- 不再接受 `profiles[].base_url`。
- 不再接受 `profiles[].provider_api`。
- 不再接受 `profiles[].api_key`。
- `profiles[]` 重命名为 `model_profiles[]`。
- worker 使用 `model_profile` 与 `endpoint`，不再使用 `profile`。

理由：v1 尚未成为稳定用户数据契约，破坏式改正比保留双语义更低成本。

## 7. Healthcheck 分层

### server environment healthcheck

server 检查地点与 endpoint 配置：

- SSH 是否免交互可连。
- `workspace_root` 是否可创建、读写、清理 probe 文件。
- `harness` 是否存在，例如 `pi --version`。
- runner 是否可安装或调用。
- terminal backend 是否存在；缺失时按 optional 标记。
- provider endpoint 是否配置完整：`base_url`、必要的 `provider_api`、是否有 key。

server 不选择 model，不做模型语义测试。若 provider 支持 model-free ping，可作为 endpoint connectivity check；否则 endpoint 的真实模型调用由 dispatcher startup healthcheck 负责。

### dispatcher startup healthcheck

dispatcher 检查完整矩阵：

- environment 是否可用。
- worker 是否允许该 environment。
- worker.endpoint 是否存在于该 environment。
- model_profile 是否完整。
- endpoint key 是否存在且不会被日志打印。
- worker + model_profile + endpoint + environment 是否能执行模型 ping。

模型失败应归因到 worker/model_profile/endpoint/environment 的具体组合，不应污染单纯地点健康。

## 8. 调度规则

project 创建时选择 `environment_id`。

dispatcher 执行时：

1. 读显式传入的 dispatcher config，解析 runtime、workers、model_profiles。
2. 从 server 拉取 environments 与 redacted endpoint metadata。
3. 在需要执行 worker 时，按受控路径拉取该 environment 的 endpoint secret。
4. 校验 `allowed_environments` 引用存在于 server environments 或内置 environments。
5. 对每个 project，按 `project.environment_id` 选择 environment。
6. 从允许该 environment 且 endpoint 存在的 workers 中选择 worker。
7. 用 worker.model_profile + environment.provider_endpoint 构造 agent 命令与注入环境变量。

若 project 绑定的 environment 不存在，dispatcher 不回退默认环境；应跳过并明确日志。

若 worker 引用的 endpoint 在目标 environment 不存在，该 worker 对该 environment 不可用；不得静默改用其它 endpoint。

dispatcher 必须支持 environment 与 endpoint 热刷新：

- 新增 endpoint 应进入后续调度与 startup healthcheck 矩阵。
- 修改 endpoint 应对后续新任务生效；运行中 task 使用启动时解析出的 endpoint，不被中途替换。
- 删除 endpoint 后，新任务跳过引用它的 worker/environment 组合。
- endpoint healthcheck 更新不应导致 backend 对象无意义重建。

## 9. 配置形态

dispatcher config 示例：

```yaml
server: "http://127.0.0.1:8000"

runtime:
  interval: 3
  max_workers: 4
  max_running_projects: 2
  max_project_workers: 2
  healthcheck_timeout: 10
  prompt_group: "default"

model_profiles:
  - id: pi-gpt54
    type: pi
    model: gpt-5.4
    context_window: 262144

workers:
  - name: pi-worker-1
    type: pi
    model_profile: pi-gpt54
    endpoint: pi-default
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
    allowed_environments: [pentestvm]
```

server environment 示例：

```json
{
  "id": "pentestvm",
  "label": "pentestVM over SSH",
  "backend": "ssh",
  "ssh_command": "ssh -F /tmp/cairn_pentestvm_ssh_config cairn-pentestvm",
  "workspace_root": "/home/kali/cairn-workspaces",
  "harness": "pi",
  "cleanup": {"completed_action": "stop"},
  "terminal": {"mode": "none"},
  "provider_endpoints": [
    {
      "id": "pi-default",
      "type": "pi",
      "base_url": "http://10.0.0.44:3000/v1",
      "provider_api": "openai-completions",
      "api_key": "<api-key>"
    }
  ]
}
```

redacted API response 示例：

```json
{
  "id": "pi-default",
  "type": "pi",
  "base_url": "http://10.0.0.44:3000/v1",
  "provider_api": "openai-completions",
  "has_api_key": true,
  "api_key_preview": "sk-...IoX9y"
}
```

## 10. v1.1 改造清单

- server DB 增加 environment provider endpoint 存储。
- server models/API/UI 增加 endpoint CRUD 或 environment 内嵌 endpoint 编辑。
- API 默认 redacted；dispatcher 专用读取路径可取 secret。
- dispatcher config 将 `profiles[]` 改为 `model_profiles[]`。
- worker 将 `profile` 改为 `model_profile`，新增 `endpoint`。
- 删除 dispatcher profile 中的 `base_url`、`provider_api`、`api_key`。
- 修改 profile resolution：`model_profile + provider_endpoint -> driver env`。
- Pi/Codex/Claude driver 不直接读取 worker profile endpoint 字段；只接收 resolved env。
- startup healthcheck report 包含 environment、worker、model_profile、endpoint。
- run log metadata 可记录 endpoint id、base_url redacted/host 信息；不得记录 key。
- UI Environment panel 显示与编辑 endpoint；key input 默认空且不会回显。
- Project detail 可显示 environment 与 workspace；不显示 endpoint key。
- 更新 `dispatch.example.yaml`、`dispatch.dev.yaml`、`dispatch.local.yaml` 到 v1.1 schema。
- 更新 docs/README/docker-compose 中对 config schema 的引用。

## 11. 验收标准

- server DB 不再有 `environment_snapshot_json`。
- server environment 中可保存 provider endpoint，并能 redacted 返回。
- 普通 API/UI 不返回 API key 明文。
- dispatcher config 中不存在 `profiles[].api_key`、`profiles[].base_url`、`profiles[].provider_api`。
- dispatcher 能用 server endpoint + dispatcher model_profile 跑 Pi worker。
- 同一 environment 可配置多个 endpoint，多个 worker 可引用不同 endpoint。
- 同一 worker 在不同 environment 中使用同名 endpoint，但具体 base_url/key 可不同。
- endpoint 缺失时 worker/environment 组合不可用，不回退其它 endpoint。
- run log、healthcheck、error preview 不包含 API key 明文。
- `dispatch.dev.yaml` 与 `dispatch.local.yaml` 被 gitignore 排除。

## 12. 非目标

- 不做 snapshot。
- 不兼容 v1 临时 schema。
- 不做多用户 RBAC。
- 不做远端 agent secret manager。
- 不做完整 Vault/keychain 集成；可作为后续 secret backend。
- 不做多 host 负载均衡。
- 不做 terminal 交互 UI；tmux/zellij 可作为后续增强。
