# 前端与架构调研 / 2026-09-09

## 研究方式

按用户要求，同时使用两个独立设计研究子 Agent：
- GPT-6 Astra / high：证据、源码与请求联动的专注工作区。
- GPT-5.6 Sol / high：目标发起、研究路线、异常恢复与结果收束。

主 Agent 检查官方资料，整合信息架构并实现一个最终原型。子 Agent 未编辑站点或生产代码。以下是借鉴与判断，不表示直接采用对应代码、品牌或技术栈。

## 官方与社区项目依据

| 来源 | 核实的机制 | Cairn 的取舍 |
| --- | --- | --- |
| [Zed Agent Panel](https://zed.dev/docs/ai/agent-panel) | Agent 工具活动、材料上下文、可选跟随和检查点 | 保留行动与代码的联系；不复制完整 IDE，不强制自动跳转阅读位置 |
| [ZAP History](https://www.zaproxy.org/docs/desktop/ui/tabs/history/) | 历史请求列表与 Request/Response 选择联动 | 每条发现直接进入请求证据；不把几十个安全工具标签页搬到主屏 |
| [mitmproxy Addons](https://docs.mitmproxy.org/stable/addons/overview/) | 通过事件挂钩扩展行为 | 临时分析脚本有明确输入、执行事件与产物；原始流量按需查看 |
| [OpenHands 当前架构](https://raw.githubusercontent.com/OpenHands/OpenHands/main/docs/architecture.md) | 前端与实际 Agent 执行、工作环境职责分离 | 前端发送用户意图、展示真实执行结果；不以聊天记录替代研究状态 |
| [Dradis Issues / Evidence](https://dradis.com/support/guides/projects/issues.html) | 问题与支持问题的具体证据分离 | Finding 必须引用证据；舍弃个人场景不需要的团队 QA 与模板管理 |
| [Caldera Operation Results](https://caldera.readthedocs.io/en/stable/Operation-Results.html) | 行动结果、输出与运行信息可检查 | 保留结果的来源和失败原因；不要求用户先配置 planner / ability / agent |
| [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) | 持久状态与暂停/恢复机制，恢复时需注意副作用重放 | 设计恢复游标、幂等控制和预算延续；暂不引入新调度框架 |
| [Caido Sitemap](https://docs.caido.io/app/quickstart/sitemap) | 从已观察流量形成目标结构 | 材料应来自实际证据；首期用紧凑列表，不做默认全屏图谱 |

以上为 2026-09-09 访问到的内容。OpenHands 产品在演进，旧 GUI 文档不能直接当作当前架构。本文是面向 Cairn 的设计推导，不承诺与来源产品相同功能。

## 两份提案与最终综合

Astra 方案以“研究问题 → 检查 → 证据 → 结论”为主；其最有价值的部分是发现可立即下钻源码/请求、纯网站与纯代码模式不能伪造另一类材料。

Sol 方案以研究路线代替扫描任务表；其最有价值的部分是说明当前方向、失败后的替代路线、暂停和没有发现问题时的正常收束。

最终采用：
1. 左侧项目切换，中间研究过程/发现/材料，右侧上下文证据。
2. 四个轻量研究阶段作为位置标记，不作为固定瀑布，也不显示虚假完成百分比。
3. 深色窄侧栏 + 冷灰阅读区 + 白色证据区，绿色只强调行动和确认状态。
4. 网站、代码、本地运行实例作为材料，不让用户选语言、工具或黑白盒术语。
5. 授权摘要、暂停/继续、报告作为顶层动作；删除常驻审批中心与工具配置导航。
6. 发现主张与证据分离，静态线索不等于动态确认；“已排除”仍有依据和适用边界。

不采纳：
- 完整路线节点图、四栏布局、默认终端、聊天作为全部主界面。
- 不按子 Agent 提案把 Hint 改成“待验证漏洞”：Cairn Hint 仍是人工补充，Hypothesis 单独表示假设。
- 不立即增加拖放压缩包、自动搭环境、团队权限与模型切换面板。
- 不引入框架迁移作为本轮工作。
