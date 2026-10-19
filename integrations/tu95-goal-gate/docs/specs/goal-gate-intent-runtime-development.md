# Cairn Goal Gate 与 Intent Runtime 开发规格

> 状态：代码已实现；自动化、HTTP 端到端与 Compose Docker 验证通过；前端浏览器人工验收和固定测评待完成
>
> 目标读者：负责实现本方案的开发者或代码 Agent
>
> 基线：`main` 分支，DeepSeek V4 Flash，安全能力测评 `18550 / 23000`
>
> 本文原为开发方案，当前实现已经落地。截至 2026-08-10，共 144 项自动化测试通过；Python 编译、内联 JavaScript 语法、实际配置加载、Compose 配置、HTTP 主链路、Compose 镜像构建与健康启动均已验证。尚未运行真实浏览器交互和新分支固定测评。

## 1. 文档目的

本次改造解决两个已经在实际运行中暴露的问题：

1. 用户输入一提交，项目就直接进入 `active`，Dispatcher 会立即开始消耗模型和执行资源；即使缺少目标地址、必要文件、成功标准或时间限制，也会先运行再失败。
2. Explore 到达 300 秒超时后，被迫把尚未完成的工作总结成 Fact；Reason 再创建新 Intent 接力，导致同一任务被切碎、上下文丢失、Fact 污染和重复调度。

改造后的主链路为：

```text
用户输入
  -> Goal Gate（先判断是否具备启动条件）
     -> preparing
     -> waiting_input（最多提出 1～3 个阻塞问题，问答轮次有上限）
     -> intake_failed（失败耗尽；只能由用户 Retry、补充上下文或强制激活）
     -> active
  -> Fact / Intent Knowledge Plane
  -> IntentRun Runtime Plane
  -> Dispatcher / Worker / Tools
  -> fact | continue | rejected/error
```

本方案不替换 Cairn 的 Fact–Intent 图。Fact、Intent 和它们之间的因果关系仍然是系统最重要的知识表达；新增内容只负责进入知识图之前的输入审查，以及知识图之外的执行生命周期。

## 2. 改造前实现依据（main 基线）

以下内容是制定方案时对 `main` 基线代码和协议的事实快照，不代表当前实验分支的实现状态：

1. `POST /projects` 当前直接写入 `status='active'`，并立即创建 `origin`、`goal` 两个 Fact：`cairn/src/cairn/server/routers/projects.py`。
2. 项目状态当前只有 `active | stopped | completed`：`cairn/src/cairn/server/models.py`。
3. Dispatcher 当前只调度 `active` 项目：`cairn/src/cairn/dispatcher/scheduler/loop.py`。
4. Explore 输出当前只允许 `description`，校验后固定解释为 Fact：`cairn/src/cairn/dispatcher/contracts.py`。
5. Explore 超时后使用同一个 Worker session 执行 conclude，但 conclude 结束后 session 不持久化：`cairn/src/cairn/dispatcher/tasks/explore.py`。
6. Claude Code、Codex 和 Pi 的 Worker Adapter 已经具备 session 恢复能力，但目前只用于 conclude：`cairn/src/cairn/dispatcher/workers/adapters/`。
7. 前端当前在 New Project 表单提交后直接进入项目图；轮询周期为 5 秒，且只认识三种项目状态：`cairn/src/cairn/server/static/index.html`。
8. 现有协议明确规定 Server 是协议真相源，推理和执行由消费者完成。因此 Goal Gate 的模型调用仍应由 Dispatcher 执行，Server 只保存状态和验证状态转换。
9. `expire_workers` 在 `GET /projects`、`GET /projects/{id}` 和 export 等读取路径上执行，仅依据 `intents.last_heartbeat_at` 清空过期 worker：`cairn/src/cairn/server/services.py`、`cairn/src/cairn/server/routers/projects.py`、`cairn/src/cairn/server/routers/export.py`。
10. Bootstrap 已使用保留 Intent（`creator=dispatcher.bootstrap`、`from=["origin"]`）并通过旧 Intent claim 接口认领：`cairn/src/cairn/dispatcher/scheduler/loop.py`、`cairn/src/cairn/dispatcher/tasks/bootstrap.py`。

## 3. 实际解决的问题

| 当前问题 | 直接原因 | 本次改动 | 解决后的行为 |
| --- | --- | --- | --- |
| 缺目标地址或文件仍然启动 | 创建项目时直接 `active` | Goal Gate + `preparing/waiting_input` | 信息不足时不创建知识图、不启动 Reason/Explore |
| 用户不知道技术细节时被迫提供 Hint | 输入和执行没有区分“用户独有信息”与“可自行发现信息” | Goal Gate 问题约束 | 只问用户独有且真正阻塞的问题；可调查内容写入 `unknowns` |
| 300 秒后任务被截断 | timeout 被当作任务终点 | IntentRun + `continue` | timeout 变成时间片终点，下次恢复同一个 Intent |
| 未完成进度进入 Fact | Explore 只有 Fact/拒绝两种结果 | `fact | continue | rejected/error` 联合结果 | 只有确认结论能进入 Fact |
| Reason 反复为同一工作创建接力 Intent | `continue` 只能通过新 Fact 表达 | `continue` 不写图、不触发 Reason | 同一工作保持同一个 Intent id |
| Worker session 只用于收尾 | session 没有持久化位置 | IntentRun 保存 session 和 checkpoint | Dispatcher 重启或重新调度后可恢复 |
| 前端无法表示“AI 在问用户” | 没有等待输入状态和问答界面 | 新项目状态、问题卡片、答案提交 | 项目卡片和详情页明确显示“等待输入” |
| 运行日志和临时计划稀释知识图 | 执行状态和知识状态混在一起 | Knowledge Plane / Runtime Plane 分离 | 运行进度留在 IntentRun，Fact 保持客观结论 |
| Goal Gate 自身失控（无限重试、反复追问） | 没有技术失败上限和问答轮次上限 | `max_intake_attempts` + `max_intake_clarification_rounds` + `intake_failed` + 人工强制激活 | 失败有界；用户始终可以 Retry、补充回答或强制激活 |
| Goal Gate 就绪后直接激活，用户没有过目最终 origin/goal 的机会 | ready 与 active 之间没有确认环节 | `ready_review` + 启动确认卡（可编辑）+ `auto_activate` 旁路 | 启动前用户总能确认或修改；测评脚本可用 auto_activate 保持自动 |

本次改动不能解决以下问题，开发时不得声称已经解决：

1. 外部目标自身的过期或重置，例如测评环境 8 分钟实例有效期。
2. 任意 `setsid/nohup` 进程的确定性追踪。当前 Worker 在 Cairn 应用容器内以进程模式运行；第一版只要求 Agent 使用可恢复文件并避免未管理的脱离进程。
3. 模型本身缺少领域知识或拒绝执行的情况。
4. 完整图增长后的全局上下文压缩。本方案为后续图投影提供了干净数据，但第一版不实现语义检索或复杂摘要。
5. Bootstrap 的 timeout 截断和强制 conclude 语义。本轮固定测评关闭 Bootstrap，因此明确裁掉 Bootstrap 接入 IntentRun；它继续使用现有保留 Intent 和旧 claim 接口。
6. 多租户鉴权、凭证隔离或恶意用户/恶意模型对抗。本方案按单用户、可信 Docker 运行环境设计。

## 4. 设计原则与不变量

### 4.1 保持不变

1. Fact/Intent 的核心字段和因果语义不变。
2. Reason 继续负责判断完成或创建 Intent。
3. Explore 继续只处理一个指定 Intent。
4. Dispatcher 继续是唯一的协议写入者和控制面。
5. Server 继续只维护协议一致性，不自行调用模型。
6. 多个 Explore 继续异步运行；不增加“等待全部 Dispatcher 完成”的全局屏障。
7. 不拆高级模型和低级模型；`intake`、`reason`、`explore` 可以使用同一模型。
8. 不增加 Plan、Step、Workstream 等知识节点。

### 4.2 新增不变量

1. `preparing` 和 `waiting_input` 项目不得创建普通 Intent，不得执行 bootstrap、reason 或 explore。
2. 项目只有经 Goal Gate `ready` 且用户确认（confirm）、用户强制激活（force）或项目显式开启 `auto_activate` 三者之一，才能在一个数据库事务中创建 `origin`、`goal` 并进入 `active`。
3. `waiting_input` 必须有 1～3 个尚未回答的问题。
4. `active` 项目必须已经存在 `origin` 和 `goal`。
5. 一个开放 Intent 最多只有一个未结束 IntentRun。
6. `continue` 不创建 Fact、不关闭 Intent、不触发 Reason。
7. `fact` 必须原子化地关闭 Intent、创建 Fact，并结束 IntentRun。
8. 同一个 IntentRun 同时只能由一个 Worker claim。
9. timeout 只结束本次执行时间片，不自动代表 Intent 完成。
10. 没有可验证进展的 IntentRun 不得无限续期。
11. `intents.worker` 与 `intents.last_heartbeat_at` 是唯一租约真相源；不得为 IntentRun 引入第二套超时时钟；过期处理只在 `expire_workers` 一处，并在同一事务中把 running Run 翻转为 yielded。
12. 任何时刻不得存在 `intents.worker IS NULL` 而对应 IntentRun 仍为 running 的记录。
13. Goal Gate 的技术失败次数（按 revision 计）与问答轮次均有上限；达到上限进入 `intake_failed` 并停止自动调度。
14. 强制激活只能由用户显式触发并二次确认，系统不得自动兜底。
15. 进展只由累计后的可验证证据（artifact 哈希、严格增长的 cursor、新 milestone）判定；模型散文、旧证据的省略或重排不参与。
16. IntentRun 技术失败的新 session 重试必须有上限；耗尽后保持 terminal `failed`，不得因 Dispatcher 重启恢复自动重试。
17. `ready_review` 项目必须存在待确认 proposal；confirm、force、编辑重审等任何离开 `ready_review` 的转换都必须清空它。

### 4.3 信任边界

1. Cairn 按单用户、可信本地运行环境设计；Server、Dispatcher 和 Worker 运行在受控 Docker 容器中。
2. 本次不实现 API 鉴权、多租户数据隔离、恶意模型对抗或额外的每任务安全容器。
3. Server 信任 Dispatcher 由实际 workspace 文件计算的 artifact 元数据；这是部署前提，不是针对恶意输入的安全边界。

## 5. 总体 Module 设计

本次新增两个深 Module；调用者只通过各自的 Interface 使用它们，不应在 Router、Dispatcher 和前端分别复制状态判断。

### 5.1 Project Intake Module

职责：

1. 创建不可执行的待审查项目。
2. 管理 Goal Gate claim、心跳、结果提交和失败释放。
3. 管理阻塞问题、用户回答和 intake revision。
4. 在 `ready` 时原子化激活项目并创建 Origin/Goal。

建议位置：

```text
cairn/src/cairn/server/intake.py
cairn/src/cairn/server/routers/intake.py
cairn/src/cairn/dispatcher/tasks/intake.py
```

Server 侧的状态转换和数据库事务集中在 `server/intake.py`。Router 只做 HTTP Interface 适配；Dispatcher 不允许自行拼 SQL 状态。

### 5.2 Intent Runtime Module

职责：

1. 创建、claim、heartbeat、yield、resume 和结束 IntentRun。
2. 持久化 Worker session、checkpoint、累计证据和有界重试状态。
3. 保证 Intent claim 与 IntentRun 状态一致。
4. 对外提供一个小的 Runtime Interface，隐藏恢复和无进展判断的实现。

建议位置：

```text
cairn/src/cairn/server/intent_runtime.py
cairn/src/cairn/server/routers/intent_runs.py
cairn/src/cairn/dispatcher/runtime/intent_run.py
```

Intent Runtime 不属于知识图。删除这个 Module 后，session、checkpoint、重试和状态一致性会重新散落到 Scheduler、Explore task 和 Worker Adapter 中，因此这个 Module 能通过 deletion test，具有足够 Depth。

## 6. Goal Gate 详细设计

### 6.1 Project 状态机

项目状态扩展为：

```text
preparing | waiting_input | ready_review | intake_failed | active | stopped | completed
```

状态语义：

| 状态 | 含义 | Dispatcher 行为 | 前端行为 |
| --- | --- | --- | --- |
| `preparing` | 等待或正在执行 Goal Gate | 只允许调度 `intake` | 显示“AI 正在检查输入” |
| `waiting_input` | Goal Gate 需要用户补充 1～3 个阻塞参数 | 不调度任何模型任务 | 显示问题卡片和回答表单 |
| `ready_review` | Goal Gate 已就绪，等待用户确认启动 | 不调度任何模型任务 | 显示启动确认卡（origin/goal 可编辑） |
| `intake_failed` | 技术失败或问答轮次达到上限，自动调度停止 | 不调度任何模型任务 | 显示清理后的错误摘要、Retry 和 Start Anyway |
| `active` | Goal 已确认，知识图可运行 | 现有 bootstrap/reason/explore 流程 | 显示项目图 |
| `stopped` | 用户硬停止运行项目 | 现有停止与清理流程 | 显示停止状态 |
| `completed` | Goal 已完成 | 现有完成流程 | 显示完成状态 |

允许的状态转换：

```text
POST /projects
    -> preparing

preparing
    -> waiting_input   Goal Gate 返回 questions，且问答轮次未达上限
    -> ready_review    Goal Gate 返回 ready，且项目未开启 auto_activate
    -> active          Goal Gate 返回 ready，且项目开启 auto_activate
    -> preparing       技术失败后释放，等待重试，且技术失败次数未达上限
    -> intake_failed   技术失败次数或问答轮次达到上限

waiting_input
    -> preparing       用户提交本轮答案或 Additional context
    -> active          用户强制激活（force 接口，人工二次确认）

ready_review
    -> active          用户确认 proposal（confirm 接口）或强制激活（force 接口）
    -> preparing       用户编辑 origin/goal 后提交，revision+1 重新审查

intake_failed
    -> preparing       用户 Retry，或提交非空 Additional context
    -> active          用户强制激活（force 接口，人工二次确认）

active
    -> stopped
    -> completed

stopped
    -> active

completed
    -> active          复用现有 reopen 流程
```

明确禁止：

1. `waiting_input/ready_review/intake_failed -> active` 由前端直接改状态触发；`ready_review` 只能经 confirm 或 force，其余只能经 force，且请求必须显式确认。
2. `preparing/waiting_input/ready_review/intake_failed -> stopped`。这些状态没有执行任务，不需要 Stop；用户可以删除项目。
3. `preparing/waiting_input/ready_review/intake_failed` 调用 Intent、Reason、Complete、Hint、Export 写接口；Export 读接口对这些状态返回 `409 Project graph is not ready`，不得导出 Origin/Goal 为空的图。
4. 系统自动强制激活；`auto_activate` 只能由创建请求或 Server 设置显式开启，不得默认对所有项目启用。

### 6.2 创建项目行为

保留现有创建表单字段，减少无关改动：

```json
{
  "title": "xx渗透测试",
  "origin": "目标是公司提供的测试环境",
  "goal": "完成安全能力测评",
  "bootstrap_enabled": true,
  "auto_activate": null,
  "hints": []
}
```

`POST /projects` 的行为改为：

1. 创建 Project，状态为 `preparing`。
2. 创建 `project_intakes` 记录，保存原始 origin、goal、hints 和 `auto_activate`。
3. 不创建 `facts.origin`、`facts.goal`。
4. 不立即写入 hints 表；原始 hints 暂存在 intake 中，项目激活时再写入。
5. `auto_activate` 可空；为 null 时跟随 Server 设置 `intake_auto_activate`（默认 false）。开启后 Goal Gate ready 直接激活、不经过 `ready_review`，主要供固定测评等脚本批量创建使用。
6. 返回 `ProjectDetail`，其中 `project.status='preparing'`、`facts=[]`、`intents=[]`、`hints=[]`。

这是有意的协议行为变化。所有旧调用者必须能够处理创建响应中暂时没有 Origin/Goal 的情况。

### 6.3 GoalSpec

Goal Gate 返回的结构化 GoalSpec：

```json
{
  "objective": "完成授权安全能力测评并提交可验证结果",
  "success_criteria": [
    "在规定范围内尽可能完成测评项",
    "结果已在目标系统中验证或提交"
  ],
  "resources": [
    "测评入口 http://example.test"
  ],
  "constraints": [
    "仅允许测试给定目标",
    "目标实例接入后 8 分钟失效"
  ],
  "unknowns": [
    "具体攻击面需要 Agent 自行探测"
  ]
}
```

Goal Gate 的 `ready` 结果还必须给出供现有知识图使用的两个文本：

```json
{
  "accepted": true,
  "data": {
    "ready": {
      "origin": "已提供授权测评入口……；已知约束……",
      "goal": "完成授权安全能力测评；成功标准为……",
      "spec": {
        "objective": "...",
        "success_criteria": ["..."],
        "resources": ["..."],
        "constraints": ["..."],
        "unknowns": ["..."]
      }
    }
  }
}
```

`ready.origin` 和 `ready.goal` 写入现有特殊 Fact；`spec` 保存在 intake 记录中供 UI 和审计使用，不传入 Fact/Intent schema。

### 6.4 阻塞问题规则

Goal Gate 只有在缺少“用户独有且无法通过后续探索获得”的信息时才能提问。

可以提问：

1. 实际目标地址、仓库、文件或设备在哪里。
2. 成功标准是什么，例如只要报告还是需要实际完成操作。
3. 授权范围、禁止触碰的目标或账户边界。
4. 只有用户知道的凭证、接入方式或硬性时间限制。

不得提问：

1. “你希望使用什么技术方案”。
2. “你觉得漏洞在哪里”。
3. 可以通过读取代码、文件、目标或联网检索得到的信息。
4. 风格偏好、输出格式等不阻塞第一步的问题。
5. 只是为了让 GoalSpec 更完整、但不影响执行的问题。

问题输出契约：

```json
{
  "accepted": true,
  "data": {
    "questions": [
      {
        "key": "resources.target_url",
        "question": "授权测评目标的访问地址是什么？",
        "why_blocking": "没有目标入口无法进行任何有效探测"
      }
    ]
  }
}
```

每个问题必须携带稳定的阻塞字段 `key`，Server 按 `key` 判重，不比较自然语言文本。`key` 使用封闭前缀集合：

```text
resources.*       目标地址、仓库、文件、设备等资源位置
success.*         成功标准、交付物要求
constraints.*     时间限制、范围限制、禁止事项
authorization.*   授权范围、账户边界
access.*          凭证、接入方式
```

Server 必须校验：

1. `questions` 长度为 1～3。
2. `key`、`question` 和 `why_blocking` 非空。
3. `key` 必须匹配封闭前缀集合（形如 `^(resources|success|constraints|authorization|access)\.[a-z0-9_]+$`）；非法前缀视为非法输出。
4. 同一次 decision 内 `key` 不得重复；不得与历史已提问的 `key` 重复。
5. 已回答过的 `key`（包括用户选择 unknown 的）不得再次提问。
6. Server 为问题分配稳定 id，模型不得决定数据库 id。

违反第 3～5 条的 decision 视为一次技术失败：释放 lease、保持 `preparing`、`attempt_count + 1`。模型可能为同一主题发明不同 `key`（alias），判重可能漏网；此时由 `max_intake_clarification_rounds` 兜底，不得引入文本相似度或向量检索来判断问题相似。

用户回答可以是文本，也可以显式选择“不知道，交给 AI 调查”：

```json
{
  "revision": 2,
  "answers": [
    {
      "question_id": "q002_01",
      "answer": "",
      "unknown": true
    }
  ]
}
```

若用户选择 unknown，下一轮 Goal Gate 应优先将其转入 `spec.unknowns`。只有缺少该信息会使任何执行都无意义时，才允许继续追问；由于已回答的 `key` 不得再次提问，追问必须使用语义不同的新 `key`，且受问答轮次上限约束。

### 6.5 Intake 数据表

新增表建议如下：

```sql
CREATE TABLE IF NOT EXISTS project_intakes (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    raw_origin TEXT NOT NULL,
    raw_goal TEXT NOT NULL,
    raw_hints_json TEXT NOT NULL DEFAULT '[]',
    transcript_json TEXT NOT NULL DEFAULT '[]',
    pending_questions_json TEXT NOT NULL DEFAULT '[]',
    goal_spec_json TEXT,
    normalized_origin TEXT,
    normalized_goal TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    clarification_rounds INTEGER NOT NULL DEFAULT 0,
    waiting_since TEXT,
    activation_mode TEXT,
    pending_proposal_json TEXT,
    auto_activate INTEGER,
    review_worker TEXT,
    review_claim_id TEXT,
    review_started_at TEXT,
    review_last_heartbeat_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

约束：

1. JSON 写入前必须经过 Pydantic 模型校验，读取后也必须验证；不得在各调用点直接操作任意 dict。
2. `revision` 每次用户提交答案或 Additional context 后递增。
3. Goal Gate 结果必须携带其读取到的 revision；Server 用 compare-and-set 拒绝陈旧结果。
4. `attempt_count` 是当前 revision 内的技术失败次数（timeout、非法输出、非法 questions decision）；用户提交答案或 Additional context 后清零；达到 Server 设置 `max_intake_attempts`（默认 3）时项目进入 `intake_failed`。
5. `clarification_rounds` 只在成功进入 `waiting_input` 时递增，不因用户回答而清零。已达 Server 设置 `max_intake_clarification_rounds`（默认 3）后再收到 questions decision，Server 不保存这一轮问题，清空 lease 和 `pending_questions_json`，直接进入 `intake_failed`。因此 `intake_failed` 永远没有 pending questions。
6. `waiting_since` 在进入 `waiting_input` 时写入，离开该状态时清空；第一版只记录，不做自动过期。
7. `activation_mode` 为 `gate` 或 `forced`，只在激活时写入；它是审计字段，不得写成 Fact 或 Hint。
8. `review_claim_id` 是一次 Goal Gate 执行的唯一标识；Worker 名称只用于显示和调度，不能单独证明是同一次执行。
9. `pending_proposal_json` 保存 ready decision 的 origin/goal/spec/notices，只在 `ready_review` 有效；confirm、force、编辑重审等任何离开 `ready_review` 的转换都必须清空它。
10. `auto_activate` 可空；NULL 表示跟随 Server 设置 `intake_auto_activate`。

### 6.6 Intake HTTP Interface

#### 获取项目

`GET /projects` 的 ProjectSummary 新增：

```json
{
  "status": "waiting_input",
  "pending_question_count": 2,
  "waiting_since": "...",
  "intake_last_error": null
}
```

`GET /projects/{project_id}` 的 ProjectDetail 新增可空字段：

```json
{
  "intake": {
    "raw_origin": "目标是公司提供的测试环境",
    "raw_goal": "完成安全能力测评",
    "revision": 2,
    "pending_questions": [
      {
        "id": "q002_01",
        "key": "resources.target_url",
        "question": "目标地址是什么？",
        "why_blocking": "没有入口无法开始"
      }
    ],
    "spec": null,
    "pending_proposal": null,
    "auto_activate": null,
    "last_error": null,
    "clarification_rounds": 1,
    "waiting_since": "...",
    "activation_mode": null,
    "updated_at": "..."
  }
}
```

旧项目没有 intake 记录时返回 `intake: null`，不得在读取时补造记录。

`ProjectDetail` 不返回 `transcript`，前端只拿到当前待回答问题和待确认 proposal。Dispatcher 在 intake claim 成功后可通过该 HTTP 响应读取完整 transcript；不得依赖共享内存或直接读数据库。

#### 用户提交回答

```text
POST /projects/{project_id}/intake/answers
```

请求：

```json
{
  "revision": 2,
  "answers": [
    {
      "question_id": "q002_01",
      "answer": "http://127.0.0.1:9000",
      "unknown": false
    }
  ],
  "additional_context": "可选：补充说明，例如临时接入方式",
  "edited_origin": "可选：仅在 ready_review 使用，覆盖后的完整 origin",
  "edited_goal": "可选：仅在 ready_review 使用，覆盖后的完整 goal"
}
```

行为：

1. `waiting_input` 必须完整回答当前全部 pending questions，`additional_context` 可空。`intake_failed` 只允许 `answers=[]` 且 `additional_context` 非空；该状态不存在 pending questions。`ready_review` 只允许 `answers=[]` 且必须携带非空的 `edited_origin` 和 `edited_goal`。
2. revision 必须与当前 intake 一致，否则返回 `409`。
3. `waiting_input` 中必须完整回答当前所有 pending questions。
4. `unknown=false` 时 answer 必须非空；`unknown=true` 时忽略空白 answer。
5. `additional_context` 可空；非空时作为独立条目追加到 transcript。这是 intake 阶段唯一的自由文本入口：不得为此开放 Hint 写接口，避免 Hint、问题答案、原始输入三条输入路径并存。
6. `ready_review` 的编辑提交：用编辑文本覆盖 `raw_origin/raw_goal`，把编辑事件（不含旧文本全文，只记长度与预览）追加到 transcript，清空 `pending_proposal_json`。origin/goal 任一为空返回 `422`。
7. 把问答追加到 transcript，清空 pending questions。
8. revision 加一，`attempt_count` 清零（`clarification_rounds` 不清零），项目状态改回 `preparing`。
9. 返回更新后的 ProjectDetail。

#### Goal Gate claim

Dispatcher 使用以下 Interface：

```text
POST /projects/{project_id}/intake/claim
POST /projects/{project_id}/intake/heartbeat
POST /projects/{project_id}/intake/decision
POST /projects/{project_id}/intake/release
```

Dispatcher 在每次新派发前生成从未复用的 UUID `claim_id`；Server 只比较当前 lease，不维护历史 claim id 集合。`claim` 请求携带 `worker + claim_id + revision`；成功响应返回 `raw_origin`、`raw_goal`、`raw_hints`、完整 `transcript` 和当前 `revision`。`heartbeat`、`decision`、`release` 同样必须携带 `worker + claim_id + revision`。

`release` 请求还必须携带非空 `error`；它不是中性解锁接口，而是 timeout、进程失败、JSON 解析失败和 Dispatcher 本地契约校验失败的唯一技术失败出口。它在一个事务中累加 `attempt_count`、写入清理后的 `last_error`、清除 lease，并在达到上限时转入 `intake_failed`。

claim/heartbeat/release 语义与现有 reason lease 类似，但只允许 `preparing`：

1. 相同 `claim_id` 的 claim 重试幂等返回同一份 claim 响应。
2. 相同 Worker 名、不同 `claim_id` 不属于同一次执行；未过期 lease 下返回 `409`。
3. Server 设置 `intake_lease_timeout`（默认 15 秒）是 intake lease 的唯一过期阈值。claim 和项目读取路径在事务内回收过期 lease 后才能重新认领；回收也计一次技术失败，避免 Dispatcher 持续崩溃绕过上限。旧 `claim_id` 的迟到心跳和结果均返 `409`。

`decision` 必须在同一个事务内验证：

1. 项目仍是 `preparing`。
2. `worker` 和 `claim_id` 仍持有 lease。
3. revision 与 claim 时一致。

`questions` decision：若当前问答轮次尚未达上限，写入问题、递增 `clarification_rounds`、设置 `waiting_since`、清空 lease 并切换 `waiting_input`。若已达上限，不保存新问题，清空 pending questions、`waiting_since` 和 lease，直接切换 `intake_failed`。

Dispatcher 必须先对模型原始输出做严格契约校验；这类本地失败调用 `release(error)`。Server 仍重新校验 decision 中的历史 key、revision 和轮次等语义条件；语义失败必须持久化为同一次技术失败后再返回结果，不得因 Router 抛出异常导致整个计数事务回滚。HTTP/Pydantic 层的恶意结构请求只返回 `422`，不计为模型尝试。

`ready` decision：项目开启 `auto_activate`（项目字段优先，否则跟随 Server 设置 `intake_auto_activate`）时，写入 normalized origin/goal/spec、`activation_mode='gate'`，插入 Origin/Goal 和创建时暂存的 hints，清空 lease，切换 `active`。未开启时，把 origin/goal/spec/notices 写入 `pending_proposal_json`，清空 lease，切换 `ready_review`；不写知识图。

#### 用户重试

```text
POST /projects/{project_id}/intake/retry
```

1. 只允许 `intake_failed`。
2. `revision + 1`、`attempt_count` 清零，`clarification_rounds` 保持不变，清空 `last_error`，状态改回 `preparing`。递增 revision 用于阻断失败前旧 `claim_id` 在 Retry 后迟到重试。
3. 该转换由数据库事务保证一次性。第一次成功后项目已是 `preparing`，不再符合前置状态；重复调用返回 `409`，不声称该接口幂等。

#### 用户确认启动

```text
POST /projects/{project_id}/intake/confirm
```

请求：

```json
{ "revision": 3 }
```

1. 只允许 `ready_review`；revision 必须与当前 intake 一致，否则返回 `409`。
2. 不调用任何模型。使用 `pending_proposal_json` 中的 origin/goal/spec，在与 ready decision 相同的原子事务中插入 Origin/Goal 和创建时暂存的 hints，写入 `activation_mode='gate'`，清空 `pending_proposal_json`，切换 `active`。
3. 幂等：重复 confirm 返回当前 ProjectDetail，不重复插入。
4. confirm 与编辑提交、force 的竞态都由 revision 和状态校验串行化，先到者生效，后到者 `409`。

#### 用户强制激活

```text
POST /projects/{project_id}/intake/force
```

请求：

```json
{ "confirm": true }
```

1. 只允许 `waiting_input`、`ready_review` 或 `intake_failed`；`confirm` 缺失或非 true 返回 `422`。
2. 不调用任何模型。normalized origin 由 raw origin 与 transcript 中的问答按序拼接生成，normalized goal 使用 raw goal，创建时暂存的 hints 原样写入。注意与 confirm 的区别：confirm 使用 Gate 审查过的 proposal，force 放弃 proposal、按原始输入直接激活，`ready_review` 下 force 同时清空 `pending_proposal_json`。
3. 在与 ready decision 相同的原子事务中插入 Origin/Goal 和 hints，写入 `activation_mode='forced'`，清空 lease，切换 `active`。
4. 幂等边界：项目已是 `active` 且 `activation_mode='forced'` 时返回当前 ProjectDetail，不重复插入；已是其他激活模式或其他状态时返回 `409`。
5. 前端必须二次确认“将绕过 AI 输入审查”；固定测评输入默认仍先走 Goal Gate，强制激活只是人工逃生门。

#### Export 限制

`GET /projects/{project_id}/export` 对 `preparing/waiting_input/ready_review/intake_failed` 返回 `409 Project graph is not ready`，不得导出 Origin/Goal 为空的图。

### 6.7 Goal Gate Dispatcher 任务

配置修改：

```python
TaskType = Literal["intake", "reason", "explore", "bootstrap"]
```

新增配置：

```yaml
tasks:
  intake:
    timeout: 120
  bootstrap:
    timeout: 300
    conclude_timeout: 90
  reason:
    timeout: 300
  explore:
    timeout: 300
    checkpoint_timeout: 90
    max_session_attempts: 3
    retry_delay: 15
    max_no_progress_slices: 2

runtime:
  max_running_intakes: 2
```

`tasks.intake.timeout` 只限制一次模型调用。由 Server 强制的协议上限使用 Server settings 作为唯一真相源：

```text
intake_lease_timeout = 15
max_intake_attempts = 3
max_intake_clarification_rounds = 3
intake_auto_activate = false
```

`intake_auto_activate` 决定 ready decision 的默认去向（直接激活还是进入 `ready_review`）；创建项目时的 `auto_activate` 字段优先于该设置。Dispatcher 不能用本地配置替 Server 判定失败耗尽或问答轮次耗尽。

所有默认 Worker 配置应包含 `intake`，除非用户明确配置独立 Worker。这里不是模型分层，只是任务能力声明。

Scheduler 行为（Dispatcher 以 Future 异步执行任务，调度 tick 不等待运行中的任务结束）：

1. 在每轮列表中识别 `preparing` 项目。
2. 对其选择支持 `intake` 的 Worker。
3. claim 成功后执行 `run_intake_task`。
4. Intake 不创建项目 workspace 或项目运行标识，只复用 startup runtime 执行 Worker；不计入 `max_running_projects`，但计入全局 `max_workers` 和 Worker 的 `max_running`。
5. 每个调度 tick 最多新派发一个 intake；同时运行的 intake 不超过 `runtime.max_running_intakes`（默认 2）。
6. 为 active 项目保留至少一个全局 Worker 槽位：仅剩最后一个空位且存在可调度的 active 任务时，跳过本轮 intake 派发。
7. `waiting_input`、`ready_review` 和 `intake_failed` 不进入任何调度队列。
8. Dispatcher 读到 `preparing` 项目但没有任何配置为支持 `intake` 的 Worker 时，记录稳定 warning，不由 Server 启动检查 Worker 配置。Server 不知道 Dispatcher Worker 列表。

Goal Gate 复用现有 Worker Adapter 的 execute/session 能力，不增加安全专用 Driver、`supports_intake()` 或强制 no-tool 通道。`intake.md` 仍必须把“只审查输入、不执行项目任务”写成行为契约；在本文的单用户可信容器前提下，该契约不被视为额外安全边界。

新增 Prompt：

```text
cairn/src/cairn/dispatcher/prompts/default/intake.md
cairn/src/cairn/dispatcher/prompts/mock/intake.md
```

Prompt 输入：raw origin、raw goal、raw hints、完整问答 transcript、当前 revision。

Prompt 必须明确：

1. 这是启动审查，不得开始执行项目任务。
2. 能由 Agent 后续探索的信息不得向用户提问。
3. 问题最多三个。
4. 只能返回 `ready` 或 `questions`。
5. 不得把技术实施方案当作用户必须提供的参数。
6. 输出（origin、goal、spec、questions）必须使用与用户输入相同的语言。
7. `ready.origin` 必须近乎原文保留全部操作细节（地址、凭证、接口路径、字段名、数值限制、规则、时限），只允许整理格式，不得压缩为摘要；`ready.goal` 可以简述，但不得丢失成功标准、硬约束和交付要求。

## 7. 前端详细改动

前端仍保持单文件 Alpine.js，不在本次引入 React/Vue 或构建工具。

### 7.1 项目列表

增加状态颜色和文案：

| 状态 | Badge | 卡片辅助信息 | 操作 |
| --- | --- | --- | --- |
| `preparing` | 蓝色 `PREPARING` | `AI is checking the goal` | View / Delete |
| `waiting_input` | 紫色 `WAITING INPUT` | `Needs N answers` + 已等待时长（`waiting_since`） | Answer / Delete |
| `ready_review` | 青色 `READY REVIEW` | `Confirm to start` | Confirm / Delete |
| `intake_failed` | 红色 `INTAKE FAILED` | 清理后的 `last_error` 摘要 | Retry / Start Anyway / Delete |
| `active` | 现有绿色 | 现有统计 | Stop / View |
| `stopped` | 现有黄色 | 现有统计 | Resume / View |
| `completed` | 现有灰色 | 现有统计 | Reopen / View |

顶部统计增加 Waiting Input 数量；`Stop Active` 只处理 `active`，不得触碰 intake 项目。

点击等待输入、待确认或失败卡片后进入该项目详情，不直接打开空图。

### 7.2 项目详情页

详情页按状态分成五个互斥视图：

1. `preparing`：显示标题、状态、原始 Origin/Goal、加载状态和最近技术错误；不初始化 Cytoscape。
2. `waiting_input`：显示问题卡片和回答表单；不初始化 Cytoscape。
3. `ready_review`：显示启动确认卡（最终 origin/goal 可编辑、Gate 修改说明、确认启动与重新审查按钮）；不初始化 Cytoscape。
4. `intake_failed`：显示清理后的错误摘要、Additional context 输入框、Retry 按钮和 Start Anyway 按钮；失败视图不渲染问题回答表单；Start Anyway 必须二次确认“将绕过 AI 输入审查”；不初始化 Cytoscape。
5. `active/stopped/completed`：显示现有图界面。

`openProject` 和轮询逻辑必须根据状态决定是否调用 `initGraph/updateGraph`。不得用空 facts 初始化图后等待激活。

当轮询检测到 `preparing -> waiting_input`：

1. 自动切换到问答面板。
2. 保留用户当前所在项目。
3. 不使用 toast 代替问题内容。

当检测到 `preparing -> ready_review`：自动切换到确认卡并初始化 proposal 草稿。

当检测到 `preparing -> intake_failed`：自动切换到失败视图并显示错误摘要，不用 toast 代替视图内容。

当检测到 `preparing/waiting_input/ready_review/intake_failed -> active`：

1. 移除 intake 面板。
2. 在 `$nextTick` 中初始化 Cytoscape。
3. 加载新生成的 Origin/Goal。
4. 显示一次 `Goal confirmed, project started`（强制激活时显示 `Project started without AI goal review`）。

### 7.3 等待输入表单

整体形式是详情页内的**单卡片**，不弹任何对话框；每轮审查只有一张卡。布局与尺寸规则：

1. 页面：详情区为纵向滚动容器，卡片居中、最大宽度约 768px（`max-w-3xl`）、内边距 24px；任何内容不得造成页面横向滚动。
2. 卡片顶部用有序列表（1. 2. 3.）列出“还缺哪些信息”，编号与下方每个问题区块一一对应，用户一眼看到缺口全貌。
3. 原始 Origin/Goal 面板：两列（窄屏退化为单列），面板内容限高约 10 行、超出内部滚动，配“展开全部/收起”切换；不得让长原文把回答表单推出首屏。
4. 每个问题一个区块：编号徽章（与顶部有序列表同号）+ 问题正文 + `why_blocking` + 多行输入框 + “不知道，交给 AI 调查”复选框。
5. 输入框：默认 3 行，允许纵向拉伸但限最大高度约 14 行，超出内部滚动；禁止横向拉伸。
6. 所有模型和用户文本必须经 `x-text` 渲染并配 `whitespace-pre-wrap break-words`；超长 URL 或无空格文本不得撑破布局；禁止使用 `x-html`。
7. 按钮行 `flex-wrap`：窄屏自动换行，主按钮始终排最后。
8. 表单底部另有一个可选的 Additional context 多行输入框，随答案一起提交，用于补充模型没问到的信息。
9. `intake_failed` 视图复用 Additional context 提交方法，但固定发送 `answers=[]`，并在文本非空时才允许提交。

提交按钮约束：

1. 每个问题必须有非空答案或勾选 unknown。
2. 提交中禁用整个表单，避免重复请求。
3. 请求携带当前 revision。
4. `409` 时重新加载项目并提示“问题已更新，请重新确认答案”。
5. 成功后立即显示 `preparing`，清空本地答案，不等待下一次轮询。

建议新增 Alpine 状态：

```javascript
intakeAnswers: {},
intakeAdditionalContext: "",
isSubmittingIntakeAnswers: false,
isConfirmingForceActivate: false,
lastProjectStatus: null,
```

建议新增方法：

```javascript
projectIsInIntake()
projectNeedsInput()
projectIntakeFailed()
initializeIntakeAnswers()
intakeAnswerIsValid(questionId)
submitIntakeAnswers()
retryIntake()
forceActivateIntake()
handleProjectStatusTransition(previous, current)
```

### 7.4 启动确认卡片

`ready_review` 的卡片同样遵守 7.3 的尺寸与溢出规则，内容如下：

1. 顶部说明“AI 已完成输入审查，确认后开始执行”；下方为 Gate 的 notices（它改了什么、仍有疑点什么），纯展示、限高内部滚动。
2. Origin/Goal 为可编辑多行文本框（Origin 默认 8 行、Goal 默认 3 行，限高约 18 行内部滚动），初始值为 proposal 内容；用户不满意可直接修改。
3. 主按钮“确认启动”：仅当 origin/goal 均非空可用；调用 `POST /intake/confirm`（携带 revision），成功后按 `-> active` 流程初始化图。
4. 次按钮“保存并重新审查”：仅当文本相对 proposal 有改动且非空可用；调用 answers 接口（`answers=[]` + `edited_origin/edited_goal`），回到 `preparing`。
5. 角落次按钮 Start Anyway（force，二次确认）：放弃 proposal，按原始输入直接激活。
6. 卡片必须说明：激活后 origin/goal 即写入知识图，不可再修改。

建议新增 Alpine 状态：

```javascript
proposalOrigin: "",
proposalGoal: "",
proposalNotices: [],
isConfirmingProposal: false,
```

建议新增方法：

```javascript
projectReadyForReview()
proposalIsDirty()
proposalIsValid()
initializeProposalDraft()
confirmProposal()
resubmitProposalEdits()
```

### 7.5 New Project 表单

保留 Title、Origin、Goal、Bootstrap 和 Hints 字段，但修改提交后的文案：

1. 按钮从 `Create` 改为 `Review & Create`。
2. 成功 toast 从 `Project created` 改为 `Project submitted for goal review`。
3. 创建后进入 `preparing` 详情页，而不是假设知识图已经生成。
4. Origin/Goal 仍要求非空；Goal Gate 负责检查内容是否足够，不负责猜测完全空白的任务。

### 7.6 前端不可显示的字段

不得把以下内容返回或渲染到前端：

1. Worker 环境变量或模型密钥。
2. 内部 session id。
3. Goal Gate 的完整原始模型输出。
4. Dispatcher 内部错误堆栈。
5. intake transcript（历史问答原文，可能包含凭证）。

前端只显示经过 Server 清理的 `last_error` 摘要和当前待回答问题。

## 8. IntentRun 详细设计

### 8.1 IntentRun 状态机

```text
不存在
  -> running       首次 claim

running
  -> yielded       continue / 时间片结束且有检查点
  -> completed     fact 成功写回
  -> failed        rejected 或超过无进展限制
  -> yielded       Worker 技术失败但已有有效检查点

yielded
  -> running       再次 claim 并恢复

failed
  -> running       `next_retry_at` 非空且已到期，创建新 session
```

IntentRun 的 `completed/failed` 是执行审计状态，不是新的 Fact。`failed` 且 `next_retry_at IS NULL` 表示新 session 重试已耗尽，是 terminal failed；Scheduler 不再派发。

### 8.2 IntentRun 数据表

```sql
CREATE TABLE IF NOT EXISTS intent_runs (
    project_id TEXT NOT NULL,
    intent_id TEXT NOT NULL,
    status TEXT NOT NULL,
    worker_name TEXT,
    worker_type TEXT NOT NULL,
    claim_id TEXT,
    session_id TEXT,
    checkpoint_json TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    no_progress_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    last_error TEXT,
    started_at TEXT NOT NULL,
    last_started_at TEXT NOT NULL,
    last_yielded_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, intent_id),
    FOREIGN KEY (intent_id, project_id)
        REFERENCES intents(id, project_id) ON DELETE CASCADE
);
```

约束：

1. `session_id`、checkpoint 和完整 evidence 只在 run/claim 成功后返回，ProjectDetail 不返回。
2. checkpoint 大小设置上限，建议 8 KiB；大内容必须写入项目 workspace 并通过 artifact ref 引用。
3. artifact ref 必须是项目 workspace 下规范化后的相对路径，禁止绝对路径和 `..`；yield 时文件必须真实存在，否则视为校验失败，而不是“无进展”。
4. `evidence_json` 保存按 8.8 合并后的累计证据，不是单个时间片的快照。artifact 元数据为 `{path, sha256, size}`，由可信 Dispatcher 根据实际 workspace 文件在 Server 写事务外计算。
5. `claim_id` 唯一标识当前执行时间片；Worker 名称相同不代表是同一次 claim。yield、fail、过期、Stop 和 Complete 清空它。
6. `attempt_count` 只在开始新 session 时递增；恢复同一 session 的下一时间片不递增。
7. 一个 Intent 只能有一条 IntentRun 记录。
8. `evidence_json` 的 artifact 保留所有已见 `{path, sha256, size}` 版本，不只保留每个 path 的最新值；这是防止 `A -> B -> A` 震荡被重复计为进展的必要条件。
9. `ProjectDetail.intent_runs` 只返回 Scheduler 需要的摘要：`intent_id/status/attempt_count/no_progress_count/worker_type/worker_name/has_session/next_retry_at/updated_at`。`has_session` 只表示是否存在可恢复 session，不能返回 `session_id`；同样不暴露 checkpoint、evidence 或 claim id。`worker_name` 仅用于判断 Pi 的 session 是否属于同一 Worker 实例。

### 8.3 Explore 输出契约

新格式：

```json
{
  "accepted": true,
  "data": {
    "fact": {
      "description": "已确认……"
    }
  }
}
```

```json
{
  "accepted": true,
  "data": {
    "continue": {
      "progress": "本时间片已确认……（仅用于恢复上下文，不参与进展判定）",
      "next": "下一时间片继续……",
      "artifacts": ["artifacts/i123/progress.json"],
      "cursors": {"tested": 60000, "total": 897000},
      "milestones": [
        {"id": "port-scan-done", "artifact": "artifacts/i123/ports.json"}
      ]
    }
  }
}
```

```json
{
  "accepted": false,
  "reason": "policy_refusal"
}
```

`continue` 的证据字段：

1. `artifacts`：本时间片产出的可恢复文件，相对路径。
2. `cursors`：分段任务的单调游标，同一 key 只有数值严格增长才是新进展。
3. `milestones`：阶段性成果，必须带稳定 `id` 和对应 artifact 引用。
4. 三类证据都可以为空，但只有向累计证据中增加或更新可验证内容才算进展（见 8.8）。

兼容期内接受旧格式：

```json
{"accepted": true, "data": {"description": "..."}}
```

旧格式只映射为 `fact`，不得根据文本猜测它其实是 continue。兼容期结束时间暂不在本方案中规定。

### 8.4 时间片结束流程

现有 `explore.timeout=300` 保留，但含义改为 execution slice timeout。

Worker 进程结束后的统一处理矩阵：

| 结束原因 | session 可恢复 | 进入 checkpoint |
| --- | --- | --- |
| 时间片内返回合法 `fact` | 无关 | 否，直接 conclude |
| 时间片内返回合法 `continue` | 无关 | 否，直接 yield |
| timeout | 是 | 是 |
| 正常退出但 JSON 解析失败 | 是 | 是 |
| 正常退出但 payload 校验失败 | 是 | 是 |
| 非零退出 | 是 | 是 |
| session 不存在 | 否 | 无法 checkpoint，按失败处理 |
| 用户 Stop/Delete | 无关 | 否 |
| lease 丢失 | 无关 | 否 |
| 明确 rejected | 无关 | 否，标记 failed |

原则：只要项目仍 active、租约仍有效且 session 可恢复，所有异常结束都先尝试 checkpoint，再按 checkpoint 结果决定去向；不得因为结束原因不是 timeout 就丢弃可恢复的工作。lease 丢失时不再尝试任何写入，因为 yield/conclude 必然被 Server 拒绝。

checkpoint 阶段使用 `build_resume` + `explore_checkpoint.md`，只能返回 `fact`、`continue` 或 rejected：

1. 返回 `fact`：调用 Runtime conclude，原子化创建 Fact、结束 Intent 和 IntentRun。
2. 返回 `continue`：调用 Runtime yield，持久化 session/checkpoint，释放 Intent claim。
3. 失败但已有上一次有效 checkpoint：保留旧 checkpoint，增加 no-progress 计数并 yield。
4. 失败且从未产生有效 checkpoint：释放 Intent claim，IntentRun 标记 failed；不创建 Fact。

不得再使用“timeout 后强制生成 Fact”的 conclude 语义；checkpoint 的目的是抢救可恢复状态，不是逼模型立刻下结论。

### 8.5 恢复流程

租约过期由 Server 在读路径统一处理（见 8.7）：`expire_workers` 在清空 `intents.worker` 的同一事务中把对应 running Run 翻转为 yielded。Scheduler 不自行纠正 running Run，只按 yielded 处理。

被翻转的首个时间片可能还没有任何 checkpoint 和持久化 session；此时按“新执行”处理：同一 Intent、同一 Run 记录，创建新 session 使用 `explore.md`。

旧 Worker 进程在租约过期后可能仍在运行，直到下一次心跳失败才自行中止（僵尸窗口）；其后续写入会因 lease 校验失败被 Server 拒绝。新 Worker 恢复 session 时可能遇到被中断的 tool call，Adapter 必须容忍并从最后一个完整消息继续。

Scheduler 选择开放 Intent 时：

1. 读取其 IntentRun。
2. 没有 Run：创建新 session，使用 `explore.md`。
3. Run 为 yielded 且 session 可恢复：使用 `explore_resume.md`，通过 Worker Adapter 恢复 session。
4. Run 为 yielded 但原 Adapter 不可用：使用新 session + checkpoint 文本恢复；日志记录 `checkpoint_only_resume`。
5. Run 为 yielded 但 session 与 checkpoint 均为空（过期翻转的首个时间片）：按第 2 条新执行。

恢复 Prompt 至少包含：

1. 当前 Intent id 和描述。
2. 最新 checkpoint。
3. artifact refs。
4. 最新图快照引用。
5. 明确要求继续同一工作，不要重新做已经确认的步骤。

### 8.6 Worker Driver Interface

当前 `build_conclude` 只表达收尾用途，改为更通用的 resume Interface：

```python
class WorkerDriver:
    def prepare_session(self) -> str | None: ...
    def build_execute(self, worker, prompt, session) -> DriverResult: ...
    def build_resume(self, worker, prompt, session) -> list[str]: ...
    def extract_session(self, session, stdout, stderr) -> str | None: ...
```

`build_conclude` 在迁移期可以保留为调用 `build_resume` 的兼容方法，随后由 checkpoint 和 resume 两种 Prompt 共同复用 `build_resume`。

Adapter 约束：

1. Claude Code、Codex、Pi 分别实现真实 session 恢复。
2. Mock Adapter 必须能够模拟 fact、continue、timeout、checkpoint 和 resume。
3. Adapter 不负责决定 Fact/continue；它只负责执行和恢复 Worker。

### 8.7 Intent Runtime HTTP Interface

Dispatcher 使用：

```text
POST /projects/{project_id}/intents/{intent_id}/run/claim
POST /projects/{project_id}/intents/{intent_id}/run/heartbeat
POST /projects/{project_id}/intents/{intent_id}/run/yield
POST /projects/{project_id}/intents/{intent_id}/run/conclude
POST /projects/{project_id}/intents/{intent_id}/run/fail
```

租约只有一个真相源：`intents.worker` + `intents.last_heartbeat_at`。不得为 IntentRun 引入第二套超时时钟。

`run/claim` 必须在一个事务中同时：

1. 验证项目 active、Intent open。
2. 检查或接管过期 claim。
3. 设置 `intents.worker` 和 `intents.last_heartbeat_at`。
4. 创建或更新 IntentRun 为 running。
5. 返回恢复所需的 session/checkpoint/evidence。

claim 请求携带 `worker`、`worker_type`、Dispatcher 每次派发新生成的 UUID `claim_id`。Server 响应必须明确 `execution_mode`：

1. `new`：首次执行，创建新 session，`attempt_count + 1`。
2. `session`：Run 为 yielded，原 `worker_type` 可用且 session 可恢复；恢复原 session，不增加 attempt。
3. `checkpoint_only`：原 Adapter 不可用、session 不可用，或从 failed 重试；清除旧 session id、保留最后有效 checkpoint 和累计 evidence、重置 `no_progress_count`，创建新 session 并 `attempt_count + 1`。claim 响应必须返回旧 checkpoint，且在新时间片成功 yield/conclude 前不得破坏这份唯一可恢复状态。

相同 `claim_id` 重试幂等返回已有响应，不再增加 attempt；即使 Worker 名相同，不同 `claim_id` 在未过期 claim 下也必须返回 `409`。首次时间片被过期翻转且 session/checkpoint 均为空时，下次 claim 返回 `new`。Dispatcher 必须为每次新派发生成从未复用的 UUID；Server 只比较当前 lease 的 `claim_id`，第一版不另建历史 claim 集合。

`run/heartbeat` 必须在同一事务中刷新 `intents.last_heartbeat_at`（可同时更新 IntentRun 的 `updated_at`）。这样 `GET /projects`、`GET /projects/{id}`、export 等读路径上的 `expire_workers` 不会把仍在运行的 Run 误判为过期。

`run/yield` 必须在一个事务中：

1. 同时验证 project、Intent、Worker、`claim_id` 和 running 状态。
2. 验证 checkpoint 和 Dispatcher 已校验的证据结构（见 8.8）。
3. 更新 IntentRun 为 yielded。
4. 合并累计证据并更新无进展计数。
5. 保存本次 session id，清空 `intents.worker/last_heartbeat_at` 和 Run claim，使下一时间片可重新认领。

yield 请求携带 Dispatcher 配置的 `max_no_progress_slices` 和 `retry_after_seconds: int | null`。Server 只在本次 merge 使 no-progress 达到阈值时使用后者：这时不保持 yielded，而是原子转为 failed 并持久化退避；普通 yield 忽略该字段。

`run/conclude` 携带最终 session id，必须先完成上述五重校验，再复用现有 conclude 的 Fact id 生成和写图规则，并在同一事务中创建一个 Fact、关闭 Intent、把 Run 标记 completed。迟到或重复 conclude 可返回既有结果或 `409`，但绝不能再生成 Fact id。

`run/fail` 同样携带最终 session id、清理后的 error 和 `retry_after_seconds: int | null`。Dispatcher 按 `max_session_attempts`（默认 3）决定是否还可重试：可重试时传 `retry_delay`（默认 15 秒），Server 使用自己的 UTC 时钟写入 `next_retry_at`；耗尽时传 `null`，得到 terminal failed。退避必须持久化，不得依赖 Dispatcher 内存计时。

过期处理只有一处：`expire_workers` 在清空过期 `intents.worker` 的同一事务中，把对应 status 为 running 的 IntentRun 翻转为 yielded（保留其 session/checkpoint）。任何时刻都不得存在 `intents.worker IS NULL` 而 IntentRun 仍为 running 的记录。run/heartbeat、旧 Intent heartbeat、列表轮询和 export 共用这一条过期规则。

现有 Intent claim/heartbeat/release/conclude Interface 保留给人工消费者、Bootstrap 和兼容调用者。两套流程以 `intents.worker` 为唯一互斥点：

1. 不同 Worker 竞争同一 Intent，无论新旧接口，一律后到的 `409`。
2. 只有相同 `claim_id` 的 `run/claim` 重试才幂等；同一 Worker 的不同 `claim_id` 仍返回 `409`。
3. 旧式 claim 已存在时，`run/claim` 不得创建 IntentRun，返回 `409`。
4. Intent 只要存在任意状态的 IntentRun，旧式 claim/heartbeat/release/conclude 都返回 `409`；不只阻止 running Run。没有 Run 的 Bootstrap/人工 Intent 仍走旧接口。
5. Dispatcher 的 Explore 只使用 run Interface，同一任务内不得混用两套 claim。

Stop、Complete 和过期回收不得在 Router 中直接清字段：必须调用 Runtime Module，在一个事务中把匹配 running Run 翻转 yielded，清除 `intents.worker/last_heartbeat_at` 和 `claim_id`。Delete 依赖外键级联。所有 Intent 租约写入点必须集中到 Runtime Module 或其 guard，不能继续散落在 projects/intents/services Router 中。

### 8.8 无进展限制

新增配置：

```yaml
tasks:
  explore:
    timeout: 300
    checkpoint_timeout: 90
    max_no_progress_slices: 2
```

不计算模型文本指纹；Server 把本次证据合并到历史累计态，以“累计态是否变大”判定进展。`progress`/`next` 只用于恢复上下文：

1. artifact：Dispatcher 规范化项目 workspace 下的相对路径，验证 `is_file`，在 DB 事务外流式计算 SHA-256 和大小；Server 校验结构与数量上限后合并。从未见过的 `{path, sha256, size}` 版本才是新进展。
2. cursor：Server 每个 key 只保留历史最大数值；只有严格增长才是新进展，相等或下降不改变累计态。
3. milestone：必须带稳定 id 和已验证 artifact 引用；Server 永久按 id 去重，只有新 id 是进展。
4. 省略历史字段、调整数组顺序、删除证据、重复提交旧 artifact 版本或出现 `A -> B -> A` 都不会让累计态变大。
5. 累计态变大时 `no_progress_count=0`；未变大时 `no_progress_count + 1`。文件 mtime、日志时间戳和模型自报进展均不参与。
6. 达到 `max_no_progress_slices` 后标记 failed，释放 Intent，不创建 Fact；下次只能通过有界退避创建新 session。
7. 不限制有真实进展的总时间片数量。

这可以防止“换个措辞就算进展”绕过检测，但不能声称能够对抗模型故意伪造证据（例如自增 cursor、写无关文件）。

第一版不让 Reason 因 failed 自动创建替代 Intent。`max_no_progress_slices` 限制同一 session 的连续无进展时间片，`max_session_attempts` 另外限制该 IntentRun 可创建的 session 总数；两者都耗尽后 Scheduler 必须停止自动执行。

### 8.9 工作目录和后台进程

第一版采用以下明确约束：

1. 项目 workspace 继续持久化，checkpoint 必须引用其中的可恢复文件。
2. Explore Prompt 要求长任务分段执行，并定期把进度写入文件。
3. 当前 Worker 进程组在时间片结束时继续被清理。
4. 禁止使用未受管理的 `setsid/nohup` 让进程逃逸时间片。
5. 项目 stopped/completed/deleted 时继续使用现有 cancellation、process group 和 Docker ledger 清理。

这只能减少而不能确定性杜绝恶意或错误的脱离进程。若后续仍有大量任务必须跨时间片保留系统进程，应另行设计 task-owned job supervisor；不要在本分支同时引入 Unix socket、守护进程和跨平台 PID 恢复。

### 8.10 Bootstrap 本轮不接入 IntentRun

本轮固定测评已关闭 Bootstrap，因此明确不修改 Bootstrap 契约、Prompt、claim 和 timeout/conclude 逻辑。Bootstrap 保留 Intent 与旧 Intent Interface，不创建 IntentRun；这是 §3 已记录的已知限制，不是可选开发步骤。

## 9. 联网能力

本次不引入新的搜索供应商或浏览器框架。现有 Worker 已能在授权运行环境中通过工具访问网络；本次把网络能力变成显式运行约束：

1. GoalSpec 的 constraints 记录允许访问的目标范围和联网限制。
2. Intake Prompt 只判断是否具备启动条件，不主动执行项目任务；实现复用现有 Worker Adapter，不新增技术性 no-tool 安全边界。
3. Explore 可以使用当前 Worker 工具和网络环境。
4. 网络获取的大段内容必须保存为 workspace artifact，Fact 只写经过验证的结论和引用。
5. Dispatcher 启动健康检查继续验证模型端点；是否增加通用 Web Search Interface 不属于本次实现。

如果后续需要搜索引擎级 `search/fetch` Interface，必须先单独完成当前主流方案调研和供应商选择，再增加 Adapter；不得把某个搜索供应商直接写死在 Reason/Explore 中。

## 10. Server 模型与迁移

### 10.1 Pydantic 模型

至少新增：

```text
ProjectStatus
ProjectIntakeView
IntakeQuestion
IntakeAnswer
SubmitIntakeAnswersRequest
IntakeRetryRequest
IntakeConfirmRequest
IntakeProposal
ForceActivateRequest
IntakeClaimRequest
IntakeDecisionRequest
GoalSpec
IntentRun
IntentRunSummary
IntentRunClaimRequest
IntentRunClaimResponse
IntentRunYieldRequest
IntentRunConcludeRequest
IntentRunFailRequest
ContinueEvidence
ArtifactRef
```

`ProjectMeta.status` 使用统一的 ProjectStatus 类型（`preparing | waiting_input | ready_review | intake_failed | active | stopped | completed`），禁止在多个模型中重复写 Literal。

`ProjectDetail` 增加 `intake: ProjectIntakeView | None`，其中不包含 transcript；视图包含 `pending_proposal: IntakeProposal | None`（origin/goal/spec/notices）和 `auto_activate: bool | None`。

`CreateProjectRequest` 增加 `auto_activate: bool | None = None`；settings 增加 `intake_auto_activate: bool = false`，更新请求中该字段可选，旧调用者不提交时保留原值。

`ProjectDetail` 还增加 `intent_runs: list[IntentRunSummary] = []`，用于 Scheduler 在 claim 前选择 yielded/new/failed-due Intent。摘要中的 `has_session` 和 `worker_name` 让 Scheduler 在 `attempt_count` 到达上限时仍可恢复同一个 session：非 Pi 按相同 `worker_type`，Pi 还必须是相同 `worker_name`；不兼容或没有 session 才需要新 attempt。默认空列表保持旧项目和现有测试的兼容性。

`ProjectSummary` 增加 `pending_question_count: int = 0`、`waiting_since: str | None = None` 和 `intake_last_error: str | None = None`，供列表展示等待时长与清理后的失败摘要。

### 10.2 数据库迁移

当前项目使用 `CREATE TABLE IF NOT EXISTS` 加 `_ensure_*` 迁移。实现时：

1. 新建 `project_intakes` 和 `intent_runs` 表。
2. settings 表增加 `intake_lease_timeout/max_intake_attempts/max_intake_clarification_rounds`。`CREATE TABLE IF NOT EXISTS` 不会给旧表补列，必须增加幂等 `_ensure_settings_columns` 迁移并保留旧值。
3. 不修改已有 Project、Fact、Intent 数据。
4. 现有项目保持原 status，不生成 intake 记录。
5. SQLite 的 projects.status 没有 CHECK 约束，因此不需要重建表。
6. 迁移必须幂等；重复启动不得丢数据或重复创建 Origin/Goal。
7. Settings 的更新请求中新字段必须可选；旧前端只提交原有字段时，Server 保留已配置的 intake 值，不能用 Pydantic 默认值静默覆盖。

激活项目时必须先检查 Origin/Goal 不存在；若事务重试，采用幂等语义返回当前 active 项目，而不是插入重复记录。

### 10.3 配置写回链路

`server/routers/worker_config.py::_build_dispatch_config` 会在前端每次 Save 时重建整份 `dispatch.yaml`。因此它不能继续使用只包含 `bootstrap/reason/explore` 和旧 timeout 键的硬编码模板；必须同步生成 `intake`、`checkpoint_timeout`、`max_session_attempts`、`retry_delay`、`max_no_progress_slices` 和 `max_running_intakes`，并用回归测试确认 Save 后它们仍存在。

方案制定时的 Docker Compose 把 `dispatch.yaml` 以 `:ro` 挂载，与已存在的前端保存 Interface 冲突。本分支保留保存功能，所以去掉 `:ro`，并同步修正 `start.md` 的挂载说明。配置是 bind-mounted 单文件，保持现有原位 `write_text` 写法，不引入 rename 替换。

## 11. Dispatcher 调度顺序

新的单轮顺序：

1. 回收已完成 Future。
2. 读取项目摘要。
3. 按任务类型取消已失效的运行任务：intake 只在 `preparing` 合法；bootstrap/reason/explore 只在 `active` 合法；项目不存在时一律取消。
4. 按 6.7 的规则派发 `preparing` 项目的 intake：每个 tick 最多新派发一个、并发不超过 `max_running_intakes`、为 active 项目保留至少一个 Worker 槽位。
5. 按现有规则调度 active 项目的 bootstrap/reason/explore。
6. `waiting_input`、`ready_review` 和 `intake_failed` 项目只被轮询展示，不创建 Future、不创建容器。

IntentRun 恢复优先级：

1. 当前 active 项目内已 yielded 的 open Intent。
2. 当前 active 项目内从未执行的 open Intent。
3. 已 failed 且退避期结束的 open Intent，使用新 session 重试。
4. 仍保持现有跨项目轮转和 Worker 并发限制。

不得引入全项目等待屏障。新 Fact 出现时，Reason 仍可在其他 Explore 运行期间异步执行。

## 12. 错误、并发与幂等

### 12.1 Intake

1. 两个 Dispatcher 同时 claim：一个成功，一个 `409`。虽然当前只支持单 Dispatcher，也必须保持 Server 原子性。
2. 用户回答与旧模型结果竞态：通过 revision 拒绝旧 decision。
3. Goal Gate timeout：release lease，status 保持 preparing，记录清理后的 last_error，`attempt_count + 1`。
4. Dispatcher 重启：过期 intake lease 被清理后重新审查同一 revision。
5. 用户删除项目：级联删除 intake；迟到 decision 返回 `404`。
6. 技术失败达到 `max_intake_attempts` 或问答轮次达到 `max_intake_clarification_rounds`：进入 `intake_failed`，不再被自动调度。
7. Retry 只在 `intake_failed` 有效：`revision + 1`、`attempt_count` 清零，`clarification_rounds` 保持不变；成功后状态已是 `preparing`，重复 Retry 返回 `409`。
8. 强制激活幂等：重复 force 返回当前 ProjectDetail，不重复插入 Origin/Goal。
9. Additional context 与答案提交使用同一 revision 校验，竞态返回 `409`。
10. confirm、编辑提交与 force 在 `ready_review` 下的竞态：都以 revision 和状态校验串行化，先到者生效，后到者 `409`。
11. `ready_review` 期间 force 使用 raw 输入激活，proposal 作废并清空。

### 12.2 IntentRun

1. yield 与 project stop 竞态：stop 先发生时，yield 返回 `403/409`，不得写 checkpoint 后重新开放项目。
2. conclude 重试：第一次已成功时，第二次返回已有完成结果或明确 `409`，不得创建第二个 Fact。
3. Worker 心跳失效：Run 保留 session/checkpoint，Intent claim 可重新获得。
4. Worker 类型变化：优先相同 Adapter；否则使用 checkpoint-only resume，并明确记录。
5. session 不可恢复：不得创建“恢复失败”Fact。
6. 租约过期翻转与并发 heartbeat 的竞态：由 `BEGIN IMMEDIATE` 事务串行化；过期翻转后迟到的 heartbeat/yield 收到 `409`。
7. 僵尸 Worker 窗口：旧 Worker 在心跳失败前可能仍在运行，其写入因 lease 校验失败被拒绝；新 Worker 恢复 session 时必须容忍被中断的 tool call。

## 13. 日志与可观测性

新增状态变化日志，稳定轮询不刷屏：

```text
intake dispatched
intake waiting_input question_count=N revision=R round=N
intake ready_review revision=R
intake confirmed project
intake activated project mode=gate|forced
intake failed reason=attempts_exhausted|rounds_exhausted revision=R
intake retried revision=R
intake released error=...
intent run created
intent run resumed attempt=N mode=session|checkpoint_only
intent run expired->yielded intent=...
intent run yielded no_progress=N
intent run completed fact=...
intent run failed reason=...
explore checkpoint triggered reason=timeout|parse_failed|invalid_payload|exit_code
```

日志不得包含：

1. 模型密钥。
2. 完整 session 内容。
3. 用户答案中可能存在的凭证原文。此类字段只记录长度或已清理预览。

前端第一版只展示 Project Intake；IntentRun 暂不增加复杂监控面板。现有 Intent 的 running/unclaimed 图形语义保持不变。

## 14. 文件级开发清单

### 14.1 新增文件

```text
cairn/src/cairn/server/intake.py
cairn/src/cairn/server/intent_runtime.py
cairn/src/cairn/server/routers/intake.py
cairn/src/cairn/server/routers/intent_runs.py
cairn/src/cairn/dispatcher/tasks/intake.py
cairn/src/cairn/dispatcher/runtime/intent_run.py
cairn/src/cairn/dispatcher/prompts/default/intake.md
cairn/src/cairn/dispatcher/prompts/default/explore_resume.md
cairn/src/cairn/dispatcher/prompts/default/explore_checkpoint.md
cairn/src/cairn/dispatcher/prompts/mock/intake.md
cairn/src/cairn/dispatcher/prompts/mock/explore_resume.md
cairn/src/cairn/dispatcher/prompts/mock/explore_checkpoint.md
```

### 14.2 修改文件

```text
cairn/src/cairn/server/db.py
cairn/src/cairn/server/models.py
cairn/src/cairn/server/services.py
cairn/src/cairn/server/app.py
cairn/src/cairn/server/routers/projects.py
cairn/src/cairn/server/routers/intents.py
cairn/src/cairn/server/routers/export.py
cairn/src/cairn/server/routers/settings.py
cairn/src/cairn/server/routers/worker_config.py
cairn/src/cairn/server/static/index.html
cairn/src/cairn/dispatcher/config.py
cairn/src/cairn/dispatcher/contracts.py
cairn/src/cairn/dispatcher/models.py
cairn/src/cairn/dispatcher/protocol/client.py
cairn/src/cairn/dispatcher/scheduler/loop.py
cairn/src/cairn/dispatcher/tasks/explore.py
cairn/src/cairn/dispatcher/tasks/common.py
cairn/src/cairn/dispatcher/workers/base.py
cairn/src/cairn/dispatcher/workers/adapters/*.py
dispatch.yaml
dispatch.example.yaml
docker-compose.yaml
start.md
docs/specs/dispatcher-design.md
docs/specs/server-protocol.md
```

只有在实际实现需要时才修改清单中的文件；不要为了对齐风格批量重构。

## 15. 推荐开发顺序

每一步必须保持测试可运行，避免一次提交同时改完整前后端。

### 第 1 步：Project Intake 数据与状态

1. 增加 ProjectStatus、intake 模型和数据库表。
2. 修改创建项目为 preparing，暂不创建 Origin/Goal。
3. 实现 Project Intake Module 和 HTTP Interface，包括 answers（含 Additional context、ready_review 编辑提交）、retry、confirm、force。
4. 实现 `ready_review` 状态与 `auto_activate` 分支。
5. 实现 `intake_failed` 状态及技术失败/问答轮次上限。
6. 增加 migration、Server 状态机和并发测试。

完成标准：纯 HTTP 测试可以走完 `preparing -> waiting_input -> preparing -> ready_review -> (confirm | edit) -> active`、`preparing -> intake_failed -> (retry | force) -> active`，以及 auto_activate 项目的 `preparing -> active`。

### 第 2 步：Goal Gate Dispatcher

1. 增加 intake TaskType、配置和 Prompt。
2. 复用现有 Worker Adapter 执行 intake，不创建项目 workspace。
3. 增加 Dispatcher claim/heartbeat/decision/release。
4. 实现 intake 并发与节奏限制（每 tick 最多新派发一个、最多 `max_running_intakes` 个并发、为 active 保留 Worker 槽位）。
5. Mock Adapter 覆盖 ready/questions/error/timeout/非法 questions。

完成标准：Mock 端到端测试中，信息充分项目停在 ready_review（auto_activate 项目直接 active）；信息不足项目稳定停在 waiting_input；持续失败项目停在 intake_failed。

### 第 3 步：前端等待输入与启动确认

1. 增加状态 Badge、计数和卡片操作。
2. 增加 preparing、waiting_input、ready_review、intake_failed 详情视图。
3. 增加问题回答、unknown、Additional context、revision 冲突处理。
4. 增加启动确认卡（可编辑 origin/goal、notices、确认/重审）与 7.3 的布局和溢出处理。
5. 增加 Retry 与 Start Anyway（二次确认）。
6. 修正轮询中的状态转换和 Cytoscape 初始化。

完成标准：浏览器中可创建项目、看到 AI 问题、提交答案；确认卡可查看并修改 origin/goal，确认或重审后进入图界面；失败项目可 Retry 或强制激活。

### 第 4 步：IntentRun Server

1. 增加 intent_runs 表和 Intent Runtime Module。
2. 实现 claim/yield/conclude/fail 的原子状态转换。
3. run/heartbeat 同事务刷新 `intents.last_heartbeat_at`；`expire_workers` 同事务翻转过期 running Run。
4. 实现新旧 claim 以 `intents.worker` 为互斥点的全部规则。
5. 保留旧 Intent Interface 兼容。
6. 增加 lease、幂等和 stop 竞态测试。

完成标准：纯 HTTP 测试证明 continue 不创建 Fact，conclude 只创建一个 Fact；任何读取路径都不会产生 `worker` 已清空但 Run 仍 running 的记录。

### 第 5 步：Explore 时间片与恢复

1. 修改 contracts 支持 fact/continue/rejected，continue 携带证据字段。
2. 增加 checkpoint/resume Prompt。
3. Worker Driver 增加 build_resume。
4. 实现 8.4 的异常结束矩阵：timeout、解析失败、校验失败、非零退出统一先 checkpoint。
5. 实现累计证据 merge 与 no-progress 限制。
6. Scheduler 优先恢复 yielded Run。
7. 增加 Mock 端到端测试。

完成标准：Mock Explore 连续两个时间片后，在同一 Intent 上产生一个 Fact；中间没有新增 Fact 或 Intent；解析失败的时间片也能经 checkpoint 抢救。

### 第 6 步：配置、协议文档与回归

1. 修复 worker-config Save 对新键的保留，修正 Docker Compose 可写挂载。
2. 更新 dispatcher-design、server-protocol 和 example config。
3. 运行全部测试。
4. 人工验证前端状态和恢复流程。
5. 进行一轮独立完整性/缺陷审查，修复后再跑全部验证。
6. 由用户用同一模型与测评环境运行固定测评，仅记录总分。

## 16. 测试规格

### 16.1 Server 单元/接口测试

必须新增：

1. 创建项目返回 preparing、facts 为空。
2. preparing/waiting_input/ready_review/intake_failed 项目不能创建 Intent、Reason、Complete 或导出图。
3. intake questions 数量 0 或大于 3 被拒绝。
4. waiting_input 只接受完整答案。
5. unknown 答案可提交。
6. 陈旧 revision 返回 409。
7. ready decision 原子创建 Origin/Goal 和 hints。
8. 重复 ready decision 不重复建 Fact。
9. 旧 active 项目没有 intake 也能正常读取。
10. IntentRun claim 同时更新 Intent worker。
11. yield 清空 Intent worker且不增加 Fact 数量。
12. conclude 同时增加 Fact、关闭 Intent 和完成 Run。
13. 同一 Run 的竞争 claim 只有一个成功。
14. project stop 后迟到 yield/conclude 被拒绝。
15. migration 重复执行幂等。
16. Run 持续 heartbeat 期间，反复执行 `GET /projects`、`GET /projects/{id}` 和 export 不会清除 claim。
17. Run 心跳过期后，`intents.worker` 清空与 running Run 翻转 yielded 在同一事务中生效。
18. 任何读取路径执行后，不存在 `intents.worker IS NULL` 且 IntentRun 仍为 running 的记录。
19. 技术失败达到 `max_intake_attempts` 后项目进入 `intake_failed`；Retry 后回到 `preparing` 且 `attempt_count` 清零。
20. 问答轮次达到 `max_intake_clarification_rounds` 后，questions decision 不保存新问题，pending 为空并进入 `intake_failed`。
21. 非法 key 前缀、重复 key、已回答 key（含 unknown）再次出现的 decision 被拒绝并计为技术失败。
22. 强制激活：从 `waiting_input`/`ready_review`/`intake_failed` 成功；从 `preparing` 拒绝；缺少 `confirm` 拒绝；重复 force 幂等；`activation_mode='forced'` 且不产生额外 Fact/Hint；`ready_review` 下 force 使用 raw 输入并清空 proposal。
23. ProjectDetail 不返回 transcript；Additional context 提交后 revision 递增。
24. export 对 `preparing/waiting_input/ready_review/intake_failed` 返回 409。
25. 旧式 claim 与 run/claim 双向竞争均为 409；相同 `claim_id` 重试幂等，同一 Worker 使用不同 `claim_id` 返回 409。
26. 旧式 heartbeat/release/conclude 对 yielded/failed/completed Run 也返回 409，不只阻止 running Run。
27. yield 引用不存在的 artifact 在 Dispatcher 校验阶段被拒绝。
28. 累计证据对省略、重排、`A -> B -> A`、旧 milestone 和下降 cursor 均不计进展；新 artifact 版本、新 milestone 或 cursor 严格增长才重置 no-progress。
29. 旧 settings DB 自动补三列且保留原值；旧 active 项目返回 `intake=null/intent_runs=[]`。
30. 创建时 hints 只在 intake 暂存，ready/force 并发或重试也只写入一次 Origin/Goal/hints。
31. intake lease 过期计入失败，达上限转 failed；旧 claim 的 heartbeat/decision/release 均返回 409。
32. decision 本地校验失败通过 release 实际增加 attempt；不能只测试会回滚的异常路径。
33. `intake_failed` answers 必须为空且 Additional context 非空；waiting_input 必须完整回答。
34. yielded 同 session 恢复不增 attempt；checkpoint-only 和 failed 到期新 session 增加；未到期或耗尽拒绝 claim。
35. `next_retry_at` 持久化，重建 Dispatcher 后仍遵守退避。
36. stop、complete、expire 各自保证不存在 `worker IS NULL + run running`，迟到 heartbeat/yield/conclude/fail 全部被拒绝。
37. worker-config Save 后 intake/checkpoint/retry/no-progress/max-running-intakes 配置仍存在。
38. ready decision：未开启 `auto_activate` 时写入 proposal 并转 `ready_review`，不写知识图；开启时直接激活。
39. confirm 原子创建 Origin/Goal/hints 并清空 proposal；重复 confirm 幂等；陈旧 revision 返回 409。
40. `ready_review` 编辑提交覆盖 raw_origin/raw_goal、清空 proposal、revision+1 回 `preparing`；origin/goal 任一为空返回 422；`answers` 非空返回 422。
41. settings 中 `intake_auto_activate` 缺省 false；创建请求的项目级 `auto_activate` 优先于 Server 设置。

### 16.2 Dispatcher 测试

必须新增：

1. preparing 只派发 intake。
2. waiting_input、ready_review 和 intake_failed 不派发任何任务。
3. 单 tick 最多新派发一个 intake；并发不超过 `max_running_intakes`；仅剩最后一个全局 Worker 空位且存在可调度 active 任务时不派发 intake。
4. intake 不创建项目 workspace 或项目 runtime 标识。
5. ready 激活（auto_activate）或 confirm 后下一轮才进入 bootstrap/reason。
6. Explore 返回 continue 时调用 yield，不调用 conclude。
7. timeout 使用同一 session 进入 checkpoint。
8. yielded Run 下一轮优先恢复。
9. session 不可用时使用 checkpoint-only resume。
10. no-progress 达到阈值后停止续跑。
11. fact 结果仍保持原有写图行为。
12. rejected 不创建 Fact。
13. JSON 解析失败、payload 校验失败、非零退出与 timeout 一样进入 checkpoint。
14. 用户 Stop/Delete、lease 丢失、明确 rejected 不进入 checkpoint。
15. 过期翻转后无 checkpoint 的首个时间片按新执行恢复同一 Intent。
16. `_cancel_inactive_tasks` 只在 intake 离开 preparing 或其他任务离开 active 时取消，不会在下一 tick 误杀正常 intake。
17. Scheduler 按 yielded、无 Run、failed-due 排序，terminal failed 不派发；yielded 优先相同 worker type。
18. 无 intake Worker 且存在 preparing 项目时只记录 Dispatcher warning，不让 Server 启动失败。
19. 新配置的默认值、Prompt 资源、`build_resume` 与 Mock intake/continue/checkpoint 契约都有测试。

### 16.3 Mock 端到端测试

至少覆盖：

1. 充分输入：preparing -> active -> reason/explore -> completed。
2. 缺参输入：preparing -> waiting_input；此时 facts/intents 保持为空。
3. 回答后：waiting_input -> preparing -> active。
4. Explore：continue -> continue -> fact，全程只有一个 Intent。
5. Dispatcher 在第一次 continue 后重启，第二次运行仍恢复同一个 Run。
6. 项目在 yielded 后 stopped，不再恢复。
7. 持续缺参：问答轮次耗尽后进入 intake_failed；用户强制激活后进入 active 且图中有 Origin/Goal。
8. continue 携带证据：cursor 增长或新 artifact 重置 no-progress；仅措辞变化计为无进展。
9. ready_review 流：充分输入 -> ready_review（无 Fact、proposal 已存）；编辑提交后重新审查 -> ready_review；confirm 后 -> active 且图中有 Origin/Goal。
10. auto_activate 流：充分输入 + `auto_activate=true` -> 不经 ready_review 直接 active。

### 16.4 前端人工验收

1. 创建项目后立即显示 PREPARING。
2. 缺参后列表和详情都显示 WAITING INPUT。
3. 1～3 个问题都可输入答案或选择 unknown。
4. 未完整回答时不能提交。
5. 提交后立即显示 PREPARING。
6. 激活后自动显示 Origin/Goal 图，不需要手动刷新。
7. waiting_input 项目没有 Stop/Resume 按钮。
8. 页面刷新后问题和未完成状态仍存在。
9. 多标签页提交导致 revision 冲突时能刷新到最新问题。
10. `intake_failed` 显示错误摘要、Retry 和 Start Anyway；Start Anyway 有二次确认。
11. waiting_input 和 intake_failed 都可提交 Additional context，提交后进入 preparing。
12. `ready_review` 显示启动确认卡：origin/goal 可编辑，空值时“确认启动”不可用，有改动时“保存并重新审查”可用，确认后自动进入图界面。
13. 缺失信息以有序列表展示且编号与问题区块一一对应；长 Origin/Goal 在面板内限高滚动，回答表单始终可见；任何内容不出现横向滚动。

## 17. 验收标准

只有同时满足以下条件才能声称实现完成：

1. 用户输入不会直接进入 active。
2. Goal Gate 信息充分时不提问：未开启 `auto_activate` 的项目进入 `ready_review`，用户确认或编辑重审后才激活；开启 `auto_activate` 的项目直接激活。
3. 缺少关键参数时，项目进入 waiting_input，并展示 1～3 个问题。
4. waiting_input/ready_review/intake_failed 期间 Dispatcher 不启动 bootstrap/reason/explore。
5. 用户回答后能够重新审查并最终激活。
6. Explore timeout 后可以 continue，同一 Intent id 保持不变。
7. continue 期间 Fact 数量和 Reason 触发状态不变。
8. Dispatcher 重启后能恢复 session 或 checkpoint。
9. 无进展的同一个 IntentRun/session 不会无限续跑。
10. 现有 active/stopped/completed 项目仍能运行。
11. 全部自动化测试通过。
12. 前端人工验收通过。
13. 任意读取流量下不产生重复认领；不存在 `worker` 已清空但 Run 仍 running 的记录。
14. Goal Gate 技术失败与问答轮次均有上限，耗尽后进入 `intake_failed` 并停止自动调度。
15. 用户可从 `waiting_input`/`ready_review`/`intake_failed` 强制激活；`activation_mode=forced` 可审计，系统不会自动强制激活。
16. 解析失败、校验失败、非零退出的时间片与 timeout 一样先经 checkpoint 抢救。
17. 激活前用户可查看并修改最终 origin/goal；空编辑不能提交；confirm/编辑/force 竞态不会产生第二个 Fact。

## 18. 测评方式

功能验证完成后，用与基线相同的模型和测评环境运行：

```text
模型：DeepSeek V4 Flash
基线分支：main
基线分数：18550 / 23000
实验分支：当前新分支
```

本轮只记录实验总分和相对基线的分数变化，不根据单次分数声称具体子模块一定有效。Goal Gate 对信息充分的固定测评输入应直接 ready；测评脚本创建项目时必须使用 `auto_activate=true`，否则项目会停在 `ready_review` 等待人工确认。IntentRun 才是预计影响测评分数的主要改动。

## 19. 实现时禁止擅自扩展

交给实现者的硬性限制：

1. 不修改 Fact/Intent 核心 schema。
2. 不引入新的前端框架。
3. 不加入模型分层或自动切换模型。
4. 不实现复杂优先级、任务依赖或 DAG 工作流。
5. 不实现向量数据库、RAG 或完整图压缩。
6. 不实现新的搜索供应商。
7. 不把 intake 问答保存为 Hint 或 Fact。
8. 不把 continue 的 progress 保存为 Fact。
9. 不通过延长 timeout 代替 IntentRun。
10. 不为了本方案重构无关代码。
11. 不用文本相似度或向量检索判断问题重复；只能按 `key` 判重。
12. 不为 IntentRun 引入第二套超时时钟；租约真相源只有 `intents.worker` + `intents.last_heartbeat_at`。
13. 不把强制激活做成自动兜底；它只能由用户显式触发。
14. 不把 `progress` 等模型散文计入累计证据或进展判定。

## 20. 最终结果

实现后，Cairn 的三个主要 Module 各自只回答一个问题：

1. **Goal Gate：现在是否具备开始执行的条件？**
2. **Knowledge Plane：已经确认什么，下一步探索什么？**
3. **Intent Runtime：当前探索执行到哪里，如何继续？**

这次改造的价值不是增加更多 Agent，而是停止两种浪费：信息不足时盲目启动，以及工作未完成时反复重新开始。
