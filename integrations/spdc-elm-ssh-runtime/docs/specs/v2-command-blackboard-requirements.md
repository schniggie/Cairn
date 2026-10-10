# Cairn Command Blackboard Requirements v2

日期：2026-05-18

状态：需求草案。v2 建立在 v1.1 worker environment 与 endpoint 边界之上，核心目标是把 Cairn 从“AI 自动决策并执行”调整为“人类或可选 reason agent 决策；worker 只执行 intent；黑板承载简洁事实与可追溯执行报告”。

## 1. 核心判断

Cairn v2 的边界应是：

- server 管任务图、黑板元数据、项目级控制面、用户可见调度偏好。
- dispatcher 管调度循环、worker 选择、运行状态、timeout 与收尾。
- worker 只执行被分配的 intent，并写回执行报告与简短 fact；worker 不自行决定下一步。

关键修正：

- `reason` 不再默认自动运行。项目创建时默认 `auto_reason = false`，用户可显式开启。
- 人类可以完全接管决策，手动创建、修改、撤回 intent。
- 若 `auto_reason = true`，reason agent 才可生成 intent；但执行 worker 仍只执行 intent。
- 黑板 fact 保持简短；详细执行过程落盘为 report，并由 fact metadata 引用。
- 用户在 WebUI 中选择的是具体 `worker.name`，例如 `pi-GPT5.5`，而不是粗粒度 worker type。

## 2. v1.1 已有基础

已有基础：

- project 已绑定 execution environment。
- server 已管理 environment 与 provider endpoint。
- dispatcher worker 已有 `name`、`type`、`model_profile`、`endpoint`、`task_types`、`max_running`、`priority`、`allowed_environments`。
- dispatcher 已按 environment、endpoint、task type、worker 并发与健康状态筛选 worker。
- SSH backend 已有 workspace 语义，并向进程注入 `CAIRN_WORKSPACE`。
- graph YAML snapshot 已可作为一键复制或 prompt context 基础。

v2 需要补齐：

- Docker backend 也必须有 workspace 语义。
- Fact 需要 metadata，以记录 report path 等机器可读信息。
- Intent 需要表达人工指定 worker、timeout override、request conclude now 等控制面。
- Project 需要表达 `auto_reason`、自动 worker 可选范围、默认 timeout 等项目级偏好。
- WebUI 需要暴露 project 创建时的 worker 范围选择、人工 intent 指派、timeout 与收尾控制。

## 3. 决策与执行分离

### 决策者

决策者可以是：

- 人类 commander。
- 可选开启的 reason agent。

决策者负责：

- 读取黑板状态。
- 判断下一步应探索什么。
- 创建或修改 intent。
- 决定是否重跑、撤回、或请求正在执行的 intent 进入收尾。

### 执行者

执行 worker 负责：

- 接收一个明确 intent。
- 在项目 workspace 中执行。
- 写执行报告。
- 返回一个简短 fact description。

执行 worker 不负责：

- 生成新的普通 intent。
- 选择下一个 worker。
- 判断整个项目后续战略。

例外：`bootstrap` 与 `reason` 是决策类任务，不应与普通 `explore` worker 的执行职责混同。v2 默认不自动运行 reason；若启用 reason，它生成的 intent 仍需进入普通调度流程。

## 4. Project 配置

project 新增或扩展字段：

- `auto_reason: bool`
- `allowed_auto_workers: list[str] | null`
- `default_timeout_seconds: int | null`
- `default_conclude_timeout_seconds: int | null`

语义：

- `auto_reason = false` 是默认值。
- `allowed_auto_workers` 是自动 reason / dispatcher 在自动生成 intent 时可用的 worker name 范围。
- `allowed_auto_workers = null` 表示使用 dispatcher 配置中对该 environment 可用的全部 worker。
- `allowed_auto_workers = []` 表示自动 agent 不可派发执行 worker；通常只用于完全人工模式或暂停自动执行。
- 人工 intent 指定 worker 时，不受 `allowed_auto_workers` 限制。

项目创建 UI 必须允许选择：

- environment。
- 是否开启 `auto_reason`。
- 自动可用 worker names。
- project 默认 timeout。

## 5. Worker 可见性与选择

用户可见 worker 以 dispatcher config 中的 `worker.name` 为准。

示例：

```yaml
workers:
  - name: pi-GPT5.4
    type: pi
    task_types: [bootstrap, reason, explore]
    max_running: 2
    priority: 0
    model_profile: pi-GPT5.4
    endpoint: pi-default

  - name: pi-GPT5.5
    type: pi
    task_types: [bootstrap, reason, explore]
    max_running: 2
    priority: 0
    model_profile: pi-GPT5.5
    endpoint: pi-default
```

语义：

- `worker.name` 是 UI 展示与人工指定的稳定名称。
- `max_running` 表示同一 worker name 的并行容量。
- 同一 worker name 下可以同时运行多个 intent，直到达到 `max_running`。
- 若人工指定的 worker 暂时无空闲容量，intent 保持 pending，不 fallback 到其它 worker。
- 若人工指定的 worker 不存在、不可用于 project environment、endpoint 缺失或不支持该 task type，server 或 dispatcher 必须给出明确错误或 blocked reason。

WebUI 应从 dispatcher 或 server 暴露的 worker inventory 读取可选 worker，而不是让用户手写名称。

## 6. Intent 模型

intent 新增或扩展字段：

- `requested_worker: str | null`
- `timeout_override_seconds: int | null`
- `conclude_timeout_override_seconds: int | null`
- `control_state: "normal" | "conclude_requested" | "abort_requested"`
- `control_requested_at: str | null`
- `control_requested_by: str | null`

语义：

- `requested_worker` 是人工指定的 worker name。
- `requested_worker = null` 表示 dispatcher 可按项目自动 worker 范围与调度规则选择。
- `timeout_override_seconds` 优先于 project 默认 timeout 与 dispatcher 默认 timeout。
- `conclude_requested` 表示用户请求当前 intent 尽快进入收尾。
- `abort_requested` 是后续可选能力；v2 第一版可不实现，但字段语义应预留。

人工 intent 可被撤回或修改，条件是：

- intent 尚未 concluded。
- intent 未处于正在收尾写回的不可中断阶段。

若 intent 已运行：

- 修改 `requested_worker` 不影响当前运行中的 task。
- 用户可请求 `conclude_now`。
- 若要换 worker 重跑，应先等待当前任务释放或由后续 retry 创建新 run。

## 7. Timeout 与收尾

有效 timeout 按以下优先级解析：

```text
intent.timeout_override_seconds
> project.default_timeout_seconds
> dispatcher task default
```

有效 conclude timeout 按以下优先级解析：

```text
intent.conclude_timeout_override_seconds
> project.default_conclude_timeout_seconds
> dispatcher task conclude default
```

`request_conclude_now` 语义：

- 不等同于失败。
- dispatcher 向当前 worker 进程发 cancel。
- dispatcher 进入 timeout fallback / conclude prompt。
- fallback 应尽量基于同一 workspace、已有 report 草稿、运行日志与现场文件进行收尾。

若 fallback 也失败或超时：

- dispatcher 写入失败 report。
- intent 回到 pending。
- 人类决定重跑、修改或撤回。

失败 report 至少应说明：

- intent id。
- worker name。
- run id。
- 触发原因：timeout、manual conclude、worker failure、parse failure 等。
- 已知进展。
- 未能完成的原因。
- 可供重试的线索。

## 8. Workspace 协议

所有 backend 都必须提供 project workspace。

通用要求：

- `EnvironmentHandle.workspace` 必须为非空绝对路径。
- worker 进程必须收到 `CAIRN_WORKSPACE`。
- graph snapshot、report、run state、worker 私有配置都应放在 workspace 下。
- dispatcher 不得让 worker report 写到 workspace 外。

推荐路径：

```text
$CAIRN_WORKSPACE/.cairn/prompts/{phase}/graph.yaml
$CAIRN_WORKSPACE/.cairn/reports/execution-{intent_id}.md
$CAIRN_WORKSPACE/.cairn/runs/{run_id}/
```

SSH backend：

```text
{environment.workspace_root}/{project_id}
```

Docker backend：

```text
/home/kali/workspace/.cairn/projects/{project_id}
```

Docker 容器仍可保留 `/home/kali/workspace` 作为工作目录，但 v2 任务输入、报告与状态文件必须落在项目 workspace 子目录，避免多个 project 或 startup probe 混用路径。

## 9. Execution Report 协议

每次执行 intent 必须产生 report 文件。

标准路径：

```text
$CAIRN_WORKSPACE/.cairn/reports/execution-{intent_id}.md
```

若同一 intent 多次运行，可使用 run id 防覆盖：

```text
$CAIRN_WORKSPACE/.cairn/reports/execution-{intent_id}-{run_id}.md
```

最终 fact metadata 应记录实际路径。

报告建议结构：

```markdown
# Execution Report

## Intent

## Summary

## Steps

## Evidence

## Artifacts

## Failures And Dead Ends

## Uncertainty

## Suggested Follow-up Questions
```

worker prompt 必须明确：

- 先尽量写 report，再返回结构化输出。
- 即使失败、超时收尾或只完成部分探索，也要写 report。
- fact description 应简短，只包含黑板可扫描结论。
- 细节、命令、证据、长日志路径、失败尝试写入 report，不塞进 fact description。

## 10. Fact Metadata

Fact 保持黑板节点的简洁性。

fact 新增：

- `metadata_json: dict | null`

v2 最低 metadata：

```json
{
  "report_path": "/home/kali/cairn-workspaces/proj_001/.cairn/reports/execution-i003-run_abc.md",
  "run_id": "run_abc",
  "worker": "pi-GPT5.5",
  "intent_id": "i003"
}
```

要求：

- `report_path` 必须是 workspace 内绝对路径。
- dispatcher conclude 前必须检查 report 存在。
- 若 report 缺失，dispatcher 应尝试 fallback 补写。
- 若最终仍缺失，dispatcher 写失败 report，并不得伪造成功 fact。
- UI 可先只显示 fact description；后续可点击 fact 预览 report。

## 11. Blackboard UI

WebUI v2 需要支持：

- project 创建时选择 `auto_reason`。
- project 创建时选择自动可用 worker names。
- project 创建或详情页设置默认 timeout。
- 人工创建 intent 时指定 `requested_worker`。
- 人工创建 intent 时指定 timeout override。
- pending intent 可撤回、修改。
- running intent 可请求 `conclude_now`。
- fact 可展示 metadata 中的 report path。
- worker 不可用时展示 blocked reason：busy、unhealthy、environment blocked、endpoint missing、task type unsupported。

非首版强制：

- 从 WebUI 直接预览远端 report。
- attach 到远端 terminal。
- 富交互式案件组看板。

这些属于展示增强，不改变 v2 协议。

## 12. Dispatcher 动态配置

v1.1 仍大量依赖 YAML。v2 应把“用户运行时需要调节的项目级策略”移入 server DB 与 WebUI。

应进入 server/project 的配置：

- `auto_reason`
- `allowed_auto_workers`
- `default_timeout_seconds`
- `default_conclude_timeout_seconds`
- intent-level timeout override
- intent-level requested worker
- `request_conclude_now`

仍留在 dispatcher YAML 的配置：

- server URL。
- worker 定义。
- model profiles。
- endpoint id 引用。
- worker priority。
- worker max_running。
- prompt group。
- dispatcher hard safety defaults。

理由：

- worker 定义与模型接入属于部署配置。
- project 与 intent 策略属于用户运行时控制面。
- max_workers 等全局 dispatcher 并发上限后续可暴露为 admin setting，但 v2 第一版不要求，否则会过早引入多用户与资源治理问题。

## 13. 调度规则

dispatcher 对 explore intent 的选择顺序：

1. 只选择 open、未 concluded、未被本地 task 运行的 intent。
2. 若 intent 有 `requested_worker`，只考虑该 worker。
3. 若 intent 无 `requested_worker`：
   - 若 intent 由 auto reason 生成，限制在 project `allowed_auto_workers` 内。
   - 若 intent 由人类创建且未指定 worker，可使用 project `allowed_auto_workers`；若为空，则按全部可用 worker。
4. 过滤 environment、endpoint、task type、max_running、health、rejected backoff。
5. 若无候选 worker，intent 保持 pending，并记录 blocked reason。
6. 不因人工指定 worker 繁忙而 fallback 到其它 worker。

reason 调度：

- 仅当 `project.auto_reason = true` 时运行。
- reason worker 自身也应受 project `allowed_auto_workers` 或独立 `allowed_reason_workers` 约束；v2 第一版可复用 `allowed_auto_workers`。
- reason 生成的 intent 默认不带 `requested_worker`。

bootstrap 调度：

- bootstrap 在完全人工模式下可关闭或显式手动触发。
- 若保留自动 bootstrap，应受 `auto_reason` 或独立 `auto_bootstrap` 开关控制。
- v2 第一版可保留当前 bootstrap 行为，但 spec 应标记为待定兼容点。

## 14. 交互提问

针对已有 fact 或 intent 的追问，可复用 timeout fallback / resume 能力。

目标语义：

- 用户可针对 fact/report 提问。
- 系统尽量唤起当时 worker 或同类 worker。
- worker 获得原 project graph、report path、workspace 与相关 run context。
- 追问结果可写为新 report、hint、fact 或 comment；首版可先写 hint/fact。

非目标：

- 首版不要求完整聊天式 attach。
- 首版不要求 worker 保持长期会话常驻。
- 一键复制 context 可继续使用 graph YAML snapshot，不必新建协议。

## 15. API 草案

项目创建：

```json
{
  "title": "case",
  "origin": "...",
  "goal": "...",
  "environment_id": "pentestvm",
  "auto_reason": false,
  "allowed_auto_workers": ["pi-GPT5.5"],
  "default_timeout_seconds": 900,
  "default_conclude_timeout_seconds": 120
}
```

创建 intent：

```json
{
  "from": ["f003"],
  "description": "验证 redis 是否可写 cron",
  "creator": "Human",
  "requested_worker": "pi-GPT5.5",
  "timeout_override_seconds": 600
}
```

请求收尾：

```http
POST /projects/{project_id}/intents/{intent_id}/request-conclude
```

请求体：

```json
{
  "actor": "Human",
  "reason": "已有足够线索，先收尾"
}
```

修改 pending intent：

```http
PATCH /projects/{project_id}/intents/{intent_id}
```

撤回 pending intent：

```http
DELETE /projects/{project_id}/intents/{intent_id}
```

worker inventory：

```http
GET /dispatcher/workers
```

或由 server 暴露 dispatcher 上报的缓存：

```http
GET /workers
```

首版可选择更简单路径，但 UI 不应硬编码 worker names。

## 16. 验收标准

- 新项目默认 `auto_reason = false`。
- 用户可创建完全人工决策项目，并手动创建 intent。
- 普通执行 worker 不会生成新 intent。
- WebUI 可在 project 创建时选择自动可用 worker names。
- WebUI 可在人工 intent 中指定 `requested_worker`。
- 人工指定 worker 繁忙时，intent 保持 pending，不 fallback。
- pending intent 可撤回或修改。
- running intent 可 `request_conclude_now`，并进入 fallback 收尾。
- fallback 失败后，系统写失败 report，intent 回到 pending。
- Fact 有 `metadata_json.report_path`。
- dispatcher 在写成功 fact 前检查 report 文件存在且位于 workspace 内。
- SSH 与 Docker backend 都提供 `CAIRN_WORKSPACE`。
- Docker report 路径位于 project workspace 子目录。
- project/intent timeout override 按规定优先级生效。
- run log 和 UI 能说明 worker blocked reason。

## 17. 非目标

- 不做多用户 RBAC。
- 不做完整 admin 资源治理面板。
- 不要求首版从 WebUI 直接读取远端 report 内容。
- 不要求 terminal attach。
- 不要求 worker 长期会话常驻。
- 不要求 agent 自动问答编排系统。
- 不把 report 内容塞入 fact description。
- 不让执行 worker 自行派生普通 intent。
